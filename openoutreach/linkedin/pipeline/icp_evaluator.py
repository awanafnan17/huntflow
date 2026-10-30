# openoutreach/linkedin/pipeline/icp_evaluator.py
"""Structured ICP Quality Evaluator for Target Qualification with Strict Match Semantics."""
from __future__ import annotations
import logging
import re
from typing import Dict, Any, List

logger = logging.getLogger(__name__)

NON_TARGET_INDUSTRIES = [
    "government", "public sector", "health security", "hospital", "clinic",
    "holiday", "travel", "tourism", "leisure", "hotel", "resort",
    "construction", "building materials", "mining", "retail", "restaurant", "bistro",
    "real estate", "brokerage", "property investment"
]

TARGET_INDUSTRIES_DEFAULT = [
    "software", "information technology", "tech", "web", "design",
    "internet", "computer", "digital", "it services"
]

CONTROLLED_ALIAS_MAP: Dict[str, List[str]] = {
    "ceo": ["ceo", "chief executive officer"],
    "cto": ["cto", "chief technology officer"],
    "cfo": ["cfo", "chief financial officer"],
    "coo": ["coo", "chief operating officer"],
    "founder": ["founder", "co-founder", "cofounder", "co founder", "owner"],
    "head of product": ["head of product", "vp of product", "director of product", "product lead"],
}

LOCATION_REGION_MAP: Dict[str, List[str]] = {
    "united kingdom": ["united kingdom", "uk", "england", "scotland", "wales", "northern ireland", "london", "manchester", "birmingham", "leeds", "bristol", "urmston"],
    "uk": ["united kingdom", "uk", "england", "scotland", "wales", "northern ireland", "london", "manchester", "birmingham", "leeds", "bristol", "urmston"],
    "germany": ["germany", "deutschland", "berlin", "munich", "hamburg", "frankfurt", "cologne", "stuttgart"],
    "europe": ["united kingdom", "uk", "england", "scotland", "wales", "germany", "deutschland", "france", "czechia", "czech republic", "prague", "netherlands", "amsterdam", "spain", "italy", "sweden", "norway", "denmark", "poland", "latvia", "riga", "austria", "switzerland", "belgium", "portugal", "finland", "ireland"],
}

NON_EUROPEAN_LOCATIONS = [
    "united arab emirates", "dubai", "uae", "malaysia", "perak", "pakistan",
    "india", "united states", "us", "usa", "canada", "australia"
]

EXACT_ALIAS_MAP: Dict[str, List[str]] = {
    "ceo": ["ceo", "chief executive officer"],
    "cto": ["cto", "chief technology officer"],
    "cfo": ["cfo", "chief financial officer"],
    "coo": ["coo", "chief operating officer"],
    "founder": ["founder", "co-founder", "cofounder", "co founder"],
    "head of product": ["head of product"],
}

RELATED_ALIAS_MAP: Dict[str, List[str]] = {
    "founder": ["owner", "proprietor"],
    "head of product": ["vp of product", "director of product", "product lead", "vp product"],
}


def extract_title_seniority(title_lower: str) -> str:
    """Extract factual seniority tier from normalized job title string."""
    if not title_lower:
        return "UNKNOWN"
    if any(re.search(r"\b" + re.escape(w) + r"\b", title_lower) for w in ["founder", "co-founder", "cofounder", "owner"]):
        return "Executive"
    if any(re.search(r"\b" + re.escape(w) + r"\b", title_lower) for w in ["ceo", "cto", "cfo", "coo", "chief"]):
        return "C-Level"
    if any(re.search(r"\b" + re.escape(w) + r"\b", title_lower) for w in ["vp", "vice president"]):
        return "VP"
    if any(re.search(r"\b" + re.escape(w) + r"\b", title_lower) for w in ["head", "director"]):
        return "Head"
    if any(re.search(r"\b" + re.escape(w) + r"\b", title_lower) for w in ["manager", "mgmt"]):
        return "Manager"
    if any(re.search(r"\b" + re.escape(w) + r"\b", title_lower) for w in ["senior", "lead", "principal"]):
        return "Senior"
    if any(re.search(r"\b" + re.escape(w) + r"\b", title_lower) for w in ["engineer", "developer", "designer", "specialist", "analyst"]):
        return "Individual Contributor"
    return "UNKNOWN"


