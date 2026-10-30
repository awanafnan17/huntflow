# openoutreach/linkedin/pipeline/search.py
"""Search keyword management and LinkedIn People search."""
from __future__ import annotations

import logging

from django.utils import timezone
from termcolor import colored

logger = logging.getLogger(__name__)


def run_search(session) -> str | None:
    """Use the next search keyword to discover new profiles. Returns keyword or None."""
    from openoutreach.linkedin.services.discovery_service import discover_campaign_targets

    campaign = session.campaign
    res = discover_campaign_targets(campaign, limit=10)
    if res.get("status") == "success":
        kw = res.get("keyword")
        logger.info(colored("\u25b6 search", "magenta", attrs=["bold"]) + " keyword=%r (found %d profiles, created %d deals)", kw, res.get("profiles_found", 0), res.get("deals_created", 0))
        return kw
    return None
