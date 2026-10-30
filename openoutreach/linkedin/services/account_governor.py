# openoutreach/linkedin/services/account_governor.py
"""
Single Account-Level Activity Governor, Rate Controller & Circuit Breaker for HuntFlow SaaS.

Strict Fail-Closed Invariant:
All live provider reads and writes MUST resolve a valid, unambiguous workspace and provider account.
Missing, ambiguous, or mismatched tenant context fails closed immediately with zero cross-tenant fallback.
"""
import logging
from contextlib import contextmanager
from datetime import timedelta
from typing import Tuple, Optional
from django.utils import timezone
from openoutreach.core.models import Workspace, WorkspaceAutomationSettings
from openoutreach.linkedin.models import LinkedInProfile, ProviderReadLedger, ActionLog, ProviderSafetyEvent

logger = logging.getLogger(__name__)

WARNING_PHRASES = [
    "accessed a high volume of linkedin profile",
    "accessed a high volume of profile data",
    "high volume of profile",
    "unusual activity on your account",
    "activity on your account",
    "we've noticed unusual activity",
    "temporarily restricted",
    "security checkpoint",
    "verify your identity",
    "quick security check",
    "unusual traffic from your computer network",
]

# SaaS Account Protective Budgets
BURST_READ_LIMIT_5M = 25       # Max 25 live reads per rolling 5-minute window
HOURLY_READ_BUDGET = 90        # Max 90 live reads per rolling hour
BURST_WRITE_LIMIT_10M = 12     # Max 12 provider writes per rolling 10-minute window


def resolve_provider_context(
    workspace: Optional[Workspace] = None,
    linkedin_profile: Optional[LinkedInProfile] = None,
    campaign=None,
    task=None,
    candidate=None,
) -> Tuple[Optional[Workspace], Optional[LinkedInProfile], Optional[str]]:
    """
    Deterministically resolves a single workspace and LinkedInProfile without arbitrary fallback.
    Returns (workspace, linkedin_profile, error_string). If invalid, returns (None, None, error_string).
    """
    resolved_ws = workspace if (isinstance(workspace, Workspace) and getattr(workspace, "pk", None)) else None
    resolved_prof = linkedin_profile if (isinstance(linkedin_profile, LinkedInProfile) and getattr(linkedin_profile, "pk", None)) else None

    # Resolve from campaign if present
    if campaign:
        camp_ws = getattr(campaign, "workspace", None)
        if isinstance(camp_ws, Workspace) and getattr(camp_ws, "pk", None):
            if resolved_ws and resolved_ws.pk != camp_ws.pk:
                return None, None, "campaign_workspace_mismatch"
            resolved_ws = camp_ws
        camp_prof = getattr(campaign, "linkedin_profile", None)
        if isinstance(camp_prof, LinkedInProfile) and getattr(camp_prof, "pk", None):
            if resolved_prof and resolved_prof.pk != camp_prof.pk:
                return None, None, "campaign_linkedin_profile_mismatch"
            resolved_prof = camp_prof

    # Resolve from task if present
    if task and hasattr(task, "payload") and isinstance(task.payload, dict):
        task_ws_id = task.payload.get("workspace_id")
        if task_ws_id:
            if resolved_ws and resolved_ws.pk != task_ws_id:
                return None, None, "task_workspace_mismatch"
            if not resolved_ws:
                resolved_ws = Workspace.objects.filter(pk=task_ws_id).first()

    # Resolve from profile's workspace
    if resolved_prof and getattr(resolved_prof, "workspace", None):
        prof_ws = resolved_prof.workspace
        if isinstance(prof_ws, Workspace) and getattr(prof_ws, "pk", None):
            if resolved_ws and resolved_ws.pk != prof_ws.pk:
                return None, None, "workspace_mismatch_cross_tenant_access_blocked"
            if not resolved_ws:
                resolved_ws = prof_ws

    # Check candidate workspace if present
    if candidate:
        cand_ws = getattr(candidate, "workspace", None)
        if not cand_ws and hasattr(candidate, "user") and candidate.user:
            try:
                from openoutreach.core.models import WorkspaceMembership
                mem = WorkspaceMembership.objects.filter(user=candidate.user, status="active").first()
                if mem:
                    cand_ws = mem.workspace
            except Exception:
                cand_ws = None
        if isinstance(cand_ws, Workspace) and getattr(cand_ws, "pk", None):
            if resolved_ws and resolved_ws.pk != cand_ws.pk:
                return None, None, "candidate_workspace_mismatch"
            if not resolved_ws:
                resolved_ws = cand_ws

    if not resolved_ws:
        return None, None, "missing_workspace_fail_closed"

    return resolved_ws, resolved_prof, None


