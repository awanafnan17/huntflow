# openoutreach/linkedin/services/manual_connection.py
"""
Canonical Manual Operator-Approved Connection Execution Service for HuntFlow SaaS.

Executes a single operator-approved connection request under strict fail-closed invariants:
1. Workspace and tenant scoping
2. Exact operator authorization & OperatorActionApproval validation
3. Atomic OperatorActionApproval claiming (select_for_update)
4. Account safety gate (circuit breaker, safety stops, session status)
5. Daily quota and rate limit verification
6. Cross-process ProviderAccountLease concurrency serialization (acquired before write-intent)
7. Durable ProviderWriteIntent & Idempotency claim inside lease
8. Provider transport invocation (or mocked transport)
9. State reconciliation on success (confirms write intent, consumes approval, logs ActionLog)
10. Fail-closed error handling on timeout/uncertainty (marks UNCERTAIN, no automatic retry)
"""
from __future__ import annotations

import hashlib
import logging
from typing import Any, Callable, Dict, Optional, Tuple

from django.db import transaction
from django.utils import timezone

from openoutreach.core.models import Workspace, OperatorActionApproval
from openoutreach.crm.models import Deal, DealState
from openoutreach.linkedin.models import (
    LinkedInProfile,
    ProviderWriteIntent,
    ConnectionAttempt,
    ActionLog,
    ProviderOperationType,
)
from openoutreach.linkedin.services.account_governor import (
    is_account_safe_for_provider_write,
    account_provider_lock,
    resolve_provider_context,
)
from openoutreach.linkedin.services.idempotency import (
    claim_provider_write,
    record_provider_write_attempt,
    confirm_provider_write,
    fail_provider_write,
)
from openoutreach.linkedin.services.reconciliation import (
    finalize_external_connection_success,
    handle_external_connection_uncertainty,
)
from openoutreach.linkedin.services.evidence_cache import record_provider_read

logger = logging.getLogger(__name__)


