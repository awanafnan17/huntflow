# openoutreach/linkedin/services/idempotency.py
"""
Durable Provider-Write Idempotency & Write Intent Engine for SaaS LinkedIn Operations.
Ensures zero duplicate provider writes across crashes, retries, concurrent workers, and uncertain outcomes.
"""
from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import timedelta
from typing import Optional, Tuple

from django.db import transaction
from django.utils import timezone

from openoutreach.core.models import Workspace
from openoutreach.linkedin.models import LinkedInProfile, ProviderWriteIntent

logger = logging.getLogger(__name__)

# Reclaim stale claims after 5 minutes if worker crashed before provider attempt
STALE_CLAIM_TIMEOUT_MINUTES = 5


def generate_write_idempotency_key(
    linkedin_profile_id: int,
    operation_type: str,
    target_identity: str,
    logical_action_identity: str = "",
) -> str:
    """Generates a deterministic unique idempotency key for a provider write intent."""
    raw = f"{linkedin_profile_id}:{operation_type}:{target_identity}:{logical_action_identity}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def claim_provider_write(
    workspace: Workspace,
    linkedin_profile: LinkedInProfile,
    operation_type: str,
    target_identity: str,
    logical_action_identity: str = "",
    custom_idempotency_key: Optional[str] = None,
) -> Tuple[bool, str, Optional[ProviderWriteIntent]]:
    """
    Atomically claims a provider write intent before interacting with LinkedIn.
    
    Returns:
        (allowed: bool, reason: str, intent: ProviderWriteIntent | None)
    
    Enforces:
    - CONFIRMED: BLOCKED (write was already executed and confirmed).
    - PROVIDER_ATTEMPTED / UNCERTAIN: BLOCKED (crash window / unknown state; requires reconciliation, NEVER blind retry).
    - CLAIMED (< 5m): BLOCKED (concurrent worker actively executing).
    - PLANNED / FAILED_BEFORE_PROVIDER / Stale CLAIMED: ALLOWED (claims the intent atomically).
    """
    if not workspace or not getattr(workspace, "pk", None):
        return False, "missing_workspace", None
    if not linkedin_profile or not getattr(linkedin_profile, "pk", None):
        return False, "missing_linkedin_profile", None

    idem_key = custom_idempotency_key or generate_write_idempotency_key(
        linkedin_profile_id=linkedin_profile.pk,
        operation_type=operation_type,
        target_identity=target_identity,
        logical_action_identity=logical_action_identity,
    )

    with transaction.atomic():
        intent, created = ProviderWriteIntent.objects.select_for_update().get_or_create(
            idempotency_key=idem_key,
            defaults={
                "workspace": workspace,
                "linkedin_profile": linkedin_profile,
                "operation_type": operation_type,
                "target_identity": target_identity,
                "logical_action_identity": logical_action_identity,
                "state": ProviderWriteIntent.State.CLAIMED,
            },
        )

        if created:
            logger.info("Claimed new ProviderWriteIntent: %s [%s]", intent.idempotency_key[:12], operation_type)
            return True, "claimed", intent

        # Intent already existed in DB: verify profile and workspace match
        if intent.linkedin_profile_id != linkedin_profile.pk:
            logger.error("Provider write intent %s profile mismatch (%d != %d) — blocked", intent.idempotency_key[:12], intent.linkedin_profile_id, linkedin_profile.pk)
            return False, "provider_profile_intent_mismatch", intent

        if intent.workspace_id != workspace.pk:
            logger.error("Provider write intent %s workspace mismatch (%d != %d) — blocked", intent.idempotency_key[:12], intent.workspace_id, workspace.pk)
            return False, "workspace_intent_mismatch", intent

        # Check its state
        if intent.state == ProviderWriteIntent.State.CONFIRMED:
            logger.warning("Provider write intent %s already CONFIRMED — duplicate write prevented", intent.idempotency_key[:12])
            return False, "already_confirmed", intent

        if intent.state in (ProviderWriteIntent.State.PROVIDER_ATTEMPTED, ProviderWriteIntent.State.UNCERTAIN):
            logger.critical(
                "Provider write intent %s in %s state — blind retry BLOCKED (requires reconciliation)",
                intent.idempotency_key[:12], intent.state
            )
            return False, "uncertain_requires_reconciliation", intent

        if intent.state == ProviderWriteIntent.State.CLAIMED:
            # Check if stale claim
            stale_threshold = timezone.now() - timedelta(minutes=STALE_CLAIM_TIMEOUT_MINUTES)
            if intent.created_at >= stale_threshold:
                logger.warning(
                    "Provider write intent %s is actively CLAIMED by concurrent worker — blocked",
                    intent.idempotency_key[:12]
                )
                return False, "already_claimed_by_concurrent_worker", intent
            else:
                logger.warning(
                    "Reclaiming stale ProviderWriteIntent %s (> %d min old)",
                    intent.idempotency_key[:12], STALE_CLAIM_TIMEOUT_MINUTES
                )

        # Transition to CLAIMED
        intent.state = ProviderWriteIntent.State.CLAIMED
        intent.save(update_fields=["state"])
        return True, "claimed", intent


def record_provider_write_attempt(intent: ProviderWriteIntent) -> None:
    """
    Marks the intent as PROVIDER_ATTEMPTED immediately prior to making the network/browser write.
    If the worker crashes after this point, retry will fail closed as UNCERTAIN rather than duplicate-writing.
    """
    if not intent:
        return
    intent.state = ProviderWriteIntent.State.PROVIDER_ATTEMPTED
    intent.attempted_at = timezone.now()
    intent.save(update_fields=["state", "attempted_at"])
    logger.debug("Recorded PROVIDER_ATTEMPTED for intent %s", intent.idempotency_key[:12])


def confirm_provider_write(
    intent: ProviderWriteIntent,
    confirmation_data: Optional[dict] = None,
) -> None:
    """Marks the intent as CONFIRMED with optional provider evidence metadata."""
    if not intent:
        return
    intent.state = ProviderWriteIntent.State.CONFIRMED
    intent.confirmed_at = timezone.now()
    if confirmation_data:
        intent.provider_confirmation = confirmation_data
    intent.save(update_fields=["state", "confirmed_at", "provider_confirmation"])
    logger.info("CONFIRMED ProviderWriteIntent %s [%s]", intent.idempotency_key[:12], intent.operation_type)


def fail_provider_write(
    intent: ProviderWriteIntent,
    error_message: str = "",
    before_provider: bool = False,
) -> None:
    """
    Marks the intent as failed.
    If before_provider=True, sets state to FAILED_BEFORE_PROVIDER (safe to retry).
    If before_provider=False, sets state to UNCERTAIN (cannot retry without reconciliation).
    """
    if not intent:
        return
    if before_provider:
        intent.state = ProviderWriteIntent.State.FAILED_BEFORE_PROVIDER
    else:
        intent.state = ProviderWriteIntent.State.UNCERTAIN
    intent.error_message = error_message
    intent.save(update_fields=["state", "error_message"])
    logger.warning("Marked ProviderWriteIntent %s as %s: %s", intent.idempotency_key[:12], intent.state, error_message)