def check_and_record_safety_warning(
    page,
    workspace: Optional[Workspace] = None,
    linkedin_profile: Optional[LinkedInProfile] = None,
) -> bool:
    """
    Inspects active page state for LinkedIn security or high-volume data access warnings.
    If detected, triggers persistent circuit breaker stopping all provider actions for the tenant.
    """
    if not page:
        return False

    current_url = ""
    page_text = ""
    try:
        current_url = (page.url or "").lower()
        page_text = (page.evaluate("() => document.body ? document.body.innerText : ''") or "").lower()
    except Exception as e:
        logger.debug("Governor page inspection note: %s", e)
        return False

    warning_detected = False
    warning_reason = ""

    if "checkpoint" in current_url or "challenge" in current_url:
        warning_detected = True
        warning_reason = "linkedin_security_checkpoint"
    elif "restricted" in current_url:
        warning_detected = True
        warning_reason = "linkedin_account_restricted"
    else:
        for phrase in WARNING_PHRASES:
            if phrase in page_text:
                warning_detected = True
                warning_reason = "high_volume_data_access_warning"
                break

    if warning_detected:
        logger.critical("SAFETY CIRCUIT BREAKER TRIGGERED: %s detected on LinkedIn.", warning_reason)

        ws, prof, err = resolve_provider_context(workspace=workspace, linkedin_profile=linkedin_profile)

        # 1. Profile Account Health Level
        if prof:
            if "checkpoint" in warning_reason:
                prof.session_status = "checkpoint"
            elif "restricted" in warning_reason:
                prof.session_status = "restricted"
            prof.session_error_details = f"Safety circuit breaker: {warning_reason}"
            prof.save(update_fields=["session_status", "session_error_details"])

        # 2. Workspace Policy Level (High-volume scrape warnings stop workspace)
        if ws:
            if warning_reason == "high_volume_data_access_warning":
                auto_st = WorkspaceAutomationSettings.get_for_workspace(ws)
                if auto_st:
                    auto_st.trigger_safety_stop(warning_reason)

            # 3. Append-only Audit History
            try:
                ProviderSafetyEvent.objects.create(
                    workspace=ws,
                    linkedin_profile=prof,
                    event_type=warning_reason,
                    url_category=current_url[:250],
                    action_taken="SAFETY_STOP" if warning_reason == "high_volume_data_access_warning" else "PROFILE_CHECKPOINT",
                    metadata={"url": current_url[:500]},
                )
            except Exception as e:
                logger.error("Failed to record ProviderSafetyEvent: %s", e)

        return True

    return False