def execute_manual_operator_connection(
    *,
    approval_id: int,
    workspace: Workspace,
    linkedin_profile: LinkedInProfile,
    deal: Deal,
    note_text: Optional[str] = None,
    session: Any = None,
    custom_transport: Optional[Callable] = None,
) -> Dict[str, Any]:
    """
    Executes a single operator-approved connection request through all canonical security gates.

    Returns dict with execution results:
    {
        "status": "success" | "blocked" | "uncertain" | "failed",
        "code": str,
        "detail": str,
        "write_intent_id": Optional[int],
        "approval_id": approval_id,
        "transport_invoked": bool,
    }
    """
    # ── 1. Tenant & Context Resolution ──────────────────────────────────
    resolved_ws, resolved_prof, err = resolve_provider_context(
        workspace=workspace,
        linkedin_profile=linkedin_profile,
        campaign=getattr(deal, "campaign", None),
    )
    if err or not resolved_ws or not resolved_prof:
        return {
            "status": "blocked",
            "code": err or "context_resolution_failed",
            "detail": "Failed to resolve valid workspace and provider account context",
            "write_intent_id": None,
            "approval_id": approval_id,
            "transport_invoked": False,
        }

    # ── 2. Note and Hash Resolution ─────────────────────────────────────
    effective_note = note_text if note_text is not None else (deal.generated_connection_note or "")
    normalized_note = effective_note.strip()
    note_hash = hashlib.sha256(normalized_note.encode("utf-8")).hexdigest()

    # ── 3. Atomic OperatorActionApproval Claim ──────────────────────────
    claim_ok, claimed_approval, claim_reason = OperatorActionApproval.claim_for_execution(
        approval_id=approval_id,
        workspace=resolved_ws,
        linkedin_profile=resolved_prof,
        lead=deal.lead,
        deal=deal,
        message_text=normalized_note,
        action_type="connect",
    )
    if not claim_ok or not claimed_approval:
        return {
            "status": "blocked",
            "code": f"approval_claim_failed_{claim_reason}",
            "detail": f"Failed to claim OperatorActionApproval #{approval_id}: {claim_reason}",
            "write_intent_id": None,
            "approval_id": approval_id,
            "transport_invoked": False,
        }

    # ── 4. Canonical Provider Safety Gate ───────────────────────────────
    is_safe, safety_reason = is_account_safe_for_provider_write(
        workspace=resolved_ws,
        linkedin_profile=resolved_prof,
        action_type="connect",
        campaign=getattr(deal, "campaign", None),
    )
    if not is_safe:
        # Revert claimed approval to approved if safety blocked before provider
        claimed_approval.status = "approved"
        claimed_approval.claimed_at = None
        claimed_approval.failure_reason = f"Blocked by provider safety: {safety_reason}"
        claimed_approval.save(update_fields=["status", "claimed_at", "failure_reason"])
        return {
            "status": "blocked",
            "code": f"safety_gate_{safety_reason}",
            "detail": f"Account safety gate rejected operation: {safety_reason}",
            "write_intent_id": None,
            "approval_id": approval_id,
            "transport_invoked": False,
        }

    # ── 5. Quota & Action Limits ────────────────────────────────────────
    if not resolved_prof.can_execute(ActionLog.ActionType.CONNECT):
        claimed_approval.status = "approved"
        claimed_approval.claimed_at = None
        claimed_approval.failure_reason = "Daily connect limit reached"
        claimed_approval.save(update_fields=["status", "claimed_at", "failure_reason"])
        return {
            "status": "blocked",
            "code": "quota_exhausted_connect",
            "detail": "Daily connect action limit reached for provider profile",
            "write_intent_id": None,
            "approval_id": approval_id,
            "transport_invoked": False,
        }

    # ── 6. Cross-Process Concurrency Serialization (ProviderAccountLease) ─
    with account_provider_lock(
        linkedin_profile=resolved_prof,
        workspace=resolved_ws,
        operation_name="manual_connect",
        lease_seconds=180,
    ) as locked_prof:
        if not locked_prof:
            # Lease conflict occurred BEFORE write intent claim / provider transport
            claimed_approval.status = "approved"
            claimed_approval.claimed_at = None
            claimed_approval.failure_reason = "ProviderAccountLease conflict"
            claimed_approval.save(update_fields=["status", "claimed_at", "failure_reason"])
            return {
                "status": "blocked",
                "code": "lease_conflict",
                "detail": "Could not acquire ProviderAccountLease (held by another worker)",
                "write_intent_id": None,
                "approval_id": approval_id,
                "transport_invoked": False,
            }

        # ── 7. Idempotency & ProviderWriteIntent Claim (Inside Acquired Lease) ─
        public_id = deal.lead.public_identifier or deal.lead.linkedin_url
        idempotency_key = f"manual_connect_{resolved_prof.id}_{deal.lead_id}_{approval_id}"

        # Check ConnectionAttempt for existing locks
        conn_attempt = ConnectionAttempt.objects.filter(idempotency_key=idempotency_key).first()
        if conn_attempt and conn_attempt.status in ["externally_confirmed", "attempting", "uncertain"]:
            return {
                "status": "blocked",
                "code": "duplicate_connection_attempt_locked",
                "detail": f"ConnectionAttempt already exists with status {conn_attempt.status}",
                "write_intent_id": None,
                "approval_id": approval_id,
                "transport_invoked": False,
            }

        intent_ok, intent_reason, write_intent = claim_provider_write(
            workspace=resolved_ws,
            linkedin_profile=resolved_prof,
            operation_type=ProviderWriteIntent.OperationType.CONNECTION_SEND,
            target_identity=public_id,
            logical_action_identity=f"manual_conn_deal_{deal.id}_appr_{approval_id}",
            custom_idempotency_key=idempotency_key,
        )
        if not intent_ok or not write_intent:
            claimed_approval.status = "approved"
            claimed_approval.claimed_at = None
            claimed_approval.failure_reason = f"ProviderWriteIntent blocked: {intent_reason}"
            claimed_approval.save(update_fields=["status", "claimed_at", "failure_reason"])
            return {
                "status": "blocked",
                "code": f"write_intent_blocked_{intent_reason}",
                "detail": f"ProviderWriteIntent claim failed: {intent_reason}",
                "write_intent_id": None,
                "approval_id": approval_id,
                "transport_invoked": False,
            }

        if not conn_attempt:
            conn_attempt = ConnectionAttempt.objects.create(
                linkedin_profile=resolved_prof,
                recipient_public_id=public_id,
                idempotency_key=idempotency_key,
                status="attempting",
                note_text=normalized_note,
            )
        else:
            conn_attempt.status = "attempting"
            conn_attempt.note_text = normalized_note
            conn_attempt.save(update_fields=["status", "note_text"])

        # ── 8. Provider Transport Invocation ─────────────────────────────
        record_provider_write_attempt(write_intent)
        transport_invoked = True

        try:
            if custom_transport:
                res = custom_transport(
                    linkedin_profile=resolved_prof,
                    deal=deal,
                    note_text=normalized_note,
                )
                new_state = DealState.PENDING if not isinstance(res, DealState) else res
            else:
                from openoutreach.linkedin.browser.connect_with_note import send_connection_request_with_note
                from openoutreach.linkedin.browser.session import AccountSession

                if session is None:
                    session = AccountSession(resolved_prof)
                
                profile_dict = {"public_identifier": public_id, "linkedin_url": deal.lead.linkedin_url}
                new_state = DealState(
                    send_connection_request_with_note(
                        session=session,
                        profile=profile_dict,
                        note=normalized_note,
                    ).value
                )

            # ── 9. Point of No Return: Finalize Success ──────────────────
            note_metadata = {
                "note": normalized_note,
                "note_length": len(normalized_note),
                "lead_public_id": public_id,
                "manual_operator_approval_id": approval_id,
                "operator_user": claimed_approval.operator.username if claimed_approval.operator else "operator",
            }
            finalize_external_connection_success(
                connection_attempt=conn_attempt,
                campaign=getattr(deal, "campaign", None),
                new_state=new_state,
                note_metadata=note_metadata,
                approval=claimed_approval,
            )
            confirm_provider_write(write_intent, confirmation_data={"new_state": new_state.value if hasattr(new_state, "value") else str(new_state)})

            record_provider_read(
                operation_type=ProviderOperationType.CONNECTION_UI,
                resource_identifier=public_id,
                cache_hit=False,
                linkedin_profile=resolved_prof,
                workspace=resolved_ws,
                success=True,
            )

            return {
                "status": "success",
                "code": "externally_confirmed",
                "detail": f"Manual connection request sent successfully (new_state={new_state})",
                "write_intent_id": write_intent.id,
                "approval_id": approval_id,
                "transport_invoked": True,
            }

        except Exception as e:
            logger.exception("Manual connection transport failed or resulted in uncertainty: %s", e)
            err_str = str(e)
            handle_external_connection_uncertainty(
                connection_attempt=conn_attempt,
                error_message=err_str,
                approval=claimed_approval,
            )
            # Transition write intent to UNCERTAIN to block automatic retries
            fail_provider_write(write_intent, error_message=err_str[:500], before_provider=False)

            return {
                "status": "uncertain",
                "code": "provider_uncertainty",
                "detail": f"Provider transport failed with uncertainty: {err_str}",
                "write_intent_id": write_intent.id,
                "approval_id": approval_id,
                "transport_invoked": True,
            }
