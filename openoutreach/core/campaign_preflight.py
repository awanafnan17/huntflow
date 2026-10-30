# openoutreach/core/campaign_preflight.py
"""Campaign Preflight & Resume Readiness Gate Service."""
from __future__ import annotations

import logging
from typing import Dict, Any

from openoutreach.crm.models.deal import DealState
from openoutreach.career.models import IncidentHold

logger = logging.getLogger(__name__)


def check_campaign_resume_readiness(campaign) -> Dict[str, Any]:
    """Evaluate explicit readiness distinction: DISCOVERY READY vs OUTREACH READY.

    Returns structured readiness status.
    """
    reasons = []

    # 1. Profile Verification
    profile = getattr(campaign, "linkedin_profile", None)
    profile_valid = bool(profile and getattr(profile, "session_status", "") == "valid")
    if not profile_valid:
        reasons.append("LinkedIn profile unauthenticated or missing valid session")

    # 2. Channel Configuration
    linkedin_enabled = bool(getattr(campaign, "linkedin_outreach_enabled", False))
    if not linkedin_enabled:
        reasons.append("LinkedIn outreach channel is disabled on campaign")

    # 3. Targeting Criteria & Consistency Validation
    from openoutreach.linkedin.pipeline.icp_evaluator import validate_campaign_icp_consistency
    icp_validation = validate_campaign_icp_consistency(campaign)

    has_target_criteria = bool(
        getattr(campaign, "target_job_titles", "").strip() or
        getattr(campaign, "target_locations", "").strip()
    )
    if not has_target_criteria:
        reasons.append("Campaign target criteria (job titles / locations) unconfigured")

    if not icp_validation["is_valid"]:
        reasons.extend(icp_validation["warnings"])

    # 4. Incident Hold Check
    incident_hold_active = IncidentHold.objects.filter(active=True).exists()
    if incident_hold_active:
        reasons.append("System incident hold is currently active")

    discovery_ready = profile_valid and linkedin_enabled and has_target_criteria and not incident_hold_active

    # 5. Outreach Readiness
    is_active = bool(getattr(campaign, "is_active", False))
    ready_deals_count = campaign.deals.filter(state=DealState.READY_TO_CONNECT).count()

    if not is_active:
        reasons.append("Campaign is PAUSED (is_active=False)")
    if ready_deals_count == 0:
        reasons.append("Zero prospects in READY_TO_CONNECT deal state")

    outreach_ready = discovery_ready and is_active and (ready_deals_count > 0) and icp_validation["is_valid"]

    return {
        "discovery_ready": discovery_ready,
        "outreach_ready": outreach_ready,
        "is_active": is_active,
        "ready_deals_count": ready_deals_count,
        "icp_validation": icp_validation,
        "reasons": reasons,
    }
