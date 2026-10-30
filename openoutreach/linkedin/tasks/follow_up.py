# openoutreach/linkedin/tasks/follow_up.py
"""Follow-up task — runs the agentic follow-up for one eligible CONNECTED deal."""
from __future__ import annotations

import logging
from datetime import timedelta

from django.utils import timezone
from termcolor import colored

from openoutreach.crm.models import DealState
from openoutreach.linkedin.models import ActionLog

logger = logging.getLogger(__name__)

# Required silence between nudges scales with unanswered count:
# 1 unanswered → 3d, 2 → 6d, 3 → 9d. Skips the LLM call while open.
MIN_DAYS_PER_UNANSWERED = 3


def _build_send_profile(deal) -> dict:
    """Minimal profile dict for ``send_raw_message`` and its fallbacks."""
    lead = deal.lead
    return {
        "public_identifier": lead.public_identifier,
        "urn": lead.urn or "",
    }


def _too_soon_to_nudge(deal) -> bool:
    """Wait ``unanswered_count * MIN_DAYS_PER_UNANSWERED`` days between nudges."""
    from openoutreach.chat.models import ChatMessage

    messages = ChatMessage.objects.filter(deal=deal)

    last = messages.order_by("-creation_date").first()
    if last is None or not last.is_outgoing:
        return False

    last_reply = messages.filter(is_outgoing=False).order_by("-creation_date").first()
    nudges = messages.filter(is_outgoing=True)
    if last_reply:
        nudges = nudges.filter(creation_date__gt=last_reply.creation_date)

    required = timedelta(days=nudges.count() * MIN_DAYS_PER_UNANSWERED)
    return timezone.now() - last.creation_date < required


def _connected_deals(campaign):
    """Open, non-disqualified CONNECTED deals in *campaign*, oldest first."""
    from openoutreach.crm.models import Deal

    return (
        Deal.objects.filter(
            campaign=campaign,
            state=DealState.CONNECTED,
            outcome="",
            lead__disqualified=False,
        )
        .select_related("lead", "campaign")
        .order_by("update_date")
    )


def _next_followup_deal(campaign):
    """Oldest CONNECTED deal in *campaign* not on a nudge cooldown."""
    for deal in _connected_deals(campaign):
        if not _too_soon_to_nudge(deal):
            return deal
    return None


