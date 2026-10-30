# openoutreach/linkedin/services/reconciliation.py
"""
Service for reconciling LinkedIn provider writes and connection attempts.
Provides durable, automated recovery when provider calls succeed or result in uncertainty
without requiring manual shell intervention.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from django.utils import timezone
from openoutreach.crm.models import Deal, DealState
from openoutreach.linkedin.models import ConnectionAttempt, LinkedInProfile, ActionLog
from openoutreach.core.services.audit_service import record_audit_event

logger = logging.getLogger(__name__)


def finalize_external_connection_success(
    connection_attempt: ConnectionAttempt,
    campaign,
    new_state: DealState,
    note_metadata: Optional[Dict[str, Any]] = None,
    approval=None,
) -> None:
    """
    Durable point-of-no-return finalizer for successful external connection requests.
    Guarantees that once the external provider operation succeeds, failure in local
    logging or secondary database models CANNOT rollback or invalidate the external truth.
    """
    public_id = connection_attempt.recipient_public_id
    
    # 1. Update ConnectionAttempt to externally_confirmed
    connection_attempt.status = "externally_confirmed"
    connection_attempt.error_message = ""
    connection_attempt.save(update_fields=["status", "error_message", "updated_at"])

    # 1b. Confirm ProviderWriteIntent if exists
    try:
        from openoutreach.linkedin.models import ProviderWriteIntent
        from openoutreach.linkedin.services.idempotency import confirm_provider_write
        intents = ProviderWriteIntent.objects.filter(
            target_identity=public_id,
            operation_type=ProviderWriteIntent.OperationType.CONNECTION_SEND,
        ).exclude(state=ProviderWriteIntent.State.CONFIRMED)
        for intent in intents:
            confirm_provider_write(intent, confirmation_data={"new_state": getattr(new_state, "value", str(new_state))})
    except Exception as e:
        logger.error("Failed to confirm ProviderWriteIntent for %s: %s", public_id, e)

    # 2. Update Deal state
    try:
        from openoutreach.core.db.deals import set_profile_state
        deal = Deal.objects.filter(lead__public_identifier=public_id, campaign=campaign).first()
        if deal:
            deal.state = new_state.value if isinstance(new_state, DealState) else str(new_state)
            deal.save(update_fields=["state", "update_date"])
    except Exception as e:
        logger.error("Failed to update Deal state for %s after external connection success: %s", public_id, e)

    # 3. Consume OperatorActionApproval if present
    if approval:
        try:
            approval.status = "consumed"
            approval.consumed_at = timezone.now()
            approval.save(update_fields=["status", "consumed_at"])
        except Exception as e:
            logger.error("Failed to mark approval consumed for %s: %s", public_id, e)

    # 4. Record ActionLog via record_action (best-effort, local metadata only)
    try:
        if hasattr(connection_attempt, "linkedin_profile") and connection_attempt.linkedin_profile:
            connection_attempt.linkedin_profile.record_action(
                ActionLog.ActionType.CONNECT,
                campaign,
                metadata=note_metadata or {"recipient": public_id, "status": "externally_confirmed"},
            )
        else:
            ActionLog.objects.create(
                linkedin_profile=connection_attempt.linkedin_profile,
                campaign=campaign,
                action_type=ActionLog.ActionType.CONNECT,
                metadata=note_metadata or {"recipient": public_id, "status": "externally_confirmed"},
            )
    except Exception as e:
        logger.error("Failed to create ActionLog for %s (external truth preserved): %s", public_id, e)

    # 5. Record Audit Event
    try:
        record_audit_event(
            workspace=getattr(campaign, "workspace", None),
            event_type="linkedin_connection_sent",
            target=campaign,
            result="externally_confirmed",
            action_key=f"connect_{public_id}",
            metadata={"recipient": public_id, "status": "externally_confirmed"},
        )
    except Exception as e:
        logger.error("Failed to record audit event for %s (external truth preserved): %s", public_id, e)


def handle_external_connection_uncertainty(
    connection_attempt: ConnectionAttempt,
    error_message: str,
    approval=None,
) -> None:
    """
    Handles provider uncertainty (timeouts, dropped connections post-submit).
    Prevents blind retry and preserves approval non-reusability.
    """
    connection_attempt.status = "uncertain"
    connection_attempt.error_message = error_message
    connection_attempt.save(update_fields=["status", "error_message", "updated_at"])

    if approval and approval.status == "claimed":
        approval.failure_reason = f"Provider uncertainty: {error_message}"
        approval.save(update_fields=["failure_reason"])


def reconcile_connection_attempt(
    connection_attempt: ConnectionAttempt,
    session=None,
) -> Dict[str, Any]:
    """
    Read-only provider reconciliation for uncertain or pending connection attempts.
    """
    public_id = connection_attempt.recipient_public_id
    profile_dict = {"public_identifier": public_id}

    if session is None:
        from openoutreach.linkedin.browser.session import AccountSession
        session = AccountSession(connection_attempt.linkedin_profile)

    try:
        from linkedin_cli.actions.status import get_connection_status
        status_enum = get_connection_status(session, profile_dict)
        observed_status = status_enum.value

        if observed_status in [DealState.PENDING.value, DealState.CONNECTED.value, "Qualified"]:
            connection_attempt.status = "externally_confirmed"
            connection_attempt.save(update_fields=["status", "updated_at"])
            return {"status": "reconciled_confirmed", "observed": observed_status}
        elif observed_status in [DealState.QUALIFIED.value, "not_connected"]:
            connection_attempt.status = "failed_before_provider"
            connection_attempt.save(update_fields=["status", "updated_at"])
            return {"status": "reconciled_not_sent", "observed": observed_status}
        else:
            return {"status": "indeterminate", "observed": observed_status}
    except Exception as e:
        logger.warning("Reconciliation query failed for %s: %s", public_id, e)
        return {"status": "reconciliation_failed", "error": str(e)}