def evaluate_lead_icp_fit(profile: Dict[str, Any], campaign) -> Dict[str, Any]:
    """Evaluate factual profile attributes against Campaign ICP parameters.

    Returns deterministic criteria: MATCH, MISMATCH, UNKNOWN.
    Final Fit: STRONG, POSSIBLE, REJECT.
    """
    title = (profile.get("current_title") or profile.get("title") or profile.get("headline") or "").strip()
    company = (profile.get("company") or "").strip()
    industry = (profile.get("industry") or "").strip()
    location = (profile.get("location") or profile.get("country_code") or "").strip()
    summary = (profile.get("summary") or "").strip()
    company_size = (profile.get("company_size") or "").strip()

    # Retrieve campaign target ICP configuration
    raw_icp_titles = [t.strip().lower() for t in getattr(campaign, "target_job_titles", "").split(",") if t.strip()]
    raw_icp_seniorities = [s.strip().lower() for s in getattr(campaign, "target_seniority", "").split(",") if s.strip()]
    raw_icp_industries = [i.strip().lower() for i in getattr(campaign, "target_industries", "").split(",") if i.strip()]
    raw_icp_locations = [l.strip().lower() for l in getattr(campaign, "target_locations", "").split(",") if l.strip()]
    raw_icp_sizes = [sz.strip().lower() for sz in getattr(campaign, "target_company_size", "").split(",") if sz.strip()]

    reasons: List[str] = []

    # -------------------------------------------------------------
    # 1. TITLE MATCH EVALUATION
    # -------------------------------------------------------------
    title_lower = title.lower()
    title_match = "UNKNOWN"

    if not title:
        title_match = "UNKNOWN"
        reasons.append("Title missing or empty")
    elif not raw_icp_titles:
        title_match = "UNKNOWN"
        reasons.append(f"Title present ({title}) — No target titles configured")
    else:
        exact_target_role = None
        related_target_role = None

        for target_role in raw_icp_titles:
            # 1. Check exact aliases first
            exact_aliases = EXACT_ALIAS_MAP.get(target_role, [target_role])
            for alias in exact_aliases:
                pattern = r"\b" + re.escape(alias) + r"\b"
                if re.search(pattern, title_lower):
                    exact_target_role = target_role
                    break
            if exact_target_role:
                break

            # 2. Check related aliases second
            related_aliases = RELATED_ALIAS_MAP.get(target_role, [])
            for alias in related_aliases:
                pattern = r"\b" + re.escape(alias) + r"\b"
                if re.search(pattern, title_lower):
                    related_target_role = target_role
                    break
            if related_target_role:
                break

        if exact_target_role:
            title_match = "MATCH"
            reasons.append(f"Title MATCHES '{exact_target_role.title()}' ({title})")
        elif related_target_role:
            title_match = "POSSIBLE"
            reasons.append(f"Title RELATED to '{related_target_role.title()}' ({title})")
        else:
            title_match = "MISMATCH"
            reasons.append(f"Title MISMATCH: '{title}' does not match target roles ({', '.join(raw_icp_titles)})")

    # -------------------------------------------------------------
    # 2. SENIORITY EVALUATION
    # -------------------------------------------------------------
    extracted_seniority = extract_title_seniority(title_lower)
    seniority_match = "UNKNOWN"

    if not title:
        seniority_match = "UNKNOWN"
    elif not raw_icp_seniorities:
        seniority_match = "UNKNOWN"
    else:
        norm_ext_sen = extracted_seniority.lower()
        if any(target_sen in norm_ext_sen or norm_ext_sen in target_sen for target_sen in raw_icp_seniorities):
            seniority_match = "MATCH"
            reasons.append(f"Seniority MATCHES target level '{extracted_seniority}'")
        else:
            seniority_match = "MISMATCH"
            reasons.append(f"Seniority MISMATCH: '{extracted_seniority}' does not match target levels ({', '.join(raw_icp_seniorities)})")

    # -------------------------------------------------------------
    # -------------------------------------------------------------
    # 3. INDUSTRY & TECH DOMAIN EVALUATION
    # -------------------------------------------------------------
    headline = (profile.get("headline") or "").strip()
    summary = (profile.get("summary") or "").strip()
    combined_domain_text = f"{industry} {company} {headline} {summary}".lower()
    industry_match = "UNKNOWN"

    is_non_target = any(re.search(r"\b" + re.escape(neg) + r"\b", combined_domain_text) for neg in NON_TARGET_INDUSTRIES)
    if is_non_target:
        industry_match = "MISMATCH"
        reasons.append(f"Non-target industry/domain detected: {industry or company or headline}")
    elif raw_icp_industries:
        matched_target_domain = None
        for pos in raw_icp_industries:
            pos_norm = pos.lower().replace("-", " ")
            text_norm = combined_domain_text.replace("-", " ")
            pattern = r"\b" + re.escape(pos_norm) + r"\b"
            if re.search(pattern, text_norm):
                matched_target_domain = pos
                break
            if pos_norm == "tech" and re.search(r"\b\w*tech\w*\b", text_norm):
                matched_target_domain = pos
                break
            if pos_norm == "ai" and re.search(r"\b(?:genai|ai)\b", text_norm):
                matched_target_domain = pos
                break
        if matched_target_domain:
            industry_match = "MATCH"
            reasons.append(f"Domain MATCHES target: '{matched_target_domain}' (observed in profile text)")
        else:
            industry_match = "UNKNOWN"
            reasons.append(f"Domain UNKNOWN: no target domain ({', '.join(raw_icp_industries)}) detected in observed text")
    else:
        industry_match = "UNKNOWN"

    # -------------------------------------------------------------
    # 4. LOCATION EVALUATION (Canonical Global Location Targeting)
    # -------------------------------------------------------------
    from openoutreach.core.services.location_targeting import evaluate_location_match, LocationTargetingPolicy

    req_location = getattr(campaign, "require_location_match", True)

    if hasattr(campaign, "get_location_targeting_policy"):
        loc_policy = campaign.get_location_targeting_policy()
    else:
        loc_policy = LocationTargetingPolicy(included_locations=raw_icp_locations, channel="campaign")

    if not req_location:
        if not location:
            location_match = "NOT_REQUIRED"
            reasons.append("Location NOT REQUIRED by campaign")
        else:
            loc_res = evaluate_location_match(location, loc_policy)
            if loc_res.decision == "EXCLUDED":
                location_match = "MISMATCH"
                reasons.append(f"Location EXCLUDED: '{location}' ({'; '.join(loc_res.reasons)})")
            elif loc_res.decision == "MATCH":
                location_match = "MATCH"
                reasons.append(f"Location MATCHES target: '{location}' (rule: {loc_res.matched_rule})")
            else:
                location_match = "NOT_REQUIRED"
                reasons.append(f"Location not restricted by campaign: '{location}'")
    else:
        if not location:
            location_match = "UNKNOWN"
            reasons.append("Location UNKNOWN")
        elif not loc_policy.included_locations and not loc_policy.excluded_locations:
            location_match = "UNKNOWN"
        else:
            loc_res = evaluate_location_match(location, loc_policy)
            if loc_res.decision == "MATCH":
                location_match = "MATCH"
                reasons.append(f"Location MATCHES target: '{location}' (rule: {loc_res.matched_rule})")
            elif loc_res.decision in ["EXCLUDED", "NO_MATCH"]:
                location_match = "MISMATCH"
                reasons.append(f"Location MISMATCH: '{location}' (decision: {loc_res.decision}, reasons: {'; '.join(loc_res.reasons)})")
            else:
                location_match = "UNKNOWN"
                reasons.append(f"Location UNKNOWN / AMBIGUOUS: '{location}' (decision: {loc_res.decision})")

    # -------------------------------------------------------------
    # 5. COMPANY SIZE EVALUATION
    # -------------------------------------------------------------
    company_size_match = "UNKNOWN"
    if not company_size:
        company_size_match = "UNKNOWN"
        reasons.append("Company size UNKNOWN")
    elif not raw_icp_sizes:
        company_size_match = "UNKNOWN"
    else:
        if any(sz in company_size.lower() for sz in raw_icp_sizes):
            company_size_match = "MATCH"
            reasons.append(f"Company size MATCHES target: {company_size}")
        else:
            company_size_match = "MISMATCH"
            reasons.append(f"Company size MISMATCH: {company_size}")

    # -------------------------------------------------------------
    # 6. FINAL FIT DECISION
    # -------------------------------------------------------------
    loc_satisfied = (location_match in ("MATCH", "NOT_REQUIRED")) or (not req_location and location_match in ("MATCH", "UNKNOWN", "NOT_REQUIRED"))
    domain_satisfied = (industry_match == "MATCH") if raw_icp_industries else (industry_match in ("MATCH", "UNKNOWN"))

    if title_match == "MISMATCH" or location_match == "MISMATCH" or seniority_match == "MISMATCH" or industry_match == "MISMATCH":
        final_fit = "REJECT"
    elif title_match == "MATCH" and loc_satisfied and domain_satisfied and seniority_match in ("MATCH", "UNKNOWN") and company_size_match in ("MATCH", "UNKNOWN"):
        final_fit = "STRONG"
    else:
        final_fit = "POSSIBLE"

    return {
        "final_fit": final_fit,
        "title_match": title_match,
        "seniority_match": seniority_match,
        "industry_match": industry_match,
        "location_match": location_match,
        "company_size_match": company_size_match,
        "actual_title": title,
        "extracted_seniority": extracted_seniority,
        "actual_company": company,
        "actual_location": location,
        "reasons": reasons,
    }


