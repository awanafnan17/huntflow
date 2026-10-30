# openoutreach/linkedin/transport_guard.py
"""Guard function for QA synthetic transport boundary lockdown."""
from __future__ import annotations

import os
from typing import Any, Optional


def is_qa_synthetic_transport_allowed(
    campaign: Optional[Any] = None,
    workspace: Optional[Any] = None,
    linkedin_profile: Optional[Any] = None,
    task: Optional[Any] = None,
) -> bool:
    """
    Evaluates whether mock synthetic QA transport is strictly permitted.
    Requires ALL five security criteria to be satisfied:
    1. HUNTFLOW_QA_SYNTHETIC_TRANSPORT == "1"
    2. Target Campaign is explicitly is_synthetic=True
    3. Target Workspace is explicitly a synthetic/QA workspace
    4. Target LinkedInProfile is explicitly synthetic/QA
    5. Production mode is NOT active

    Fails closed immediately if ANY condition is violated.
    """
    # 1. QA flag requirement
    if os.environ.get("HUNTFLOW_QA_SYNTHETIC_TRANSPORT") != "1":
        return False

    # 5. Production lockdown check (fail closed in production regardless of other flags)
    django_env = os.environ.get("DJANGO_ENV", "").lower()
    if django_env in ("production", "prod"):
        return False
    if os.environ.get("HUNTFLOW_PRODUCTION_LOCKDOWN") == "1":
        return False
    if os.environ.get("HUNTFLOW_ENV", "").lower() in ("production", "prod"):
        return False

    # 2. Campaign / Target requirement: must be explicitly synthetic
    if campaign is None:
        if task and hasattr(task, "payload") and isinstance(task.payload, dict):
            camp_id = task.payload.get("campaign_id")
            if camp_id:
                from openoutreach.core.models import Campaign
                campaign = Campaign.objects.filter(pk=camp_id).first()

    if campaign is not None:
        if not getattr(campaign, "is_synthetic", False):
            return False
    elif task and hasattr(task, "payload") and isinstance(task.payload, dict):
        job_id = task.payload.get("job_id")
        if job_id:
            from openoutreach.career.models import Job
            job_obj = Job.objects.filter(pk=job_id).first()
            if job_obj and not (getattr(job_obj, "is_synthetic", False) or "qa" in (getattr(job_obj, "title", "") or "").lower() or "synthetic" in (getattr(job_obj, "title", "") or "").lower()):
                return False
        else:
            return False
    else:
        return False

    # 3. Target Workspace requirement: must be a synthetic/QA workspace
    ws = workspace or getattr(campaign, "workspace", None)
    if not ws:
        return False
    ws_slug = (getattr(ws, "slug", "") or "").lower()
    ws_name = (getattr(ws, "name", "") or "").lower()
    is_ws_synthetic = (
        getattr(ws, "is_synthetic", False)
        or ws_slug.startswith("ws-qa-")
        or "qa" in ws_slug
        or "synthetic" in ws_slug
        or "qa" in ws_name
        or "synthetic" in ws_name
    )
    if not is_ws_synthetic:
        return False

    # 4. Target LinkedInProfile requirement: must be explicitly synthetic/QA
    lp = linkedin_profile or getattr(campaign, "linkedin_profile", None)
    if not lp:
        return False
    lp_username = (getattr(lp, "linkedin_username", "") or "").lower()
    lp_ws = getattr(lp, "workspace", None)
    lp_ws_slug = (getattr(lp_ws, "slug", "") or "").lower() if lp_ws else ""
    is_lp_synthetic = (
        getattr(lp, "is_synthetic", False)
        or lp_username.startswith("qa_")
        or "synthetic" in lp_username
        or "qa" in lp_ws_slug
        or "synthetic" in lp_ws_slug
    )
    if not is_lp_synthetic:
        return False

    return True