def handle_follow_up(task, session, qualifiers):
    from linkedin_cli.actions.message import send_raw_message
    from openoutreach.core.agents.follow_up import run_follow_up_agent
    from openoutreach.core.db.deals import capture_and_contribute, set_profile_state
    from openoutreach.core.db.summaries import materialize_profile_summary_if_missing

    campaign = session.campaign
    ws = getattr(campaign, "workspace", None)
    if not ws:
        logger.warning("[%s] follow_up: workspace unresolved — failing closed without provider action", campaign)
        return

    linkedin_profile = getattr(session, "linkedin_profile", None)
    if not linkedin_profile:
        logger.warning("[%s] follow_up: provider linkedin_profile unresolved — failing closed without provider action", campaign)
        return

    from openoutreach.linkedin.services.account_governor import resolve_provider_context
    res_ws, res_prof, ctx_err = resolve_provider_context(
        workspace=ws,
        linkedin_profile=linkedin_profile,
        campaign=campaign,
    )
    if ctx_err or not res_ws or not res_prof:
        logger.warning("[%s] follow_up: ambiguous or invalid provider context (%s) — failing closed", campaign, ctx_err or "unresolved")
        return

    if linkedin_profile.session_status == "checkpoint":
        logger.warning("[%s] follow_up: LinkedIn session is in CHECKPOINT_REQUIRED state — waiting on operator verification", campaign)
        return

    if not linkedin_profile.can_execute(ActionLog.ActionType.FOLLOW_UP):
        logger.info("[%s] follow_up: daily limit reached — slot skipped", campaign)
        return

    from openoutreach.core.models import WorkspaceAutomationSettings, OperatorActionApproval
    auto_st = WorkspaceAutomationSettings.objects.filter(workspace=ws).first()
    if not auto_st:
        logger.warning("[%s] follow_up: automation settings unresolved for workspace %s — failing closed without provider action", campaign, getattr(ws, "slug", ws))
        return

    from django.db import models as db_models
    has_approval = OperatorActionApproval.objects.filter(
        workspace=ws,
        action_type__in=["follow_up", "first_message", "ai_reply", "message"],
        status="approved",
    ).filter(
        db_models.Q(linkedin_profile=linkedin_profile) |
        db_models.Q(linkedin_profile__isnull=True, operator=linkedin_profile.user)
    ).exists()

    if not auto_st.master_automation_enabled and not has_approval:
        logger.warning("[%s] follow_up: master automation is PAUSED for workspace %s", campaign, getattr(ws, "slug", ws))
        return

    if not auto_st.auto_followup and not has_approval:
        logger.warning("[%s] follow_up: follow-up channel is PAUSED in automation settings", campaign)
        return

    deal = _next_followup_deal(campaign)
    if deal is None:
        connected = _connected_deals(campaign).count()
        if connected:
            logger.info(
                "[%s] follow_up: %d connected lead(s), all within nudge cooldown — nothing due",
                campaign, connected,
            )
        else:
            logger.info("[%s] follow_up: no connected leads yet — nobody to follow up", campaign)
        return

    if deal and hasattr(deal, "campaign") and getattr(deal.campaign, "linkedin_profile", None):
        if deal.campaign.linkedin_profile != linkedin_profile:
            logger.error("[%s] follow_up: deal campaign profile (%s) mismatch with session profile (%s) — failing closed", campaign, deal.campaign.linkedin_profile, linkedin_profile)
            return

    public_id = deal.lead.public_identifier
    if deal and ActionLog.objects.filter(campaign=deal.campaign, metadata__lead_public_id=public_id).exclude(linkedin_profile=linkedin_profile).exists():
        logger.error("[%s] follow_up: historical action log for deal #%d belongs to a different LinkedInProfile — failing closed", campaign, deal.id)
        return

    logger.info(
        "[%s] %s %s",
        campaign, colored("▶ follow_up", "green", attrs=["bold"]), public_id,
    )

    # Retry the contact-info overlay while still email-empty — LinkedIn may not
    # have exposed the 1st-degree address at connect time; a later visit picks it
    # up and contributes it. No-op once an email is captured.
    # Recheck for inbound reply before sending canned follow-up
    from openoutreach.chat.models import ChatMessage
    inbound_messages = ChatMessage.objects.filter(deal=deal, is_outgoing=False)
    inbound_exists = inbound_messages.exists()

    ai_replies_enabled = bool(auto_st.auto_monitor_replies)

    if inbound_exists and not ai_replies_enabled:
        logger.info("[%s] follow_up for %s: inbound message already received and AI replies disabled (auto_monitor_replies=False) — suppressing canned follow-up", campaign, public_id)
        deal.save()
        return

    materialize_profile_summary_if_missing(deal, session)
    decision = run_follow_up_agent(session, deal)
    profile = _build_send_profile(deal)

    if decision.action == "send_message":
        from openoutreach.linkedin.models import ProviderWriteIntent
        # Determine concrete operation and content type
        has_outbound = ChatMessage.objects.filter(deal=deal, is_outgoing=True).exists()
        if inbound_exists:
            op_type = ProviderWriteIntent.OperationType.AI_REPLY_SEND
            content_type = "ai_reply"
            latest_inbound = inbound_messages.order_by("-creation_date", "-pk").first()
            action_id = f"deal_{deal.id}_inbound_{latest_inbound.id}" if latest_inbound else f"deal_{deal.id}_reply_{timezone.now().date()}"
        elif not has_outbound:
            op_type = ProviderWriteIntent.OperationType.FIRST_MESSAGE_SEND
            content_type = "first_message"
            action_id = f"deal_{deal.id}_first_msg"
        else:
            op_type = ProviderWriteIntent.OperationType.FOLLOW_UP_SEND
            content_type = "follow_up"
            action_id = f"deal_{deal.id}_follow_up_{timezone.now().date()}"

        # External Action Gate validation
        from openoutreach.core.services.external_action_gates import evaluate_external_action_gates
        gate_action_type = "ai_reply" if inbound_exists else ("first_message" if not has_outbound else "follow_up")
        permitted, gate_reason = evaluate_external_action_gates(
            workspace=ws,
            campaign=campaign,
            linkedin_profile=getattr(session, "linkedin_profile", None),
            lead=deal.lead,
            deal=deal,
            action_type=gate_action_type,
        )
        if not permitted:
            logger.warning("[%s] %s for %s blocked by external action gate: %s", campaign, op_type, public_id, gate_reason)
            deal.save()
            return

        # Canonical Outbound Validation
        from openoutreach.core.content_style import validate_linkedin_outbound_message
        msg_text = decision.message or ""

        outbound_res = validate_linkedin_outbound_message(
            message_text=msg_text,
            content_type=content_type,
            workspace=ws,
            linkedin_profile=session.linkedin_profile,
            deal=deal,
        )

        if not outbound_res["allowed"]:
            logger.error(
                "[%s] %s message for %s failed canonical outbound validation (reason=%s, errors=%s) — send BLOCKED",
                campaign, op_type, public_id, outbound_res["reason"], outbound_res["errors"],
            )
            deal.save()
            return

        # Claim one-time operator approval if present
        from openoutreach.core.models import OperatorActionApproval
        from django.db import models as db_models
        approval_qs = OperatorActionApproval.objects.filter(
            workspace=ws,
            action_type__in=["follow_up", "first_message", "ai_reply", "message"],
            status="approved",
        ).filter(
            db_models.Q(linkedin_profile=linkedin_profile) |
            db_models.Q(linkedin_profile__isnull=True, operator=linkedin_profile.user)
        )
        if deal.lead_id:
            approval_qs = approval_qs.filter(db_models.Q(lead=deal.lead) | db_models.Q(lead__isnull=True))
        if deal.id:
            approval_qs = approval_qs.filter(db_models.Q(deal=deal) | db_models.Q(deal__isnull=True))
        approval = approval_qs.first()
        if approval:
            if not approval.is_valid_for(
                workspace=ws,
                linkedin_profile=linkedin_profile,
                lead=deal.lead,
                deal=deal,
                message_text=msg_text,
                action_type=approval.action_type,
            ):
                approval = None
            else:
                approval.status = "claimed"
                approval.claimed_at = timezone.now()
                approval.save(update_fields=["status", "claimed_at"])

        # Determine concrete operation type for durable ProviderWriteIntent
        from openoutreach.linkedin.services.idempotency import (
            claim_provider_write,
            record_provider_write_attempt,
            confirm_provider_write,
            fail_provider_write,
        )
        from openoutreach.linkedin.services.account_governor import account_provider_lock

        allowed, write_reason, write_intent = claim_provider_write(
            workspace=ws,
            linkedin_profile=session.linkedin_profile,
            operation_type=op_type,
            target_identity=public_id,
            logical_action_identity=action_id,
        )
        if not allowed:
            logger.warning("[%s] %s for %s blocked by durable write intent: %s", campaign, op_type, public_id, write_reason)
            deal.save()
            return

        # Final defense-in-depth gate check immediately before provider write boundary
        final_gate = validate_linkedin_outbound_message(
            message_text=msg_text,
            content_type=content_type,
            workspace=ws,
            linkedin_profile=session.linkedin_profile,
            deal=deal,
        )
        if not final_gate["allowed"]:
            logger.error(
                "[%s] %s message for %s failed final pre-send gate (reason=%s, errors=%s) — send BLOCKED",
                campaign, op_type, public_id, final_gate["reason"], final_gate["errors"],
            )
            fail_provider_write(write_intent, f"{final_gate['reason']}: {final_gate['errors']}", before_provider=True)
            deal.save()
            return

        logger.info("[%s] %s message for %s: %s", campaign, op_type, public_id, msg_text)
        with account_provider_lock(linkedin_profile=session.linkedin_profile, workspace=ws, operation_name=op_type):
            try:
                record_provider_write_attempt(write_intent)
                sent = send_raw_message(session, profile, msg_text)
                if sent:
                    confirm_provider_write(write_intent, confirmation_data={"length": len(msg_text)})
                else:
                    fail_provider_write(write_intent, "send_returned_false", before_provider=False)
            except Exception as send_err:
                logger.error("%s for %s: exception during send — %s", op_type, public_id, send_err)
                fail_provider_write(write_intent, str(send_err), before_provider=False)
                sent = False
                if approval and approval.status == "claimed":
                    approval.failure_reason = str(send_err)
                    approval.save(update_fields=["failure_reason"])

        if not sent:
            logger.warning("follow_up for %s: send failed or was not confirmed — remaining CONNECTED on cooldown", public_id)
            deal.save()
            return

        if approval and approval.status == "claimed":
            approval.status = "consumed"
            approval.consumed_at = timezone.now()
            approval.save(update_fields=["status", "consumed_at"])

        try:
            session.linkedin_profile.record_action(
                ActionLog.ActionType.FOLLOW_UP, session.campaign,
                metadata={"lead_public_id": public_id, "message": msg_text, "operation_type": op_type},
            )
        except Exception as log_err:
            logger.error("Failed to record ActionLog for follow_up to %s (send preserved): %s", public_id, log_err)

        import uuid
        try:
            ChatMessage.objects.create(
                deal=deal,
                content=msg_text,
                is_outgoing=True,
                owner=getattr(session, "django_user", None),
                creation_date=timezone.now(),
                linkedin_urn=f"local:out:{timezone.now().timestamp()}:{uuid.uuid4().hex[:8]}",
            )
        except Exception as chat_err:
            logger.debug("Local ChatMessage record creation: %s", chat_err)

        from openoutreach.linkedin.db.chat import sync_conversation
        try:
            sync_conversation(session, public_id)
        except Exception:
            logger.exception("post-send sync failed for %s (best-effort)", public_id)
        deal.save()

    elif decision.action == "mark_completed":
        set_profile_state(session, public_id, DealState.COMPLETED.value, outcome=decision.outcome)
        logger.info("[%s] follow_up completed for %s: outcome=%s", campaign, public_id, decision.outcome)

    elif decision.action == "wait":
        deal.save()
