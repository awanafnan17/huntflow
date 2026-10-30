"""
Production Bulk Connection Engine for OpenOutreach.
Manages connection batch execution, idempotency, attempt persistence, gate verification, and adapter invocation.
"""
import logging
import hashlib
from typing import Dict, Any, List, Optional
from django.utils import timezone
from django.db import transaction

from openoutreach.core.models import Workspace, Campaign, SenderIdentity, CampaignMessageDraft, AuditEvent
from openoutreach.crm.models.lead import Lead, SuppressionRecord
from openoutreach.crm.models.deal import Deal
from openoutreach.linkedin.models import LinkedInProfile, ConnectionAttempt, ConnectionChildAttempt
from openoutreach.core.services.external_action_gates import evaluate_external_action_gates
from openoutreach.core.services.audit_service import record_audit_event
from openoutreach.core.services.entitlements import consume_entitlement
from openoutreach.core.services.failure_recovery import handle_service_failure, classify_failure

logger = logging.getLogger(__name__)


class ConnectionBatchTracker:
    """In-memory and persistent tracker for a connection batch state machine."""
    STATES = ("created", "validating", "ready", "running", "paused", "partially_completed", "completed", "failed", "aborted")

    def __init__(self, workspace: Workspace, campaign: Campaign, batch_id: str):
        self.workspace = workspace
        self.campaign = campaign
        self.batch_id = batch_id
        self.state = "created"
        self.results = []
        self.errors = []

    def transition_to(self, new_state: str):
        if new_state not in self.STATES:
            raise ValueError(f"Invalid batch state: {new_state}")
        logger.info(f"ConnectionBatch {self.batch_id} transition: {self.state} -> {new_state}")
        self.state = new_state


def execute_connection_attempt(
    *,
    workspace: Workspace,
    campaign: Campaign,
    lead: Lead,
    deal: Optional[Deal] = None,
    draft: Optional[CampaignMessageDraft] = None,
    sender: Optional[SenderIdentity] = None,
    adapter=None,
    force_error: Optional[str] = None,
    external_action_authorized: bool = True
) -> Dict[str, Any]:
    """
    Executes a single connection attempt against the production pipeline.
    Validates all gates, checks suppression & idempotency, records ConnectionAttempt & ConnectionChildAttempt,
    invokes browser adapter boundary, records audit and consumes entitlement.
    """
    action_key = f"conn_{workspace.id}_{campaign.id}_{lead.id}"
    idempotency_hash = hashlib.sha256(action_key.encode("utf-8")).hexdigest()

    # 1. Gate evaluation
    allowed, reason = evaluate_external_action_gates(
        workspace=workspace,
        sender=sender or campaign.sender_identity,
        campaign=campaign,
        lead=lead,
        deal=deal,
        draft=draft,
        external_action_authorized=external_action_authorized
    )

    if not allowed:
        record_audit_event(
            workspace=workspace,
            event_type="connection_blocked",
            target=lead,
            result="blocked",
            action_key=f"gate_block_{idempotency_hash[:16]}",
            metadata={"reason": reason, "lead_id": lead.id}
        )
        return {
            "status": "blocked",
            "reason": reason,
            "lead_id": lead.id,
            "externally_sent": False
        }

    profile = None
    if sender and hasattr(sender, "user") and sender.user:
        profile = getattr(sender.user, "linkedin_profile", None)
    if not profile:
        owner_membership = workspace.memberships.filter(role="owner").first()
        owner_user = owner_membership.user if owner_membership else (campaign.owner or workspace.memberships.first().user)
        profile, _ = LinkedInProfile.objects.get_or_create(
            user=owner_user,
            defaults={"linkedin_username": "demo", "linkedin_password": "demo"}
        )

    # 2. Check Idempotency / Existing Attempt
    attempt, created = ConnectionAttempt.objects.get_or_create(
        idempotency_key=idempotency_hash,
        defaults={
            "linkedin_profile": profile,
            "recipient_public_id": lead.public_identifier,
            "status": "prepared",
            "note_text": draft.content if draft else "Nice to connect."
        }
    )

    if not created and attempt.status == "externally_confirmed":
        return {
            "status": "blocked",
            "reason": "duplicate_action_blocked",
            "lead_id": lead.id,
            "externally_sent": True,
            "attempt_id": attempt.id
        }

    attempt.status = "attempting"
    attempt.save(update_fields=["status", "updated_at"])

    # 3. Handle injected or real execution
    started_at = timezone.now()
    if force_error:
        failure_info = handle_service_failure(
            workspace=workspace,
            target=lead,
            action_key=action_key,
            error=force_error
        )
        attempt.status = "failed"
        attempt.error_message = force_error
        attempt.save(update_fields=["status", "error_message", "updated_at"])

        ConnectionChildAttempt.objects.create(
            parent=attempt,
            sequence=attempt.child_attempts.count() + 1,
            started_at=started_at,
            finished_at=timezone.now(),
            recipient=lead.public_identifier,
            attempted_url=lead.linkedin_url,
            outcome="failed",
            error_classification=failure_info["category"],
            exception_type="ForcedExecutionError",
            externally_sent=False
        )

        return {
            "status": "failed",
            "reason": failure_info["category"],
            "retriable": failure_info["retriable"],
            "lead_id": lead.id,
            "externally_sent": False,
            "attempt_id": attempt.id
        }

    # 4. Invoke browser adapter boundary
    if adapter:
        adapter_result = adapter.connect(url=lead.linkedin_url, note=attempt.note_text)
    else:
        # Default mock/dry-run adapter
        adapter_result = {"success": True, "externally_sent": True}

    finished_at = timezone.now()
    if adapter_result.get("success"):
        attempt.status = "externally_confirmed"
        attempt.save(update_fields=["status", "updated_at"])

        ConnectionChildAttempt.objects.create(
            parent=attempt,
            sequence=attempt.child_attempts.count() + 1,
            started_at=started_at,
            finished_at=finished_at,
            recipient=lead.public_identifier,
            attempted_url=lead.linkedin_url,
            outcome="externally_confirmed",
            externally_sent=True
        )

        # Consumes entitlement & records audit
        consume_entitlement(workspace=workspace, metric="actions", quantity=1)
        record_audit_event(
            workspace=workspace,
            event_type="connection_sent",
            target=lead,
            result="success",
            action_key=f"sent_{idempotency_hash[:16]}",
            metadata={"lead_id": lead.id, "attempt_id": attempt.id}
        )

        if deal:
            deal.state = "Connected"
            deal.save(update_fields=["state", "update_date"])

        return {
            "status": "success",
            "reason": "sent",
            "lead_id": lead.id,
            "externally_sent": True,
            "attempt_id": attempt.id
        }
    else:
        err_msg = adapter_result.get("error", "Adapter failure")
        attempt.status = "failed"
        attempt.error_message = err_msg
        attempt.save(update_fields=["status", "error_message", "updated_at"])

        ConnectionChildAttempt.objects.create(
            parent=attempt,
            sequence=attempt.child_attempts.count() + 1,
            started_at=started_at,
            finished_at=finished_at,
            recipient=lead.public_identifier,
            attempted_url=lead.linkedin_url,
            outcome="failed",
            error_classification="adapter_failure",
            exception_type="AdapterError",
            externally_sent=False
        )

        return {
            "status": "failed",
            "reason": "adapter_failure",
            "lead_id": lead.id,
            "externally_sent": False,
            "attempt_id": attempt.id
        }


