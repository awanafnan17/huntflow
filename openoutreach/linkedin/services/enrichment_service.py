# openoutreach/linkedin/services/enrichment_service.py
"""Authoritative LinkedIn Profile Enrichment Service with Aggressive Evidence Caching."""
from __future__ import annotations

import logging
import re
from typing import Dict, Any, Optional

from openoutreach.crm.models.lead import Lead
from openoutreach.linkedin.models import ProviderOperationType
from openoutreach.linkedin.services.account_governor import (
    check_and_record_safety_warning,
    is_account_safe_for_provider_read,
)
from openoutreach.linkedin.services.evidence_cache import (
    PROFILE_STATIC_FACTS_TTL,
    get_cached_evidence,
    record_provider_read,
    set_cached_evidence,
)

logger = logging.getLogger(__name__)


def enrich_linkedin_lead(lead: Lead, session, force_refresh: bool = False) -> Dict[str, Any]:
    """Authoritative Lead profile enrichment service using Evidence Cache -> Voyager API -> Playwright DOM fallback.

    Populates factual fields: first_name, last_name, current_title, company, country_code/location.
    """
    if not lead or not lead.linkedin_url:
        return {"status": "UNAVAILABLE", "method": "NONE", "lead_id": getattr(lead, "id", None)}

    # 1. Short-circuit if Lead model already has full factual fields
    if not force_refresh and lead.first_name and lead.current_title and lead.company:
        record_provider_read(
            operation_type=ProviderOperationType.PROFILE_DETAIL,
            resource_identifier=lead.public_identifier or lead.linkedin_url,
            cache_hit=True,
            linkedin_profile=getattr(session, "linkedin_profile", None),
            workspace=lead.workspace,
            success=True,
            read_cost=0,
        )
        return {
            "status": "ENRICHED",
            "method": "CACHE",
            "lead_id": lead.pk,
            "public_identifier": lead.public_identifier,
            "fields_populated": ["first_name", "last_name", "current_title", "company", "country_code"],
        }

    # 2. Check Central Provider Evidence Cache (Cross-campaign / cross-workspace reuse)
    pid = lead.public_identifier or lead.linkedin_url
    if not force_refresh:
        cached_facts = get_cached_evidence(
            resource_type="profile",
            resource_identifier=pid,
            linkedin_profile=getattr(session, "linkedin_profile", None),
        )
        if cached_facts:
            _apply_profile_data_to_lead(lead, cached_facts)
            record_provider_read(
                operation_type=ProviderOperationType.PROFILE_DETAIL,
                resource_identifier=pid,
                cache_hit=True,
                linkedin_profile=getattr(session, "linkedin_profile", None),
                workspace=lead.workspace,
                success=True,
                read_cost=0,
            )
            return {
                "status": "ENRICHED",
                "method": "EVIDENCE_CACHE",
                "lead_id": lead.pk,
                "public_identifier": lead.public_identifier,
                "fields_populated": list(cached_facts.keys()),
            }

    # 3. Verify Account Safety Governor before hitting provider
    is_safe, safety_reason = is_account_safe_for_provider_read(
        workspace=lead.workspace,
        linkedin_profile=getattr(session, "linkedin_profile", None),
    )
    if not is_safe:
        logger.warning("Provider profile enrichment blocked by governor: %s", safety_reason)
        return {"status": "BLOCKED", "method": "GOVERNOR_STOP", "reason": safety_reason, "lead_id": lead.pk}

    session.ensure_browser()
    method_used = "NONE"
    profile_data: Dict[str, Any] = {}

    # 4. Primary Path: Voyager API call
    try:
        from linkedin_cli.api.client import PlaywrightLinkedinAPI
        api = PlaywrightLinkedinAPI(session=session)
        voyager_profile, _raw = api.get_profile(profile_url=lead.linkedin_url)
        if voyager_profile:
            profile_data = voyager_profile
            method_used = "VOYAGER"
    except Exception as e:
        logger.debug("Voyager API lookup for %s failed (%s) — switching to browser DOM fallback", lead.public_identifier, e)

    # 5. Fallback Path: Authenticated Playwright DOM Page Navigation (Heavy / High-Cost)
    if not profile_data and session.page:
        try:
            page = session.page
            page.goto(lead.linkedin_url, wait_until="domcontentloaded", timeout=30000)
            check_and_record_safety_warning(page, workspace=lead.workspace, linkedin_profile=getattr(session, "linkedin_profile", None))
            page.wait_for_timeout(1500)

            # Extract Name from Page Title
            raw_title = page.title()
            name_part = raw_title.split("|")[0].strip() if "|" in raw_title else raw_title.strip()
            
            name_tokens = name_part.split()
            first_name = name_tokens[0] if name_tokens else ""
            last_name = " ".join(name_tokens[1:]) if len(name_tokens) > 1 else ""

            # Extract Main Body Text Lines
            headline = ""
            company = ""
            location = ""

            if page.locator("main").count() > 0:
                main_text = page.locator("main").first.inner_text()
                lines = [line.strip() for line in main_text.split("\n") if line.strip()]
                
                # Top Card line extraction heuristic
                if len(lines) >= 2:
                    headline = lines[1]
                if len(lines) >= 3 and not lines[2].startswith("·") and not lines[2].startswith("Contact"):
                    location = lines[2]

                # Extract company from headline ' at ' or top card links
                if " at " in headline:
                    parts = headline.split(" at ", 1)
                    headline_title = parts[0].strip()
                    company_part = parts[1].split("|")[0].split("•")[0].strip()
                    if company_part:
                        company = company_part

            profile_data = {
                "first_name": first_name,
                "last_name": last_name,
                "headline": headline,
                "current_title": headline,
                "company": company,
                "location": location,
                "country_code": "",
                "public_identifier": lead.public_identifier,
            }
            method_used = "BROWSER_DOM"
        except Exception as err:
            logger.error("Browser DOM fallback failed for %s: %s", lead.public_identifier, err)

    if not profile_data:
        record_provider_read(
            operation_type=ProviderOperationType.PROFILE_DETAIL,
            resource_identifier=pid,
            cache_hit=False,
            linkedin_profile=getattr(session, "linkedin_profile", None),
            workspace=lead.workspace,
            success=False,
            read_cost=1,
            metadata={"error": "Profile extraction returned empty data"},
        )
        return {"status": "FAILED", "method": method_used, "reason": "PROFILE_DATA_EMPTY", "lead_id": lead.pk}

    _apply_profile_data_to_lead(lead, profile_data)

    # Persist in Central Evidence Cache
    set_cached_evidence(
        resource_type="profile",
        resource_identifier=pid,
        data=profile_data,
        ttl=PROFILE_STATIC_FACTS_TTL,
        linkedin_profile=getattr(session, "linkedin_profile", None),
    )

    record_provider_read(
        operation_type=ProviderOperationType.PROFILE_DETAIL,
        resource_identifier=pid,
        cache_hit=False,
        linkedin_profile=getattr(session, "linkedin_profile", None),
        workspace=lead.workspace,
        success=True,
        read_cost=2 if method_used == "BROWSER_DOM" else 1,
    )

    return {
        "status": "ENRICHED",
        "method": method_used,
        "lead_id": lead.pk,
        "public_identifier": lead.public_identifier,
        "fields_populated": list(profile_data.keys()),
    }


