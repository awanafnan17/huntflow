# openoutreach/linkedin/services/discovery_service.py
"""Authoritative Production Target Discovery Service."""
from __future__ import annotations

import logging
import re
from typing import Dict, Any, List, Optional

from django.db import transaction

from openoutreach.crm.models.deal import Deal, DealState
from openoutreach.crm.models.lead import Lead
from openoutreach.linkedin.models import SearchKeyword
from openoutreach.linkedin.pipeline.search_keywords import generate_search_keywords
from openoutreach.linkedin.pipeline.icp_evaluator import evaluate_lead_icp_fit
from openoutreach.linkedin.session_manager import open_authenticated_linkedin_context

logger = logging.getLogger(__name__)


def discover_campaign_targets(campaign, limit: int = 10, session: Optional[Any] = None) -> Dict[str, Any]:
    """Authoritative production discovery entrypoint used by both Portal and Daemon."""
    from linkedin_cli.actions.search import search_people
    from openoutreach.core.models import WorkspaceAutomationSettings
    from openoutreach.linkedin.db.leads import discover_and_enrich, lead_exists
    from openoutreach.linkedin.models import ProviderOperationType
    from openoutreach.linkedin.services.account_governor import (
        check_and_record_safety_warning,
        is_account_safe_for_provider_read,
    )
    from openoutreach.linkedin.services.evidence_cache import (
        JOB_SEARCH_RESULTS_TTL,
        get_cached_evidence,
        record_provider_read,
        set_cached_evidence,
    )

    ws = getattr(campaign, "workspace", None)

    # 1. Exact Campaign LinkedInProfile Resolution (Fail-Closed, No Unscoped Fallbacks)
    profile_obj = getattr(campaign, "linkedin_profile", None)
    if not profile_obj:
        logger.error("Discovery blocked: Campaign '%s' has no explicitly bound LinkedInProfile.", getattr(campaign, "name", campaign))
        return {
            "status": "blocked",
            "reason": "missing_campaign_linkedin_profile",
            "keyword": None,
            "profiles_found": 0,
            "leads_created": 0,
            "deals_created": 0,
            "strong_fits": 0,
            "needs_review": 0,
            "rejected": 0,
            "session_valid": False,
            "challenge": False,
        }

    # 2. Workspace & Provider Tenancy Verification
    if ws and profile_obj.workspace_id != ws.id:
        logger.error(
            "Discovery blocked: Campaign workspace (%s) does not match LinkedInProfile workspace (%s).",
            ws.id,
            profile_obj.workspace_id,
        )
        return {
            "status": "blocked",
            "reason": "cross_workspace_profile_mismatch",
            "keyword": None,
            "profiles_found": 0,
            "leads_created": 0,
            "deals_created": 0,
            "strong_fits": 0,
            "needs_review": 0,
            "rejected": 0,
            "session_valid": False,
            "challenge": False,
        }

    # 3. Provider Active & Session Validity Check
    if not profile_obj.active or profile_obj.session_status in ["checkpoint", "restricted", "needs_reauth", "invalid", "expired"]:
        logger.warning(
            "Discovery blocked: LinkedInProfile %s is not active/valid (status=%s, active=%s).",
            profile_obj.id,
            profile_obj.session_status,
            profile_obj.active,
        )
        return {
            "status": "blocked",
            "reason": f"invalid_provider_session_status_{profile_obj.session_status}",
            "keyword": None,
            "profiles_found": 0,
            "leads_created": 0,
            "deals_created": 0,
            "strong_fits": 0,
            "needs_review": 0,
            "rejected": 0,
            "session_valid": False,
            "challenge": False,
        }

    # 4. Account Safety Governor Check (Exact Account Scoped)
    is_safe, safety_reason = is_account_safe_for_provider_read(workspace=ws, linkedin_profile=profile_obj)
    if not is_safe:
        logger.critical("Prospect discovery halted by governor for profile %s: %s", profile_obj.id, safety_reason)
        return {
            "status": "blocked",
            "reason": safety_reason,
            "keyword": None,
            "profiles_found": 0,
            "leads_created": 0,
            "deals_created": 0,
            "strong_fits": 0,
            "needs_review": 0,
            "rejected": 0,
            "session_valid": False,
            "challenge": False,
        }

    # 5. Demand-Driven Buffer Check: Do not search if READY_TO_CONNECT inventory is sufficient
    auto_st = WorkspaceAutomationSettings.get_for_workspace(ws) if ws else None
    high_water = getattr(auto_st, "connection_inventory_high_water", 20) or 20
    ready_count = Deal.objects.filter(campaign=campaign, state=DealState.READY_TO_CONNECT, lead__disqualified=False).count()
    if ready_count >= high_water:
        logger.info(
            "Campaign '%s' has %d READY_TO_CONNECT prospects (high-water limit %d). Demand-driven discovery skipping live search.",
            campaign.name,
            ready_count,
            high_water,
        )
        return {
            "status": "skipped",
            "reason": "inventory_at_or_above_high_water",
            "keyword": None,
            "profiles_found": 0,
            "leads_created": 0,
            "deals_created": 0,
            "strong_fits": 0,
            "needs_review": 0,
            "rejected": 0,
            "session_valid": True,
            "challenge": False,
        }

    # 3. Select or generate an unused SearchKeyword
    unused_kw_obj = SearchKeyword.objects.filter(campaign=campaign, used=False).order_by("-id").first()
    if not unused_kw_obj:
        used_list = list(SearchKeyword.objects.filter(campaign=campaign, used=True).values_list("keyword", flat=True))
        fresh_keywords = generate_search_keywords(
            campaign=campaign,
            product_docs=campaign.product_docs or f"Target Audience: {campaign.target_audience_definition}",
            campaign_objective=campaign.campaign_objective or f"Outreach for {campaign.name}",
            n_keywords=5,
            exclude_keywords=used_list if used_list else None,
        )
        if fresh_keywords:
            objs = [SearchKeyword(campaign=campaign, keyword=k) for k in fresh_keywords]
            SearchKeyword.objects.bulk_create(objs, ignore_conflicts=True)
            unused_kw_obj = SearchKeyword.objects.filter(campaign=campaign, used=False).order_by("-id").first()

    if not unused_kw_obj:
        return {
            "status": "error",
            "message": "Failed to obtain search keywords for campaign.",
            "keyword": None,
            "profiles_found": 0,
            "leads_created": 0,
            "deals_created": 0,
            "strong_fits": 0,
            "needs_review": 0,
            "rejected": 0,
        }

    keyword = unused_kw_obj.keyword
    logger.info("Executing authoritative discovery for campaign '%s' using keyword '%s'", campaign.name, keyword)

    # 4. Run search inside authenticated Playwright session with safety check & ledger recording
    profiles_raw = []
    session_status_valid = True
    challenge_encountered = False

    def _execute_search(active_sess):
        active_sess.campaign = campaign
        if getattr(active_sess, "page", None):
            check_and_record_safety_warning(active_sess.page, workspace=ws, linkedin_profile=profile_obj)

        res = search_people(active_sess, keyword=keyword, page=1)
        if isinstance(res, dict):
            raw = res.get("profiles", [])
        elif isinstance(res, list):
            raw = res
        else:
            raw = []

        # Record provider search in ledger
        record_provider_read(
            operation_type=ProviderOperationType.PROSPECT_SEARCH,
            resource_identifier=f"{campaign.id}:{keyword}",
            cache_hit=False,
            linkedin_profile=profile_obj,
            workspace=ws,
            success=True,
            read_cost=2,
            metadata={"profiles_found": len(raw)},
        )
        return raw

    try:
        if session and getattr(session, "page", None):
            profiles_raw = _execute_search(session)
        else:
            with open_authenticated_linkedin_context(linkedin_profile_id=profile_obj.id) as (page, context, browser, session_res):
                from openoutreach.linkedin.browser.session import AccountSession
                local_sess = AccountSession(profile_obj)
                local_sess.page = page
                local_sess.context = context
                local_sess.browser = browser
                profiles_raw = _execute_search(local_sess)
    except Exception as e:
        logger.error("Authoritative discovery session error: %s", e, exc_info=True)
        session_status_valid = False
        record_provider_read(
            operation_type=ProviderOperationType.PROSPECT_SEARCH,
            resource_identifier=f"{campaign.id}:{keyword}",
            cache_hit=False,
            linkedin_profile=profile_obj,
            workspace=ws,
            success=False,
            read_cost=2,
            metadata={"error": str(e)},
        )

    # 5. Mark keyword as used
    unused_kw_obj.used = True
    unused_kw_obj.save(update_fields=["used"])

    # 6. Process discovered leads: Early Local ICP Filtering Before Deep Enrichment
    leads_created = 0
    deals_created = 0
    duplicates_count = 0
    strong_fits = 0
    needs_review = 0
    rejected = 0

    from linkedin_cli.url_utils import public_id_to_url, url_to_public_id
    from openoutreach.crm.models.deal import Outcome

    for item in profiles_raw:
        if not isinstance(item, dict):
            continue
        url = item.get("url")
        if not url:
            continue

        pid = item.get("public_identifier") or url_to_public_id(url)
        if not pid:
            continue

        clean_url = public_id_to_url(pid)

        # Build candidate card facts from search result card
        name_parts = (item.get("name") or "").split()
        first_name = name_parts[0] if name_parts else ""
        last_name = " ".join(name_parts[1:]) if len(name_parts) > 1 else ""
        
        raw_title = (item.get("headline") or item.get("title") or "").strip()
        degree_pattern = r"^(?:·\s*)?(?:1st|2nd|3rd\+?)(?:\s*degree)?$"
        is_degree = bool(re.match(degree_pattern, raw_title, re.IGNORECASE))
        
        title = "" if is_degree else raw_title
        company = item.get("current_company") or item.get("company") or ""
        location = item.get("location") or ""
        
        # Only assign 2-letter ISO country codes to country_code; store location in location field
        raw_cc = (item.get("country_code") or "").strip().lower()
        country_code = raw_cc if len(raw_cc) == 2 else ""

        # Find or create Lead locally
        lead = Lead.objects.filter(public_identifier=pid).first()
        if not lead:
            lead = Lead.objects.create(
                public_identifier=pid,
                linkedin_url=clean_url,
                workspace=campaign.workspace,
                first_name=first_name,
                last_name=last_name,
                headline=title,
                current_title=title,
                company=company,
                location=location,
                country_code=country_code,
            )
            leads_created += 1
        else:
            if lead.workspace is None and campaign.workspace:
                lead.workspace = campaign.workspace
                lead.save(update_fields=["workspace"])

        # Check existing deal for this campaign
        existing_deal = Deal.objects.filter(lead=lead, campaign=campaign).first()
        if existing_deal:
            duplicates_count += 1
            continue

        # ── Early Local ICP Evaluation ──
        profile_dict = lead.to_profile_dict()
        icp_eval = evaluate_lead_icp_fit(profile_dict, campaign)
        final_fit = icp_eval["final_fit"]
        reason_str = " | ".join(icp_eval.get("reasons", []))

        if final_fit == "STRONG":
            deal_state = DealState.READY_TO_CONNECT
            deal_outcome = ""
            strong_fits += 1
        elif final_fit == "POSSIBLE":
            deal_state = DealState.QUALIFIED
            deal_outcome = ""
            needs_review += 1
        else:
            # Early Cheap Rejection: Reject locally without visiting full profile
            deal_state = DealState.FAILED
            deal_outcome = Outcome.WRONG_FIT
            rejected += 1

        deal = Deal.objects.create(
            lead=lead,
            campaign=campaign,
            state=deal_state,
            outcome=deal_outcome,
            reason=reason_str,
        )
        deals_created += 1

    return {
        "status": "success",
        "keyword": keyword,
        "keyword_id": unused_kw_obj.id,
        "profiles_found": len(profiles_raw),
        "leads_created": leads_created,
        "deals_created": deals_created,
        "duplicates": duplicates_count,
        "strong_fits": strong_fits,
        "needs_review": needs_review,
        "rejected": rejected,
        "session_valid": session_status_valid,
        "challenge": challenge_encountered,
    }