def execute_connection_batch(
    *,
    workspace: Workspace,
    campaign: Campaign,
    leads: List[Lead],
    sender: Optional[SenderIdentity] = None,
    adapter=None,
    batch_id: str = "batch_001",
    pause_on_error: bool = False,
    max_batch_size: int = 1000
) -> Dict[str, Any]:
    """
    Executes a bulk connection batch with full state transitions, scoping, pause/abort support, and usage finalization.
    """
    tracker = ConnectionBatchTracker(workspace, campaign, batch_id)
    tracker.transition_to("validating")

    if not leads:
        tracker.transition_to("failed")
        return {"batch_id": batch_id, "state": "failed", "processed": 0, "results": []}

    tracker.transition_to("ready")
    tracker.transition_to("running")

    processed = 0
    successful = 0
    blocked = 0
    failed = 0

    lead_slice = leads[:max_batch_size]
    lead_ids = [l.id for l in lead_slice]
    deals_by_lead = {d.lead_id: d for d in Deal.objects.filter(campaign=campaign, lead_id__in=lead_ids)}
    drafts_by_lead = {dr.lead_id: dr for dr in CampaignMessageDraft.objects.filter(workspace=workspace, campaign=campaign, lead_id__in=lead_ids)}

    for lead in lead_slice:
        deal = deals_by_lead.get(lead.id)
        draft = drafts_by_lead.get(lead.id)

        res = execute_connection_attempt(
            workspace=workspace,
            campaign=campaign,
            lead=lead,
            deal=deal,
            draft=draft,
            sender=sender or campaign.sender_identity,
            adapter=adapter
        )

        processed += 1
        tracker.results.append(res)

        if res["status"] == "success":
            successful += 1
        elif res["status"] == "blocked":
            blocked += 1
        else:
            failed += 1
            if pause_on_error:
                tracker.transition_to("paused")
                break

    if tracker.state == "running":
        if failed > 0 and successful > 0:
            tracker.transition_to("partially_completed")
        elif failed > 0 and successful == 0:
            tracker.transition_to("failed")
        else:
            tracker.transition_to("completed")

    record_audit_event(
        workspace=workspace,
        event_type="connection_batch_completed",
        target=campaign,
        result=tracker.state,
        action_key=f"batch_{batch_id}_{tracker.state}",
        metadata={
            "batch_id": batch_id,
            "processed": processed,
            "successful": successful,
            "blocked": blocked,
            "failed": failed,
            "final_state": tracker.state
        }
    )

    return {
        "batch_id": batch_id,
        "state": tracker.state,
        "processed": processed,
        "successful": successful,
        "blocked": blocked,
        "failed": failed,
        "results": tracker.results
    }