def is_account_safe_for_provider_read(
    workspace: Optional[Workspace] = None,
    linkedin_profile: Optional[LinkedInProfile] = None,
    campaign=None,
    task=None,
    candidate=None,
) -> Tuple[bool, str]:
    """
    Verifies that the account and tenant are in a clean, operator-authorized state for live LinkedIn READS.
    Enforces strict multi-tenant fail-closed isolation, circuit breaker states, and rolling burst rate limits.
    """
    # 1. Deterministic Tenant & Account Resolution
    ws, prof, err = resolve_provider_context(
        workspace=workspace,
        linkedin_profile=linkedin_profile,
        campaign=campaign,
        task=task,
        candidate=candidate,
    )
    if err or not ws:
        return False, err or "missing_workspace_fail_closed"

    # 2. LinkedIn Profile Session Health Check
    if prof:
        if prof.session_status in ["checkpoint", "restricted", "needs_reauth", "invalid", "expired"]:
            return False, f"provider_session_status_{prof.session_status}"
        from openoutreach.career.models import IncidentHoldTarget
        if IncidentHoldTarget.objects.filter(
            incident_hold__active=True,
            object_id=prof.id
        ).exists():
            return False, "incident_hold_active_on_provider_account"

    # 3. Workspace Automation & Circuit Breaker Checks
    auto_st = WorkspaceAutomationSettings.get_for_workspace(ws)
    if auto_st:
        if auto_st.reconcile_safety_invariants():
            auto_st.save(update_fields=["provider_safety_state", "soak_status", "soak_stop_reason", "updated_at"])

        if auto_st.provider_safety_state in ["HIGH_VOLUME_DATA_ACCESS_WARNING", "CHECKPOINT_REQUIRED", "ACCOUNT_RESTRICTED"]:
            if auto_st.provider_safety_state == "HIGH_VOLUME_DATA_ACCESS_WARNING":
                return False, "high_volume_data_access_warning_requires_operator_clearance"
            return False, f"provider_safety_state_{auto_st.provider_safety_state.lower()}"

        if auto_st.soak_status == "SAFETY_STOP":
            return False, f"safety_stop_active_{auto_st.soak_stop_reason}"

        if getattr(auto_st, "manual_provider_use_paused", False):
            is_explicit_verify = bool(
                task and getattr(task, "task_type", None) == "session_health_verification"
                and getattr(task, "operator_initiated", False)
            )
            if not is_explicit_verify:
                return False, "manual_provider_use_paused"

    # 4. Rolling Burst & Rate Window Governance
    now = timezone.now()
    five_min_ago = now - timedelta(minutes=5)
    recent_reads_5m = ProviderReadLedger.objects.filter(
        workspace=ws,
        created_at__gte=five_min_ago,
        cache_hit=False,
    )
    if prof:
        recent_reads_5m = recent_reads_5m.filter(linkedin_profile=prof)

    if recent_reads_5m.count() >= BURST_READ_LIMIT_5M:
        return False, "burst_read_limit_exceeded_cooling_down"

    one_hour_ago = now - timedelta(hours=1)
    hourly_reads = ProviderReadLedger.objects.filter(
        workspace=ws,
        created_at__gte=one_hour_ago,
        cache_hit=False,
    )
    if prof:
        hourly_reads = hourly_reads.filter(linkedin_profile=prof)

    if hourly_reads.count() >= HOURLY_READ_BUDGET:
        return False, "hourly_read_budget_exceeded"

    return True, "ok"


