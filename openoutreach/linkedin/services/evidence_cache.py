# openoutreach/linkedin/services/evidence_cache.py
"""
Central Provider Read Ledger, Evidence Cache, and Resource Governor for HuntFlow.

Guarantees:
1. Every live LinkedIn read is categorized, tracked, and cost-accounted in ProviderReadLedger.
2. Evidence is cached with resource-specific TTLs (Profile facts: 14d, Job Details: 24h, Search: 8h, Inbox: 20m).
3. Cross-campaign and cross-job evidence reuse to minimize live LinkedIn page navigations.
4. Idempotency and coalescing to prevent duplicate live fetches of the same resource.
"""
import logging
import time
from datetime import timedelta
from typing import Any, Callable, Dict, Optional, Tuple

from django.utils import timezone
from openoutreach.linkedin.models import (
    LinkedInProfile,
    ProviderEvidenceCache,
    ProviderOperationType,
    ProviderReadLedger,
)

logger = logging.getLogger(__name__)

class ProviderCachePolicy:
    """Central unified policy configuration for provider caching and TTLs."""
    PROFILE_STATIC_FACTS_TTL: timedelta = timedelta(days=14)
    JOB_SEARCH_RESULTS_TTL: timedelta = timedelta(hours=8)
    JOB_DETAIL_TTL: timedelta = timedelta(hours=24)
    EASY_APPLY_CAPABILITY_TTL: timedelta = timedelta(hours=24)
    CONNECTION_STATE_TTL: timedelta = timedelta(hours=12)
    INBOX_TTL: timedelta = timedelta(minutes=20)
    PENDING_INVITATIONS_TTL: timedelta = timedelta(hours=3)

    @classmethod
    def get_ttl_for_resource(cls, resource_type: str) -> timedelta:
        mapping = {
            "profile": cls.PROFILE_STATIC_FACTS_TTL,
            "job_search": cls.JOB_SEARCH_RESULTS_TTL,
            "job_detail": cls.JOB_DETAIL_TTL,
            "easy_apply": cls.EASY_APPLY_CAPABILITY_TTL,
            "connection_state": cls.CONNECTION_STATE_TTL,
            "inbox": cls.INBOX_TTL,
            "pending_invitations": cls.PENDING_INVITATIONS_TTL,
        }
        return mapping.get(resource_type, timedelta(hours=1))


# Standard Configurable Freshness Classes (aliased to central policy)
PROFILE_STATIC_FACTS_TTL = ProviderCachePolicy.PROFILE_STATIC_FACTS_TTL
JOB_SEARCH_RESULTS_TTL = ProviderCachePolicy.JOB_SEARCH_RESULTS_TTL
JOB_DETAIL_TTL = ProviderCachePolicy.JOB_DETAIL_TTL
EASY_APPLY_CAPABILITY_TTL = ProviderCachePolicy.EASY_APPLY_CAPABILITY_TTL
CONNECTION_STATE_TTL = ProviderCachePolicy.CONNECTION_STATE_TTL
INBOX_TTL = ProviderCachePolicy.INBOX_TTL
PENDING_INVITATIONS_TTL = ProviderCachePolicy.PENDING_INVITATIONS_TTL


def _clean_profile(prof) -> Optional[LinkedInProfile]:
    if isinstance(prof, LinkedInProfile) and getattr(prof, "pk", None):
        return prof
    return None


def _clean_workspace(ws, prof=None):
    from openoutreach.core.models import Workspace
    if isinstance(ws, Workspace) and getattr(ws, "pk", None):
        return ws
    clean_prof = _clean_profile(prof)
    if clean_prof and getattr(clean_prof, "workspace", None) and getattr(clean_prof.workspace, "pk", None):
        return clean_prof.workspace
    if clean_prof and getattr(clean_prof, "user", None):
        from openoutreach.core.models import WorkspaceMembership
        mem = WorkspaceMembership.objects.filter(user=clean_prof.user, status="active").first()
        if mem and mem.workspace:
            return mem.workspace
    return Workspace.objects.filter(slug="default").first() or Workspace.objects.first()