def _apply_profile_data_to_lead(lead: Lead, profile_data: Dict[str, Any]):
    """Applies extracted factual fields to the Lead model and saves updated fields."""
    update_fields = []
    degree_pattern = r"^(?:·\s*)?(?:1st|2nd|3rd\+?)(?:\s*degree)?$"
    is_degree = lambda s: bool(re.match(degree_pattern, (s or "").strip(), re.IGNORECASE))

    if profile_data.get("first_name") and not lead.first_name:
        lead.first_name = profile_data["first_name"]
        update_fields.append("first_name")
    if profile_data.get("last_name") and not lead.last_name:
        lead.last_name = profile_data["last_name"]
        update_fields.append("last_name")
    if profile_data.get("headline") and not lead.headline:
        hl = profile_data["headline"].strip()
        if not is_degree(hl):
            lead.headline = hl
            update_fields.append("headline")
    if profile_data.get("current_title") and not lead.current_title:
        ct = profile_data["current_title"].strip()
        if not is_degree(ct):
            lead.current_title = ct
            update_fields.append("current_title")
    if profile_data.get("company") and not lead.company:
        lead.company = profile_data["company"]
        update_fields.append("company")
    
    loc = profile_data.get("location") or ""
    if loc and not lead.location:
        lead.location = loc
        update_fields.append("location")

    raw_cc = (profile_data.get("country_code") or "").strip().lower()
    if raw_cc and len(raw_cc) == 2 and not lead.country_code:
        lead.country_code = raw_cc
        update_fields.append("country_code")
    elif raw_cc and len(raw_cc) > 2 and not lead.location:
        lead.location = raw_cc
        update_fields.append("location")

    if update_fields:
        lead.save(update_fields=update_fields)