def is_account_safe_for_provider_write(
    workspace: Optional[Workspace] = None,
    linkedin_profile: Optional[LinkedInProfile] = None,
    action_type: str = "connect",
    campaign=None,
    task=None,
    candidate=None,
) -> Tuple[bool, str]:
    """
    Verifies that the account and tenant are in a clean, operator-authorized state for live LinkedIn WRITES.
    Enforces action limits, circuit breaker states, and rolling burst write limits.
    """
    # 1. Deterministic Tenant & Account Resolution
    ws, prof, err = resolve_provider_context(
        workspace=workspace,
        linkedin_profile=linkedin_profile,
        campaign=campaign,
        task=task,
        candidate=candidate,
    )
    if err or not ws:
        return False, err or "missing_workspace_fail_closed"
    if not prof:
        return False, "missing_provider_account_fail_closed"

    # 2. LinkedIn Profile Session Health & Daily Action Limit
    if prof.session_status in ["checkpoint", "restricted", "needs_reauth", "invalid", "expired"]:
        return False, f"provider_session_status_{prof.session_status}"
    if not prof.can_execute(action_type):
        return False, f"provider_daily_limit_or_session_exhausted_{action_type}"

    # 3. Incident Hold check for this profile
    from openoutreach.career.models import IncidentHoldTarget
    if IncidentHoldTarget.objects.filter(
        incident_hold__active=True,
        object_id=prof.id
    ).exists():
        return False, "incident_hold_active_on_provider_account"

    # 4. Workspace Automation & Circuit Breaker Checks
    auto_st = WorkspaceAutomationSettings.get_for_workspace(ws)
    if auto_st:
        if auto_st.reconcile_safety_invariants():
            auto_st.save(update_fields=["provider_safety_state", "soak_status", "soak_stop_reason", "updated_at"])

        if auto_st.provider_safety_state in ["HIGH_VOLUME_DATA_ACCESS_WARNING", "CHECKPOINT_REQUIRED", "ACCOUNT_RESTRICTED"]:
            if auto_st.provider_safety_state == "HIGH_VOLUME_DATA_ACCESS_WARNING":
                return False, "high_volume_data_access_warning_requires_operator_clearance"
            return False, f"provider_safety_state_{auto_st.provider_safety_state.lower()}"

        if auto_st.soak_status == "SAFETY_STOP":
            return False, f"safety_stop_active_{auto_st.soak_stop_reason}"

        if getattr(auto_st, "manual_provider_use_paused", False):
            return False, "manual_provider_use_paused"

    # 5. Rolling Burst Write Window Governance
    now = timezone.now()
    ten_min_ago = now - timedelta(minutes=10)
    recent_writes_10m = ActionLog.objects.filter(
        created_at__gte=ten_min_ago,
        linkedin_profile=prof,
    )

    if recent_writes_10m.count() >= BURST_WRITE_LIMIT_10M:
        return False, "burst_write_limit_exceeded_cooling_down"

    return True, "ok"


def get_account_live_read_count_today(
    workspace: Optional[Workspace] = None,
    linkedin_profile: Optional[LinkedInProfile] = None,
) -> int:
    """Returns the count of live (cache_hit=False) LinkedIn provider reads made today."""
    today_start = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
    qs = ProviderReadLedger.objects.filter(
        created_at__gte=today_start,
        cache_hit=False,
    )
    if workspace:
        qs = qs.filter(workspace=workspace)
    if linkedin_profile:
        qs = qs.filter(linkedin_profile=linkedin_profile)

    return qs.count()


def get_account_live_read_count_recent(
    workspace: Optional[Workspace] = None,
    linkedin_profile: Optional[LinkedInProfile] = None,
    minutes: int = 5,
) -> int:
    """Returns the count of live LinkedIn provider reads in the last N minutes."""
    since = timezone.now() - timedelta(minutes=minutes)
    qs = ProviderReadLedger.objects.filter(
        created_at__gte=since,
        cache_hit=False,
    )
    if workspace:
        qs = qs.filter(workspace=workspace)
    if linkedin_profile:
        qs = qs.filter(linkedin_profile=linkedin_profile)
    return qs.count()


def get_account_live_write_count_recent(
    workspace: Optional[Workspace] = None,
    linkedin_profile: Optional[LinkedInProfile] = None,
    minutes: int = 10,
) -> int:
    """Returns the count of provider writes recorded in ActionLog in the last N minutes."""
    since = timezone.now() - timedelta(minutes=minutes)
    qs = ActionLog.objects.filter(created_at__gte=since)
    if linkedin_profile:
        qs = qs.filter(linkedin_profile=linkedin_profile)
    elif workspace:
        qs = qs.filter(campaign__workspace=workspace)
    return qs.count()


