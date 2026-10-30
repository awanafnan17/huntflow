# openoutreach/linkedin/services/efficiency_metrics.py
"""
Provider Read Efficiency Telemetry and Metrics Engine.

Computes exact operational KPIs:
- Provider reads / successful application
- Provider reads / qualified prospect
- Cache hit rate and duplicate reads avoided
- Account-level activity breakdown
"""
from typing import Dict, Any, Optional
from django.utils import timezone
from openoutreach.core.models import Workspace
from openoutreach.linkedin.models import (
    LinkedInProfile,
    ProviderReadLedger,
    ProviderOperationType,
)
from openoutreach.core.services.quota_service import get_daily_successful_easy_apply_count
from openoutreach.crm.models.deal import Deal, DealState


def get_provider_efficiency_summary(
    workspace: Optional[Workspace] = None,
    linkedin_profile: Optional[LinkedInProfile] = None,
) -> Dict[str, Any]:
    """Generates the authoritative LinkedIn Provider Efficiency Summary."""
    today_start = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
    
    qs = ProviderReadLedger.objects.filter(created_at__gte=today_start)
    if workspace:
        qs = qs.filter(workspace=workspace)
    if linkedin_profile:
        qs = qs.filter(linkedin_profile=linkedin_profile)

    total_entries = qs.count()
    cache_hits = qs.filter(cache_hit=True).count()
    live_reads = qs.filter(cache_hit=False).count()
    unique_resources = qs.values("resource_identifier").distinct().count()

    job_search_reads = qs.filter(operation_type=ProviderOperationType.JOB_SEARCH, cache_hit=False).count()
    job_detail_reads = qs.filter(operation_type=ProviderOperationType.JOB_DETAIL, cache_hit=False).count()
    prospect_searches = qs.filter(operation_type=ProviderOperationType.PROSPECT_SEARCH, cache_hit=False).count()
    profile_views = qs.filter(operation_type=ProviderOperationType.PROFILE_DETAIL, cache_hit=False).count()
    inbox_reads = qs.filter(operation_type__in=[ProviderOperationType.INBOX, ProviderOperationType.PENDING_INVITATIONS], cache_hit=False).count()
    session_health_reads = qs.filter(operation_type=ProviderOperationType.SESSION_HEALTH, cache_hit=False).count()

    # Successful Outcomes Today
    successful_applications = get_daily_successful_easy_apply_count(workspace=workspace) if workspace else 0
    
    deals_qs = Deal.objects.filter(creation_date__gte=today_start)
    if workspace:
        deals_qs = deals_qs.filter(campaign__workspace=workspace)
    qualified_prospects = deals_qs.filter(state__in=[DealState.READY_TO_CONNECT, DealState.QUALIFIED, DealState.CONNECTED, DealState.PENDING]).count()
    connections_sent = deals_qs.filter(state__in=[DealState.PENDING, DealState.CONNECTED, DealState.COMPLETED]).count()

    # Compute Ratios
    reads_per_successful_app = (
        round(live_reads / successful_applications, 2) if successful_applications > 0 else "N/A (0 apps)"
    )
    reads_per_qualified_prospect = (
        round(live_reads / qualified_prospects, 2) if qualified_prospects > 0 else "N/A (0 prospects)"
    )

    return {
        "provider_reads_today": live_reads,
        "cache_hits_today": cache_hits,
        "total_access_attempts": total_entries,
        "duplicate_reads_avoided": cache_hits,
        "unique_resources_accessed": unique_resources,
        "job_search_reads": job_search_reads,
        "job_detail_reads": job_detail_reads,
        "prospect_searches": prospect_searches,
        "full_profile_views": profile_views,
        "inbox_pending_reads": inbox_reads,
        "session_health_reads": session_health_reads,
        "successful_applications": successful_applications,
        "qualified_prospects": qualified_prospects,
        "connections_sent": connections_sent,
        "reads_per_successful_app": reads_per_successful_app,
        "reads_per_qualified_prospect": reads_per_qualified_prospect,
    }
