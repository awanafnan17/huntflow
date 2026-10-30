# openoutreach/linkedin/tasks/check_pending.py
"""Check pending task — re-checks one due PENDING deal in the campaign.

Lazy: the slot carries only ``campaign_id``. The handler picks the
oldest-due PENDING deal at execution time. If the recheck leaves the
deal in PENDING, the backoff is doubled and ``next_check_pending_at``
re-stamped via the ``on_deal_state_entered`` hook.
"""
from __future__ import annotations

import logging

from django.utils import timezone
from termcolor import colored

from openoutreach.core.db.deals import set_profile_state
from openoutreach.crm.models import DealState
from linkedin_cli.exceptions import SkipProfile

logger = logging.getLogger(__name__)


def _next_due_pending_deal(campaign, profile=None):
    from django.db.models import Q
    from openoutreach.crm.models import Deal
    from openoutreach.linkedin.models import ProviderRelationship

    ws = getattr(campaign, "workspace", None)
    prof = profile or getattr(campaign, "linkedin_profile", None)
    now = timezone.now()
    pending_deals = (
        Deal.objects.filter(
            campaign=campaign,
            state=DealState.PENDING,
            next_check_pending_at__lte=now,
        )
        .select_related("lead", "campaign")
        .order_by("next_check_pending_at")
    )
    for deal in pending_deals:
        if prof:
            rel = ProviderRelationship.objects.filter(
                workspace=ws,
                linkedin_profile=prof,
                lead=deal.lead,
            ).first()
            if rel:
                if rel.operator_review_required:
                    continue
                if rel.next_eligible_reconciliation_at and now < rel.next_eligible_reconciliation_at:
                    continue
        return deal
    return None


def _double_backoff(deal) -> float:
    from openoutreach.core.conf import CAMPAIGN_CONFIG
    current = deal.backoff_hours or CAMPAIGN_CONFIG["check_pending_recheck_after_hours"]
    deal.backoff_hours = current * 2
    deal.save(update_fields=["backoff_hours"])
    return deal.backoff_hours


def handle_check_pending(task, session, qualifiers):
    from linkedin_cli.actions.status import get_connection_status
    from openoutreach.linkedin.models import ProviderOperationType
    from openoutreach.linkedin.services.account_governor import is_account_safe_for_provider_read
    from openoutreach.linkedin.services.evidence_cache import record_provider_read

    campaign = session.campaign
    ws = getattr(campaign, "workspace", None)
    profile = getattr(session, "linkedin_profile", None)

    if profile and profile.session_status == "checkpoint":
        logger.warning("[%s] check_pending: LinkedIn session is in CHECKPOINT_REQUIRED state — waiting on operator verification", campaign)
        return

    is_safe, safety_reason = is_account_safe_for_provider_read(
        workspace=ws,
        linkedin_profile=profile,
    )
    if not is_safe:
        logger.warning("[%s] check_pending blocked by governor: %s", campaign, safety_reason)
        return

    deal = _next_due_pending_deal(campaign, profile=profile)
    if ws:
        from openoutreach.core.models import WorkspaceAutomationSettings
        from django.utils import timezone
        auto_st = WorkspaceAutomationSettings.get_for_workspace(ws)
        if auto_st:
            now = timezone.now()
            auto_st.last_acceptance_poll_at = now
            auto_st.last_reply_poll_at = now
            auto_st.save(update_fields=["last_acceptance_poll_at", "last_reply_poll_at", "updated_at"])

    if deal is None:
        logger.info("[%s] check_pending: no due PENDING deals — slot skipped", campaign)
        return

    public_id = deal.lead.public_identifier
    logger.info(
        "[%s] %s %s",
        campaign, colored("▶ check_pending", "magenta", attrs=["bold"]), public_id,
    )

    profile_dict = deal.lead.to_profile_dict()
    profile_for_status = profile_dict.get("profile") or profile_dict

    try:
        # The library returns the observed UI state as a str; lift it into our enum.
        raw_status = get_connection_status(session, profile_for_status)
        new_state = DealState(raw_status.value)
        record_provider_read(
            operation_type=ProviderOperationType.PENDING_INVITATIONS,
            resource_identifier=public_id,
            cache_hit=False,
            linkedin_profile=profile,
            workspace=ws,
            success=True,
            read_cost=1,
        )
    except SkipProfile as e:
        logger.warning("Skipping %s: %s", public_id, e)
        set_profile_state(session, public_id, DealState.FAILED.value)
        return
    except Exception as e:
        record_provider_read(
            operation_type=ProviderOperationType.PENDING_INVITATIONS,
            resource_identifier=public_id,
            cache_hit=False,
            linkedin_profile=profile,
            workspace=ws,
            success=False,
            read_cost=1,
        )
        raise

    if new_state == DealState.PENDING:
        # Still pending — double the backoff before set_profile_state so the
        # state hook re-stamps next_check_pending_at with the doubled value.
        old = deal.backoff_hours or 0
        new = _double_backoff(deal)
        logger.info("%s still pending — backoff %.1fh → %.1fh", public_id, old, new)

    set_profile_state(session, public_id, new_state.value)