def renew_provider_lease(
    linkedin_profile: LinkedInProfile,
    owner_token: str,
    extension_seconds: int = 180,
) -> bool:
    """
    Extends an active ProviderAccountLease while an operation is still actively running.
    Guarantees that only the legitimate current owner possessing owner_token can extend.
    """
    from openoutreach.linkedin.models import ProviderAccountLease
    if not linkedin_profile or not getattr(linkedin_profile, "pk", None) or not owner_token:
        return False

    new_expires_at = timezone.now() + timedelta(seconds=extension_seconds)
    updated = ProviderAccountLease.objects.filter(
        linkedin_profile=linkedin_profile,
        owner_token=owner_token,
    ).update(expires_at=new_expires_at)
    return updated > 0


import threading

_THREAD_ACTIVE_LEASES = threading.local()


def get_active_profile_lease_token(profile_id: int) -> Optional[str]:
    """Return the active owner token if the current thread holds a lease on profile_id."""
    tokens = getattr(_THREAD_ACTIVE_LEASES, "tokens", {})
    return tokens.get(profile_id)


@contextmanager
def account_provider_lock(
    linkedin_profile: Optional[LinkedInProfile] = None,
    workspace: Optional[Workspace] = None,
    operation_name: str = "",
    lease_seconds: int = 180,
    allow_reentrant: bool = False,
):
    """
    Context manager providing cross-process concurrency serialization for live provider actions.
    Combines DB transaction row locking (select_for_update) with durable ProviderAccountLease tracking
    and cryptographic owner tokens to serialize writes across workers while ensuring crash recovery.
    """
    import os
    import uuid
    from django.db import transaction
    from openoutreach.linkedin.models import ProviderAccountLease

    ws, prof, err = resolve_provider_context(workspace=workspace, linkedin_profile=linkedin_profile)
    if not ws:
        yield None
        return

    if prof and getattr(prof, "pk", None):
        # Check reentrant lease on current thread if requested
        if allow_reentrant:
            active_token = get_active_profile_lease_token(prof.pk)
            if active_token:
                yield prof
                return

    worker_id = f"pid_{os.getpid()}_{uuid.uuid4().hex[:6]}"
    owner_token = f"tok_{uuid.uuid4().hex}"
    now = timezone.now()
    expires_at = now + timedelta(seconds=lease_seconds)

    if prof and getattr(prof, "pk", None):
        with transaction.atomic():
            # Clean up expired lease for this profile if any
            ProviderAccountLease.objects.filter(
                linkedin_profile=prof,
                expires_at__lt=now,
            ).delete()

            lease, created = ProviderAccountLease.objects.select_for_update().get_or_create(
                linkedin_profile=prof,
                defaults={
                    "workspace": ws,
                    "locked_by": worker_id,
                    "owner_token": owner_token,
                    "acquired_at": now,
                    "expires_at": expires_at,
                    "active_operation": operation_name,
                },
            )
            if not created and lease.expires_at >= now and lease.owner_token != owner_token:
                logger.warning(
                    "Profile %d is locked by active lease held by %s until %s",
                    prof.pk, lease.locked_by, lease.expires_at
                )
                yield None
                return

            if not created:
                lease.locked_by = worker_id
                lease.owner_token = owner_token
                lease.acquired_at = now
                lease.expires_at = expires_at
                lease.active_operation = operation_name
                lease.save(update_fields=["locked_by", "owner_token", "acquired_at", "expires_at", "active_operation"])

        if not hasattr(_THREAD_ACTIVE_LEASES, "tokens"):
            _THREAD_ACTIVE_LEASES.tokens = {}
        _THREAD_ACTIVE_LEASES.tokens[prof.pk] = owner_token

        try:
            yield prof
        finally:
            if hasattr(_THREAD_ACTIVE_LEASES, "tokens"):
                _THREAD_ACTIVE_LEASES.tokens.pop(prof.pk, None)
            try:
                # Stale owner safety: ONLY release if owner_token matches this context manager instance
                ProviderAccountLease.objects.filter(
                    linkedin_profile=prof,
                    owner_token=owner_token,
                ).delete()
            except Exception as e:
                logger.debug("Lease release note: %s", e)
    else:
        yield ws
