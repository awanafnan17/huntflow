# openoutreach/linkedin/tasks/connect.py
"""Connect task — resolves one candidate from the campaign pool and acts.

Lazy: the task payload carries only ``campaign_id``. The handler picks
its candidate at execution time via the campaign's ``ConnectStrategy``.
No self-rescheduling — pacing is owned by ``tasks/scheduler.py``.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

from termcolor import colored
from django.utils import timezone

from openoutreach.core.db.deals import increment_connect_attempts, set_profile_state
from openoutreach.crm.models import DealState
from openoutreach.linkedin.db.leads import disqualify_lead
from openoutreach.linkedin.models import ActionLog
from openoutreach.linkedin.browser.connect_with_note import ConnectionNoteUIError
from linkedin_cli.exceptions import ProfileInaccessibleError, ReachedConnectionLimit, SkipProfile

logger = logging.getLogger(__name__)

MAX_CONNECT_ATTEMPTS = 3


@dataclass
class ConnectStrategy:
    find_candidate: Callable
    pre_connect: Callable | None
    qualifier: object


def strategy_for(campaign, qualifiers):
    """Build the right ConnectStrategy based on campaign type."""
    qualifier = qualifiers.get(campaign.pk)
    from openoutreach.linkedin.pipeline.pools import find_candidate

    return ConnectStrategy(
        find_candidate=lambda s: find_candidate(s, qualifier),
        pre_connect=None,
        qualifier=qualifier,
    )


def handle_connect(task, session, qualifiers):
    from linkedin_cli.actions.status import get_connection_status
    from openoutreach.core.agents.connection_note import generate_connection_note
    from openoutreach.linkedin.browser.connect_with_note import send_connection_request_with_note

    campaign = session.campaign
    session_profile = getattr(session, "linkedin_profile", None)
    if not session_profile or not getattr(session_profile, "pk", None):
        logger.error("[%s] connect: session lacks valid LinkedInProfile — failing closed", campaign)
        return

    camp_profile = getattr(campaign, "linkedin_profile", None)
    if camp_profile and camp_profile != session_profile:
        logger.error("[%s] connect: campaign profile (%s) mismatch with session profile (%s) — failing closed", campaign, camp_profile, session_profile)
        return

    strategy = strategy_for(campaign, qualifiers)

    ws = getattr(campaign, "workspace", None)
    from openoutreach.core.models import WorkspaceAutomationSettings, OperatorActionApproval
    auto_settings = WorkspaceAutomationSettings.objects.filter(workspace=ws).first() if ws else None

    from django.db import models as db_models
    has_approval = False
    if ws:
        has_approval = OperatorActionApproval.objects.filter(
            workspace=ws,
            action_type="connect",
            status="approved",
        ).filter(
            db_models.Q(linkedin_profile=session_profile) |
            db_models.Q(linkedin_profile__isnull=True, operator=session_profile.user)
        ).exists()

    if ws and not auto_settings and not has_approval:
        logger.warning("[%s] connect: automation settings unresolved for workspace — failing closed without provider action", campaign)
        return

    if auto_settings:
        if not auto_settings.master_automation_enabled and not has_approval:
            logger.warning("[%s] connect: master automation is PAUSED in automation settings", campaign)
            return
        if not auto_settings.auto_send_connections and not has_approval:
            logger.warning("[%s] connect: connections channel is PAUSED in automation settings", campaign)
            return

    if session.linkedin_profile.session_status == "checkpoint":
        logger.warning("[%s] connect: LinkedIn session is in CHECKPOINT_REQUIRED state — waiting on operator verification", campaign)
        return

    if not session.linkedin_profile.can_execute(ActionLog.ActionType.CONNECT):
        _used = session.linkedin_profile._daily_count(ActionLog.ActionType.CONNECT)
        _limit = session.linkedin_profile.connect_daily_limit
        logger.info(
            "[%s] connect: daily limit reached — slot skipped (used=%d, limit=%d)",
            campaign, _used, _limit,
        )
        return

    from openoutreach.core.db.deals import _deal_to_profile_dict

    target_pid = task.payload.get("public_identifier") if task and task.payload else None
    target_lead_id = task.payload.get("lead_id") if task and task.payload else None
    candidate = None
    if target_lead_id:
        from openoutreach.crm.models import Deal
        deal_any = Deal.objects.filter(lead_id=target_lead_id).select_related("campaign", "lead").first()
        if deal_any and hasattr(deal_any, "campaign") and getattr(deal_any.campaign, "linkedin_profile", None):
            if deal_any.campaign.linkedin_profile != session_profile:
                logger.error("[%s] connect: target lead belongs to deal under campaign profile (%s) != session profile (%s) — failing closed", campaign, deal_any.campaign.linkedin_profile, session_profile)
                return
        deal_target = Deal.objects.filter(
            lead_id=target_lead_id,
            campaign=campaign,
            state=DealState.READY_TO_CONNECT,
        ).select_related("lead").first()
        if not deal_target:
            deal_target = Deal.objects.filter(
                lead_id=target_lead_id,
                campaign=campaign,
            ).select_related("lead").first()
        if deal_target:
            candidate = _deal_to_profile_dict(deal_target)
        else:
            logger.warning("[%s] connect: target lead_id %s not in current campaign — failing closed", campaign, target_lead_id)
            return
    elif target_pid:
        from openoutreach.crm.models import Deal
        deal_any = Deal.objects.filter(lead__public_identifier=target_pid).select_related("campaign", "lead").first()
        if deal_any and hasattr(deal_any, "campaign") and getattr(deal_any.campaign, "linkedin_profile", None):
            if deal_any.campaign.linkedin_profile != session_profile:
                logger.error("[%s] connect: target PID belongs to deal under campaign profile (%s) != session profile (%s) — failing closed", campaign, deal_any.campaign.linkedin_profile, session_profile)
                return
        deal_target = Deal.objects.filter(
            lead__public_identifier=target_pid,
            campaign=campaign,
            state=DealState.READY_TO_CONNECT,
        ).select_related("lead").first()
        if not deal_target:
            deal_target = Deal.objects.filter(
                lead__public_identifier=target_pid,
                campaign=campaign,
            ).select_related("lead").first()
        if deal_target:
            candidate = _deal_to_profile_dict(deal_target)
        else:
            logger.warning("[%s] connect: target PID %s not in current campaign — failing closed", campaign, target_pid)
            return

    if candidate is None:
        candidate = strategy.find_candidate(session)

    if candidate is None:
        from openoutreach.crm.models import Deal
        _qualified = Deal.objects.filter(campaign=campaign, state=DealState.QUALIFIED).count()
        _ready = Deal.objects.filter(campaign=campaign, state=DealState.READY_TO_CONNECT).count()
        logger.info(
            "[%s] connect: no candidate available — slot skipped (qualified=%d, ready=%d)",
            campaign, _qualified, _ready,
        )
        return

    public_id = candidate["public_identifier"]
    profile = candidate.get("profile") or candidate

    from openoutreach.crm.models import Deal

    deal = Deal.objects.filter(
        lead__public_identifier=public_id,
        campaign=session.campaign,
    ).first()

    if deal and hasattr(deal, "campaign") and getattr(deal.campaign, "linkedin_profile", None):
        if deal.campaign.linkedin_profile != session_profile:
            logger.error("[%s] connect: deal campaign profile (%s) mismatch with session profile (%s) — failing closed", campaign, deal.campaign.linkedin_profile, session_profile)
            return

    if deal and deal.state in (DealState.PENDING, DealState.CONNECTED, DealState.COMPLETED, DealState.FAILED):
        logger.info("[%s] connect: candidate %s already in state %s — skipping duplicate task", campaign, public_id, deal.state)
        return

    # Check Account Safety Governor before attempting live provider interaction
    from openoutreach.linkedin.services.account_governor import (
        is_account_safe_for_provider_write,
        account_provider_lock,
    )
    from openoutreach.linkedin.services.evidence_cache import record_provider_read
    from openoutreach.linkedin.models import ProviderOperationType

    is_safe, safety_reason = is_account_safe_for_provider_write(
        workspace=getattr(campaign, "workspace", None),
        linkedin_profile=session.linkedin_profile,
        action_type="connect",
        campaign=campaign,
        task=task,
    )
    if not is_safe and not task.payload.get("mock_boundary"):
        logger.warning("[%s] connect task blocked by account safety governor: %s", campaign, safety_reason)
        return

    # Check QA synthetic transport guard (requires all 5 safety conditions)
    from openoutreach.linkedin.transport_guard import is_qa_synthetic_transport_allowed
    is_qa_transport = is_qa_synthetic_transport_allowed(
        campaign=campaign,
        workspace=getattr(campaign, "workspace", None),
        linkedin_profile=session.linkedin_profile,
        task=task,
    )

    if is_qa_transport:
        logger.info("QA SYNTHETIC BOUNDARY: Executing mock connection send for candidate %s", public_id)
        set_profile_state(session, public_id, DealState.PENDING.value)
        target_name = ""
        if candidate and isinstance(candidate, dict):
            p = candidate.get("profile") or candidate
            if isinstance(p, dict) and p.get("first_name"):
                target_name = f"{p.get('first_name')} {p.get('last_name', '')}".strip()
        if not target_name and public_id:
            target_name = public_id.replace("-", " ").title()
        session.linkedin_profile.record_action(
            ActionLog.ActionType.CONNECT, session.campaign,
            metadata={"note": "QA synthetic connect", "lead_public_id": public_id, "target_name": target_name, "note_was_sent": False},
        )
        return

    reason = deal.reason if deal else ""
    stats = strategy.qualifier.explain(candidate, session) if strategy.qualifier else ""
    logger.info("[%s] %s", campaign, colored("▶ connect", "cyan", attrs=["bold"]))
    logger.info("[%s] %s (%s) — %s", campaign, public_id, stats, reason or "")

    # ── Resolve profile data for note generation ─────────────────
    # Use candidate["profile"] first; only live-scrape if missing.
    note_profile = candidate.get("profile") or {}
    if not note_profile.get("first_name"):
        # Profile data not available from the pipeline — try Lead scrape.
        # The profile page is already loaded by get_connection_status below,
        # so this is a Voyager API call, not an extra page navigation.
        try:
            from openoutreach.crm.models import Lead
            lead_obj = Lead.objects.filter(public_identifier=public_id).first()
            if lead_obj:
                scraped = lead_obj.get_profile(session)
                if scraped:
                    note_profile = scraped
        except Exception:
            logger.debug("Could not scrape profile for note — using available data")

    # ── Resolve single-source connection note ────────────────────
    from openoutreach.crm.models import Deal
    deal = Deal.objects.filter(lead__public_identifier=public_id, campaign=campaign).first()

    note_text = ""
    note_source = "disabled"

    if deal and deal.generated_connection_note and deal.generated_connection_note.strip():
        note_text = deal.generated_connection_note.strip()
        note_source = "persisted"
    elif campaign.connection_note_enabled:
        note_text, note_source = generate_connection_note(session, campaign, note_profile)
        if deal and note_text:
            deal.generated_connection_note = note_text
            deal.save(update_fields=["generated_connection_note"])

    if note_text:
        from openoutreach.core.content_style import validate_linkedin_outbound_message
        outbound_res = validate_linkedin_outbound_message(
            message_text=note_text,
            content_type="connection_note",
            workspace=getattr(campaign, "workspace", None),
            linkedin_profile=session.linkedin_profile,
            prospect_facts=note_profile,
            deal=deal,
        )
        if not outbound_res["allowed"]:
            logger.warning(
                "[%s] connect note failed canonical validation (reason: %s, errors: %s) — suppressing note from request",
                campaign, outbound_res["reason"], outbound_res["errors"],
            )
            note_text = ""
            note_source = "blocked_style"

    note_was_sent = bool(note_text)
    note_metadata = {
        "note": note_text,
        "note_source": note_source,
        "note_length": len(note_text),
        "note_enabled": campaign.connection_note_enabled,
        "note_was_sent": note_was_sent,
        "lead_public_id": public_id,
    }
    logger.info(
        "[%s] note: source=%s length=%d enabled=%s",
        campaign, note_source, len(note_text), campaign.connection_note_enabled,
    )

    # Claim one-time operator approval if present
    from django.db import models as db_models
    from openoutreach.core.models import OperatorActionApproval
    ws = getattr(campaign, "workspace", None)
    approval_qs = OperatorActionApproval.objects.filter(
        workspace=ws,
        action_type="connect",
        status="approved",
    ).filter(
        db_models.Q(linkedin_profile=session_profile) |
        db_models.Q(linkedin_profile__isnull=True, operator=session_profile.user)
    )
    if deal and deal.lead_id:
        approval_qs = approval_qs.filter(db_models.Q(lead=deal.lead) | db_models.Q(lead__isnull=True))
    if deal and deal.id:
        approval_qs = approval_qs.filter(db_models.Q(deal=deal) | db_models.Q(deal__isnull=True))
    approval = approval_qs.first()
    if approval:
        if not approval.is_valid_for(
            workspace=ws,
            linkedin_profile=session_profile,
            lead=deal.lead if deal else None,
            deal=deal,
            action_type="connect",
        ):
            approval = None
        else:
            approval.status = "claimed"
            approval.claimed_at = timezone.now()
            approval.save(update_fields=["status", "claimed_at"])

    # ── Bounded Production & Backlog Safety Gate ─────────────────
    from openoutreach.core.models import WorkspaceAutomationSettings
    auto_settings = WorkspaceAutomationSettings.objects.filter(workspace=ws).first() if ws else None
    if not auto_settings and not approval:
        logger.warning("[%s] connect: automation settings unresolved for workspace — failing closed without provider action", campaign)
        return

    if auto_settings:
        # 1. Channel Switch Gate
        if not auto_settings.auto_send_connections and not approval:
            logger.warning("[%s] connect: connections channel is PAUSED in automation settings", campaign)
            return

        # 2. Backlog Governance Gate
        from openoutreach.crm.models import Deal
        crm_pending_count = Deal.objects.filter(campaign__workspace=auto_settings.workspace, state="Pending").count()
        provider_pending_count = auto_settings.linkedin_pending_invitations_count or 0
        effective_backlog = max(crm_pending_count, provider_pending_count)
        if effective_backlog >= auto_settings.max_pending_invitations:
            auto_settings.backlog_pause_reason = f"pending_invitation_backlog_exceeded ({effective_backlog} >= {auto_settings.max_pending_invitations})"
            auto_settings.save(update_fields=["backlog_pause_reason", "updated_at"])
            if auto_settings.connection_backlog_behavior == "PAUSE_NEW_CONNECTIONS" and not auto_settings.connection_backlog_operator_override and not approval:
                logger.warning("[%s] connect: %s — connection creation PAUSED", campaign, auto_settings.backlog_pause_reason)
                return
            elif auto_settings.connection_backlog_operator_override:
                logger.info("[%s] connect: Backlog exceeded (%d >= %d) but OPERATOR OVERRIDE is ACTIVE — proceeding with bounded connection", campaign, effective_backlog, auto_settings.max_pending_invitations)

        # 3. Daily Limit Gate
        if auto_settings.max_daily_connections > 0 and not approval:
            from openoutreach.core.services.quota_service import get_daily_connection_provider_attempt_count
            sent_today = get_daily_connection_provider_attempt_count(
                linkedin_profile=session.linkedin_profile,
            )
            if sent_today >= auto_settings.max_daily_connections:
                logger.warning("[%s] connect: daily limit reached (%d >= %d) — connection paused", campaign, sent_today, auto_settings.max_daily_connections)
                return

    # ── Durable Pre-Provider Idempotency Lock ───────────────────
    from openoutreach.linkedin.models import ConnectionAttempt
    from openoutreach.linkedin.services.reconciliation import (
        finalize_external_connection_success,
        handle_external_connection_uncertainty,
    )

    idempotency_key = f"connect_{session.linkedin_profile.id}_{public_id}"
    conn_attempt = ConnectionAttempt.objects.filter(idempotency_key=idempotency_key).first()

    if conn_attempt and conn_attempt.status in [
        "externally_confirmed",
        "attempting",
        "uncertain",
        "reconciliation_required",
    ]:
        if conn_attempt.status == "attempting" and conn_attempt.updated_at and (timezone.now() - conn_attempt.updated_at).total_seconds() > 900:
            conn_attempt.status = "failed_before_provider"
            conn_attempt.error_message = "Previous connection attempt timed out"
            conn_attempt.save(update_fields=["status", "error_message", "updated_at"])
        else:
            logger.warning(
                "[%s] connect: %s is already locked with status %s — skipping duplicate provider write",
                campaign, public_id, conn_attempt.status,
            )
            return

    # Durable ProviderWriteIntent Claim
    from openoutreach.linkedin.models import ProviderWriteIntent
    from openoutreach.linkedin.services.idempotency import (
        claim_provider_write,
        record_provider_write_attempt,
        confirm_provider_write,
        fail_provider_write,
    )
    allowed, write_reason, write_intent = claim_provider_write(
        workspace=ws,
        linkedin_profile=session.linkedin_profile,
        operation_type=ProviderWriteIntent.OperationType.CONNECTION_SEND,
        target_identity=public_id,
        logical_action_identity=f"camp_{campaign.id}_conn",
        custom_idempotency_key=idempotency_key,
    )
    if not allowed:
        logger.warning(
            "[%s] connect: %s blocked by durable provider write intent (%s)",
            campaign, public_id, write_reason,
        )
        return

    if not conn_attempt:
        conn_attempt = ConnectionAttempt.objects.create(
            linkedin_profile=session.linkedin_profile,
            recipient_public_id=public_id,
            idempotency_key=idempotency_key,
            status="attempting",
            note_text=note_text,
        )
    else:
        conn_attempt.status = "attempting"
        conn_attempt.note_text = note_text
        conn_attempt.save(update_fields=["status", "note_text", "updated_at"])

    try:
        if auto_settings:
            auto_settings.last_connection_attempt_at = timezone.now()
            auto_settings.save(update_fields=["last_connection_attempt_at", "updated_at"])

        # Check QA synthetic transport guard (requires all 5 safety conditions)
        from openoutreach.linkedin.transport_guard import is_qa_synthetic_transport_allowed
        is_qa_transport = is_qa_synthetic_transport_allowed(
            campaign=campaign,
            workspace=getattr(campaign, "workspace", None),
            linkedin_profile=session.linkedin_profile,
            task=task,
        )

        if is_qa_transport:
            logger.info("QA SYNTHETIC BOUNDARY: Executing mock connection send for candidate %s", public_id)
            new_state = DealState.PENDING
        else:
            status = DealState(get_connection_status(session, profile).value)

            if status in (DealState.CONNECTED, DealState.PENDING):
                finalize_external_connection_success(
                    connection_attempt=conn_attempt,
                    campaign=campaign,
                    new_state=status,
                    note_metadata=note_metadata,
                    approval=approval,
                )
                set_profile_state(session, public_id, status.value)
                return

            with account_provider_lock(linkedin_profile=session.linkedin_profile, workspace=ws, operation_name="connect"):
                try:
                    record_provider_write_attempt(write_intent)
                    new_state = DealState(
                        send_connection_request_with_note(
                            session=session, profile=profile, note=note_text,
                        ).value
                    )
                    confirm_provider_write(write_intent, confirmation_data={"new_state": new_state.value})
                    record_provider_read(
                        operation_type=ProviderOperationType.CONNECTION_UI,
                        resource_identifier=public_id,
                        cache_hit=False,
                        linkedin_profile=session.linkedin_profile,
                        workspace=getattr(campaign, "workspace", None),
                        success=True,
                        read_cost=1,
                    )
                except ConnectionNoteUIError as exc:
                    fail_provider_write(write_intent, str(exc), before_provider=False)
                    record_provider_read(
                        operation_type=ProviderOperationType.CONNECTION_UI,
                        resource_identifier=public_id,
                        cache_hit=False,
                        linkedin_profile=session.linkedin_profile,
                        workspace=getattr(campaign, "workspace", None),
                        success=False,
                        read_cost=1,
                    )
                    conn_attempt.status = "failed_before_provider"
                    conn_attempt.error_message = str(exc)
                    conn_attempt.save(update_fields=["status", "error_message", "updated_at"])
                    if approval and approval.status == "claimed":
                        approval.status = "failed"
                        approval.failure_reason = str(exc)
                        approval.save(update_fields=["status", "failure_reason"])

                    failed_metadata = {
                        "note": note_text,
                        "note_source": note_source,
                        "note_length": len(note_text),
                        "note_enabled": campaign.connection_note_enabled,
                        "note_was_sent": False,
                        "note_required": True,
                        "failure_reason": str(exc),
                        "lead_public_id": public_id,
                    }
                    session.linkedin_profile.record_action(
                        ActionLog.ActionType.CONNECT, session.campaign,
                        metadata=failed_metadata,
                    )
                    logger.error("Connection note required but not sent for %s: %s", public_id, exc)
                    attempts = increment_connect_attempts(session, public_id)
                    if attempts >= MAX_CONNECT_ATTEMPTS:
                        reason = f"Note UI unavailable ({exc})"
                        disqualify_lead(public_id)
                        set_profile_state(session, public_id, DealState.FAILED.value, reason=reason)
                    raise exc

        if new_state == DealState.QUALIFIED:
            # No Connect button found — track attempt, disqualify after MAX_CONNECT_ATTEMPTS
            attempts = increment_connect_attempts(session, public_id)
            if attempts >= MAX_CONNECT_ATTEMPTS:
                reason = f"Unreachable: no Connect button after {attempts} attempts"
                disqualify_lead(public_id)
                set_profile_state(session, public_id, DealState.FAILED.value, reason=reason)
                conn_attempt.status = "failed_before_provider"
                conn_attempt.error_message = reason
                conn_attempt.save(update_fields=["status", "error_message", "updated_at"])
                logger.warning("Disqualified %s — %s", public_id, reason)
            else:
                set_profile_state(session, public_id, new_state.value)
                conn_attempt.status = "failed_before_provider"
                conn_attempt.save(update_fields=["status", "updated_at"])
                logger.debug("%s: connect attempt %d/%d — no button found", public_id, attempts, MAX_CONNECT_ATTEMPTS)
        else:
            set_profile_state(session, public_id, new_state.value)
            finalize_external_connection_success(
                connection_attempt=conn_attempt,
                campaign=campaign,
                new_state=new_state,
                note_metadata=note_metadata,
                approval=approval,
            )

    except ReachedConnectionLimit as e:
        logger.warning("Rate limited: %s", e)
        conn_attempt.status = "failed_before_provider"
        conn_attempt.error_message = str(e)
        conn_attempt.save(update_fields=["status", "error_message", "updated_at"])
        if approval and approval.status == "claimed":
            approval.status = "failed"
            approval.failure_reason = str(e)
            approval.save(update_fields=["status", "failure_reason"])
        session.linkedin_profile.mark_exhausted(ActionLog.ActionType.CONNECT)
    except ProfileInaccessibleError as e:
        logger.warning("Profile inaccessible — marking FAILED: %s", e)
        conn_attempt.status = "failed_before_provider"
        conn_attempt.error_message = str(e)
        conn_attempt.save(update_fields=["status", "error_message", "updated_at"])
        if approval and approval.status == "claimed":
            approval.status = "failed"
            approval.failure_reason = str(e)
            approval.save(update_fields=["status", "failure_reason"])
        set_profile_state(session, public_id, DealState.FAILED.value,
                          reason=f"Profile inaccessible: {e}")
    except SkipProfile as e:
        logger.warning("Skipping %s: %s", public_id, e)
        conn_attempt.status = "failed_before_provider"
        conn_attempt.error_message = str(e)
        conn_attempt.save(update_fields=["status", "error_message", "updated_at"])
        if approval and approval.status == "claimed":
            approval.status = "failed"
            approval.failure_reason = str(e)
            approval.save(update_fields=["status", "failure_reason"])
        set_profile_state(session, public_id, DealState.FAILED.value)
    except ConnectionNoteUIError:
        raise
    except Exception as e:
        logger.exception("Unexpected error during connection execution for %s: %s", public_id, e)
        handle_external_connection_uncertainty(
            connection_attempt=conn_attempt,
            error_message=str(e),
            approval=approval,
        )