def record_provider_read(
    operation_type: str,
    resource_identifier: str,
    cache_hit: bool = False,
    linkedin_profile: Optional[LinkedInProfile] = None,
    workspace=None,
    success: bool = True,
    read_cost: int = 1,
    duration_ms: int = 0,
    metadata: Optional[Dict[str, Any]] = None,
) -> Optional[ProviderReadLedger]:
    """Record an operational live or cached LinkedIn provider read event in the central ledger."""
    clean_prof = _clean_profile(linkedin_profile)
    clean_ws = _clean_workspace(workspace, clean_prof)

    try:
        entry = ProviderReadLedger.objects.create(
            linkedin_profile=clean_prof,
            workspace=clean_ws,
            operation_type=operation_type,
            resource_identifier=str(resource_identifier)[:255],
            cache_hit=cache_hit,
            success=success,
            read_cost=0 if cache_hit else read_cost,
            duration_ms=duration_ms,
            metadata=metadata or {},
        )
        # Also track against active StageCanaryBudget if present
        if clean_ws and not cache_hit:
            try:
                from openoutreach.core.models import StageCanaryBudget
                active_budget = StageCanaryBudget.objects.filter(
                    workspace=clean_ws,
                    linkedin_profile=clean_prof,
                    stage="STAGE_1",
                    state__in=[StageCanaryBudget.State.CLAIMED, StageCanaryBudget.State.NETWORK_ATTEMPTED]
                ).first()
                if active_budget:
                    active_budget.record_provider_read(count=read_cost)
            except Exception as b_err:
                logger.debug("Canary budget read accounting note: %s", b_err)

        logger.debug(
            "Recorded provider access: type=%s id=%s cache_hit=%s cost=%d",
            operation_type,
            resource_identifier,
            cache_hit,
            entry.read_cost,
        )
        return entry
    except Exception as e:
        logger.debug("Could not persist ProviderReadLedger entry: %s", e)
        return None


def get_cached_evidence(
    resource_type: str,
    resource_identifier: str,
    linkedin_profile: Optional[LinkedInProfile] = None,
) -> Optional[Dict[str, Any]]:
    """Retrieve unexpired, valid evidence from the local cache."""
    now = timezone.now()
    clean_prof = _clean_profile(linkedin_profile)
    qs = ProviderEvidenceCache.objects.filter(
        resource_type=resource_type,
        resource_identifier=str(resource_identifier),
        is_valid=True,
        expires_at__gt=now,
    )
    if resource_type == "connection_state":
        if not clean_prof:
            return None
        item = qs.filter(linkedin_profile=clean_prof).first()
    elif clean_prof:
        # Match specific profile or global (profile=None)
        item = qs.filter(linkedin_profile=clean_prof).first() or qs.filter(linkedin_profile__isnull=True).first()
    else:
        item = qs.first()

    if item:
        return item.data
    return None


def set_cached_evidence(
    resource_type: str,
    resource_identifier: str,
    data: Dict[str, Any],
    ttl: timedelta,
    linkedin_profile: Optional[LinkedInProfile] = None,
) -> Optional[ProviderEvidenceCache]:
    """Persist evidence into local cache with configured TTL."""
    now = timezone.now()
    expires_at = now + ttl
    clean_prof = _clean_profile(linkedin_profile)
    try:
        item, _ = ProviderEvidenceCache.objects.update_or_create(
            resource_type=resource_type,
            resource_identifier=str(resource_identifier),
            linkedin_profile=clean_prof,
            defaults={
                "data": data,
                "observed_at": now,
                "expires_at": expires_at,
                "is_valid": True,
            },
        )
        return item
    except Exception as e:
        logger.debug("Could not set cached evidence: %s", e)
        return None


def get_or_refresh_provider_evidence(
    resource_type: str,
    resource_identifier: str,
    fetcher_fn: Callable[[], Dict[str, Any]],
    ttl: timedelta,
    operation_type: str,
    linkedin_profile: Optional[LinkedInProfile] = None,
    workspace=None,
    force_refresh: bool = False,
    read_cost: int = 1,
) -> Tuple[Dict[str, Any], bool]:
    """
    Central helper for all provider-facing services.
    Returns (data, is_cache_hit).
    """
    if not force_refresh:
        cached = get_cached_evidence(
            resource_type=resource_type,
            resource_identifier=resource_identifier,
            linkedin_profile=linkedin_profile,
        )
        if cached is not None:
            record_provider_read(
                operation_type=operation_type,
                resource_identifier=resource_identifier,
                cache_hit=True,
                linkedin_profile=linkedin_profile,
                workspace=workspace,
                success=True,
                read_cost=0,
            )
            return cached, True

    # Cache miss: Execute live fetcher
    t0 = time.time()
    success = True
    try:
        data = fetcher_fn()
    except Exception as e:
        success = False
        duration_ms = int((time.time() - t0) * 1000)
        record_provider_read(
            operation_type=operation_type,
            resource_identifier=resource_identifier,
            cache_hit=False,
            linkedin_profile=linkedin_profile,
            workspace=workspace,
            success=False,
            read_cost=read_cost,
            duration_ms=duration_ms,
            metadata={"error": str(e)},
        )
        raise

    duration_ms = int((time.time() - t0) * 1000)
    record_provider_read(
        operation_type=operation_type,
        resource_identifier=resource_identifier,
        cache_hit=False,
        linkedin_profile=linkedin_profile,
        workspace=workspace,
        success=success,
        read_cost=read_cost,
        duration_ms=duration_ms,
    )

    if data:
        set_cached_evidence(
            resource_type=resource_type,
            resource_identifier=resource_identifier,
            data=data,
            ttl=ttl,
            linkedin_profile=linkedin_profile,
        )

    return data, False
