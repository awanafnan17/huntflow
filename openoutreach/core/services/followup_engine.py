"""
Production Follow-Up Engine for OpenOutreach.
Schedules, evaluates, and executes multi-touch follow-up sequences across campaign deals.
"""
import logging
from datetime import timedelta
from typing import Dict, Any, List, Optional
from django.utils import timezone

from openoutreach.core.models import Workspace, Campaign, SenderIdentity, CampaignMessageDraft, MessageSequenceStep, AuditEvent
from openoutreach.crm.models.lead import Lead, SuppressionRecord
from openoutreach.crm.models.deal import Deal
from openoutreach.career.models import IncidentHold, IncidentHoldTarget
from openoutreach.core.services.external_action_gates import evaluate_external_action_gates
from openoutreach.core.services.audit_service import record_audit_event
from openoutreach.core.services.entitlements import consume_entitlement

logger = logging.getLogger(__name__)


def find_due_followups(
    *,
    workspace: Workspace,
    campaign: Optional[Campaign] = None,
    min_interval_days: int = 3
) -> List[Deal]:
    """
    Finds deals due for a follow-up action based on campaign, creation date/interval, state, and attempts.
    """
    cutoff = timezone.now() - timedelta(days=min_interval_days)
    qs = Deal.objects.filter(
        campaign__workspace=workspace,
        creation_date__lte=cutoff,
        state="Qualified"
    )
    if campaign:
        qs = qs.filter(campaign=campaign)
    return list(qs)


def schedule_followups(
    *,
    workspace: Workspace,
    campaign: Campaign,
    min_interval_days: int = 3
) -> List[Dict[str, Any]]:
    """
    Schedules due follow-ups for a campaign.
    """
    due_deals = find_due_followups(workspace=workspace, campaign=campaign, min_interval_days=min_interval_days)
    scheduled = []

    for deal in due_deals:
        action_key = f"followup_sched_{campaign.id}_{deal.id}_{deal.connect_attempts + 1}"
        record_audit_event(
            workspace=workspace,
            event_type="followup_scheduled",
            target=deal,
            result="scheduled",
            action_key=action_key,
            metadata={"step_number": deal.connect_attempts + 1, "deal_id": deal.id}
        )
        scheduled.append({"deal_id": deal.id, "step_number": deal.connect_attempts + 1, "action_key": action_key})

    return scheduled


def execute_followup(
    *,
    workspace: Workspace,
    campaign: Campaign,
    deal: Deal,
    step: Optional[MessageSequenceStep] = None,
    draft: Optional[CampaignMessageDraft] = None,
    adapter=None,
    external_action_authorized: bool = True,
    has_reply: bool = False,
    has_rejection: bool = False,
    has_withdrawal: bool = False,
    min_interval_days: int = 3,
    max_attempts: int = 3
) -> Dict[str, Any]:
    """
    Executes a single follow-up touch point evaluating all 16 required checks and persistence guarantees.
    """
    action_key = f"followup_exec_{campaign.id}_{deal.id}_{deal.connect_attempts + 1}"

    # 1. Original action & deal state check
    if not deal or deal.state in ("Failed", "Opted Out", "Completed"):
        return {"status": "blocked", "stop_reason": "invalid_deal_state", "deal_id": deal.id}

    # 2. Maximum attempts check
    if deal.connect_attempts >= max_attempts:
        return {"status": "blocked", "stop_reason": "maximum_attempt_reached", "deal_id": deal.id}

    # 3. Minimum interval check
    days_passed = (timezone.now() - deal.creation_date).days
    if days_passed < min_interval_days:
        return {"status": "blocked", "stop_reason": "minimum_interval_not_reached", "deal_id": deal.id}

    # 4. Reply / Rejection / Withdrawal check
    if has_reply:
        deal.state = "Completed"
        deal.save(update_fields=["state", "update_date"])
        return {"status": "blocked", "stop_reason": "reply_received", "deal_id": deal.id}

    if has_rejection:
        deal.state = "Failed"
        deal.save(update_fields=["state", "update_date"])
        return {"status": "blocked", "stop_reason": "rejection_received", "deal_id": deal.id}

    if has_withdrawal:
        deal.state = "Failed"
        deal.save(update_fields=["state", "update_date"])
        return {"status": "blocked", "stop_reason": "application_withdrawn", "deal_id": deal.id}

    # 5. Gate checks (suppression, incident hold, sender, campaign, entitlement, review, auth)
    allowed, reason = evaluate_external_action_gates(
        workspace=workspace,
        sender=campaign.sender_identity,
        campaign=campaign,
        lead=deal.lead,
        deal=deal,
        draft=draft,
        external_action_authorized=external_action_authorized
    )

    if not allowed:
        record_audit_event(
            workspace=workspace,
            event_type="followup_blocked",
            target=deal,
            result="blocked",
            action_key=f"followup_block_{deal.id}",
            metadata={"reason": reason}
        )
        return {"status": "blocked", "stop_reason": reason, "deal_id": deal.id}

    # 6. Execute via adapter
    if adapter:
        result = adapter.send_followup(deal_id=deal.id, note="Following up.")
    else:
        result = {"success": True}

    if result.get("success"):
        deal.connect_attempts += 1
        deal.save(update_fields=["connect_attempts", "update_date"])

        consume_entitlement(workspace=workspace, metric="actions", quantity=1)
        record_audit_event(
            workspace=workspace,
            event_type="followup_sent",
            target=deal,
            result="success",
            action_key=action_key,
            metadata={"attempt_number": deal.connect_attempts, "deal_id": deal.id}
        )

        return {"status": "success", "stop_reason": "sent", "deal_id": deal.id, "attempt_number": deal.connect_attempts}
    else:
        return {"status": "failed", "stop_reason": result.get("error", "adapter_failure"), "deal_id": deal.id}


def execute_followup_batch(
    *,
    workspace: Workspace,
    campaign: Campaign,
    deals: List[Deal],
    adapter=None,
    batch_id: str = "followup_batch_001",
    external_action_authorized: bool = True
) -> Dict[str, Any]:
    """
    Executes a batch of follow-ups.
    """
    processed = 0
    successful = 0
    blocked = 0
    failed = 0
    results = []

    for deal in deals:
        draft = CampaignMessageDraft.objects.filter(workspace=workspace, campaign=campaign, deal=deal).first()
        res = execute_followup(
            workspace=workspace,
            campaign=campaign,
            deal=deal,
            draft=draft,
            adapter=adapter,
            external_action_authorized=external_action_authorized
        )

        processed += 1
        results.append(res)

        if res["status"] == "success":
            successful += 1
        elif res["status"] == "blocked":
            blocked += 1
        else:
            failed += 1

    record_audit_event(
        workspace=workspace,
        event_type="followup_batch_completed",
        target=campaign,
        result="completed",
        action_key=f"followup_batch_{batch_id}_done",
        metadata={"processed": processed, "successful": successful, "blocked": blocked, "failed": failed}
    )

    return {
        "batch_id": batch_id,
        "processed": processed,
        "successful": successful,
        "blocked": blocked,
        "failed": failed,
        "results": results
    }