def validate_campaign_icp_consistency(campaign) -> Dict[str, Any]:
    """Validate Campaign ICP target configuration for contradictions and inconsistencies."""
    raw_icp_titles = [t.strip().lower() for t in getattr(campaign, "target_job_titles", "").split(",") if t.strip()]
    raw_icp_seniorities = [s.strip().lower() for s in getattr(campaign, "target_seniority", "").split(",") if s.strip()]

    req_title = getattr(campaign, "require_title_match", True)
    req_location = getattr(campaign, "require_location_match", True)

    warnings = []

    if req_title and not raw_icp_titles:
        warnings.append("Target job titles not configured.")
    if req_location and not getattr(campaign, "target_locations", "").strip():
        warnings.append("Target locations not configured.")

    if raw_icp_titles and raw_icp_seniorities:
        for title in raw_icp_titles:
            implied = extract_title_seniority(title)
            if implied != "UNKNOWN":
                implied_norm = implied.lower()
                matched = any(sen in implied_norm or implied_norm in sen for sen in raw_icp_seniorities)
                if not matched:
                    warnings.append(
                        f"Seniority Conflict: Target title '{title.title()}' implies seniority '{implied}', "
                        f"but '{implied}' is not included in Target Seniority ({', '.join(raw_icp_seniorities)})."
                    )

    status = "WARNING" if warnings else "VALID"

    return {
        "status": status,
        "warnings": warnings,
        "is_valid": len(warnings) == 0,
        "titles_configured": bool(raw_icp_titles),
        "seniority_configured": bool(raw_icp_seniorities),
        "locations_configured": bool(getattr(campaign, "target_locations", "").strip()),
    }
