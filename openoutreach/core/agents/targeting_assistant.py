# openoutreach/core/agents/targeting_assistant.py
"""
AI Targeting Assistant & Intent Conversion Agent for HuntFlow.

Converts operator natural language into canonical structured configurations:
1. Prospect Audience ICP (titles, seniorities, industries, locations, sizes, exclusions, scoring).
2. Job Search Targeting (job titles, skills, locations, remote/relocation, Easy Apply priority, daily target 50).
3. Campaign Disambiguation & Dynamic Campaign Creation.
4. Contradiction & Conflict Detection.
5. Explainable Prospect Scoring (0-100 breakdown with hard exclusion priority).
6. Read-only Match & Job Previews.
7. Targeting Versioning & Rollback.
8. Live Canonical Status and Blockers Lookup.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Dict, Any, List, Optional, Tuple
from django.utils import timezone
from django.db import transaction

from openoutreach.core.models import (
    Workspace,
    Campaign,
    CampaignPurpose,
    WorkspaceAutomationSettings,
    TargetingConversation,
    TargetingMessage,
    TargetingConfigVersion,
)

logger = logging.getLogger(__name__)

# Standard Title Normalization Mapping
TITLE_NORMALIZATION_MAP = {
    "founders": ["Founder", "Co-Founder"],
    "founder": ["Founder", "Co-Founder"],
    "co-founder": ["Co-Founder", "Founder"],
    "co-founders": ["Co-Founder", "Founder"],
    "ctos": ["CTO", "Chief Technology Officer"],
    "cto": ["CTO", "Chief Technology Officer"],
    "ceos": ["CEO", "Chief Executive Officer"],
    "ceo": ["CEO", "Chief Executive Officer"],
    "engineering managers": ["Engineering Manager", "Software Engineering Manager", "Head of Engineering"],
    "engineering manager": ["Engineering Manager", "Software Engineering Manager"],
    "heads of ai": ["Head of AI", "Director of AI", "VP of AI"],
    "head of ai": ["Head of AI", "Director of AI", "VP of AI"],
    "vp of engineering": ["VP of Engineering", "Vice President of Engineering", "Head of Engineering"],
    "vp engineering": ["VP of Engineering", "Vice President of Engineering"],
    "technical directors": ["Technical Director", "Director of Technology"],
    "technical director": ["Technical Director", "Director of Technology"],
    "exploration managers": ["Exploration Manager", "Head of Exploration", "VP Exploration"],
    "exploration manager": ["Exploration Manager", "Head of Exploration"],
    "directors": ["Director", "Managing Director"],
    "python developers": ["Python Developer", "Python Engineer", "Backend Engineer"],
    "ai engineers": ["AI Engineer", "Machine Learning Engineer", "AI Application Engineer"],
}


def normalize_title_list(raw_titles: List[str]) -> List[str]:
    """Expands/normalizes natural language title phrases without excessive broadness."""
    result = []
    seen = set()
    for t in raw_titles:
        t_clean = t.strip()
        t_lower = t_clean.lower()
        if not t_clean:
            continue
        if t_lower in TITLE_NORMALIZATION_MAP:
            expanded = TITLE_NORMALIZATION_MAP[t_lower]
            for exp in expanded:
                if exp.lower() not in seen:
                    seen.add(exp.lower())
                    result.append(exp)
        else:
            if t_lower not in seen:
                seen.add(t_lower)
                result.append(t_clean.title() if len(t_clean) > 3 else t_clean.upper())
    return result


def detect_contradictions(config: Dict[str, Any], config_type: str = "prospect_icp") -> List[str]:
    """Detects logical contradictions and conflicts within structured targeting."""
    contradictions = []

    if config_type == "prospect_icp":
        target_locations = [loc.strip().lower() for loc in config.get("target_locations", []) if loc.strip()]
        excluded_locations = [loc.strip().lower() for loc in config.get("excluded_locations", []) if loc.strip()]
        
        # 1. Location intersection conflict
        overlap_loc = set(target_locations).intersection(set(excluded_locations))
        if overlap_loc:
            contradictions.append(
                f"Location Conflict: '{', '.join(overlap_loc).title()}' is listed in both Target Locations and Excluded Locations."
            )

        # 2. Title intersection conflict
        target_titles = [t.strip().lower() for t in config.get("target_titles", []) if t.strip()]
        excluded_titles = [t.strip().lower() for t in config.get("excluded_titles", []) if t.strip()]
        overlap_titles = set(target_titles).intersection(set(excluded_titles))
        if overlap_titles:
            contradictions.append(
                f"Title Conflict: '{', '.join(overlap_titles).title()}' is listed in both Target Titles and Excluded Titles."
            )

        # 3. Industry intersection conflict
        target_industries = [i.strip().lower() for i in config.get("target_industries", []) if i.strip()]
        excluded_industries = [i.strip().lower() for i in config.get("excluded_industries", []) if i.strip()]
        overlap_ind = set(target_industries).intersection(set(excluded_industries))
        if overlap_ind:
            contradictions.append(
                f"Industry Conflict: '{', '.join(overlap_ind).title()}' is listed in both Target Industries and Excluded Industries."
            )

        # 4. Company size conflict
        size_min = config.get("company_size_min")
        size_max = config.get("company_size_max")
        if size_min is not None and size_max is not None and size_min > size_max:
            contradictions.append(
                f"Company Size Conflict: Minimum size ({size_min}) is greater than maximum size ({size_max})."
            )

    elif config_type == "job_target":
        target_locations = [loc.strip().lower() for loc in config.get("target_locations", []) if loc.strip()]
        excluded_locations = [loc.strip().lower() for loc in config.get("excluded_locations", []) if loc.strip()]
        overlap_loc = set(target_locations).intersection(set(excluded_locations))
        if overlap_loc:
            contradictions.append(
                f"Location Conflict: '{', '.join(overlap_loc).title()}' is listed in both Target Job Locations and Excluded Locations."
            )

        min_match = config.get("minimum_match_score", 70)
        if min_match < 70:
            contradictions.append(
                f"Match Threshold Conflict: Configured match score ({min_match}%) is below the mandatory technical minimum (70%)."
            )

    return contradictions


class TargetingAssistant:
    """Core intelligence engine for natural language targeting and intent translation."""

    @classmethod
    def call_llm(cls, prompt: str, system_prompt: str = "") -> Optional[str]:
        """Calls Ollama (qwen3:1.7b-q4_K_M) with fallback."""
        from openoutreach.career.services.ats_llm_engine import call_ollama_raw, is_ollama_available
        if not is_ollama_available():
            return None
        full_prompt = f"{system_prompt}\n\nUser Request: {prompt}\n\nOutput Valid JSON:"
        try:
            return call_ollama_raw(full_prompt, timeout=15.0)
        except Exception as e:
            logger.warning("Ollama targeting inference failed: %s", e)
            return None

    @classmethod
    def parse_natural_language_intent(
        cls,
        prompt: str,
        workspace: Optional[Workspace] = None,
        campaign: Optional[Campaign] = None,
        conversation_history: Optional[List[Dict[str, str]]] = None,
    ) -> Dict[str, Any]:
        """
        Parses operator natural language into a canonical structured representation.
        """
        prompt_clean = prompt.strip()
        prompt_lower = prompt_clean.lower()

        # 1. Check for Status Queries (Must be checked FIRST before any modification parser)
        if any(q in prompt_lower for q in [
            "show me my current job targeting",
            "show me current job targeting",
            "current job targeting",
            "today's application progress",
            "todays application progress",
            "application progress",
            "how are my jobs going",
            "what jobs are you targeting",
            "job status",
            "jobs status",
            "job progress",
        ]):
            return cls._handle_job_status_query(workspace)

        elif any(q in prompt_lower for q in [
            "who are you targeting",
            "who are we targeting",
            "show active targeting",
            "current targeting",
            "what audience",
            "targeting status",
            "what's active",
            "what is active",
            "current prospect targeting",
        ]):
            return cls._handle_active_targeting_query(workspace, campaign)

        elif any(q in prompt_lower for q in ["how many connections today", "connection count", "connections today"]):
            return cls._handle_counter_query(workspace, "connections")

        elif any(q in prompt_lower for q in ["how many jobs", "jobs applied", "job applications today", "jobs today"]):
            return cls._handle_counter_query(workspace, "jobs")

        elif any(q in prompt_lower for q in ["why haven't you reached 50", "why haven't you reached", "why not 50"]):
            return cls._handle_job_shortfall_query(workspace)

        elif any(q in prompt_lower for q in ["undo", "rollback", "restore previous targeting", "undo my last targeting change"]):
            return cls._handle_rollback_intent(workspace, campaign)

        elif any(q in prompt_lower for q in ["show me 10 people", "show me 10 prospects", "preview people", "preview audience"]):
            return cls._handle_preview_intent(workspace, campaign, "prospects")

        elif any(q in prompt_lower for q in ["show me examples of jobs", "preview jobs", "sample jobs"]):
            return cls._handle_preview_intent(workspace, campaign, "jobs")

        # 2. Check for Campaign Intent & Campaign Routing
        intent_type = "prospect_icp"
        if any(w in prompt_lower for w in ["apply to", "search for jobs", "find jobs", "job search", "easy apply jobs", "salary requirement"]):
            intent_type = "job_target"
        elif any(w in prompt_lower for w in ["job", "jobs", "easy apply", "apply", "sponsorship", "relocation"]) and not any(w in prompt_lower for w in ["founder", "founders", "cto", "ctos", "executive", "clients", "leads"]):
            intent_type = "job_target"

        if "create a new campaign" in prompt_lower or "new campaign targeting" in prompt_lower:
            intent_type = "create_campaign"

        # Disambiguate Campaign if not explicitly provided
        target_campaign = campaign
        if not target_campaign and workspace:
            if any(k in prompt_lower for k in ["mining", "geosentry", "mineral", "exploration"]):
                target_campaign = Campaign.objects.filter(workspace=workspace, name__icontains="Mining").first() or Campaign.objects.filter(workspace=workspace, name__icontains="GeoSentry").first()
            elif any(k in prompt_lower for k in ["freelance", "client", "clients", "automation"]):
                target_campaign = Campaign.objects.filter(workspace=workspace, name__icontains="Freelance").first() or Campaign.objects.filter(workspace=workspace, name__icontains="Client").first()
            elif intent_type == "job_target":
                target_campaign = Campaign.objects.filter(workspace=workspace, name__icontains="Job").first()
            
            if not target_campaign:
                target_campaign = Campaign.objects.filter(workspace=workspace, is_active=True).first() or Campaign.objects.filter(workspace=workspace).first()

        # 3. Parse Fields using Hybrid Extraction (Deterministic + LLM refinement)
        if intent_type == "job_target":
            structured_data = cls._parse_job_targeting(prompt_clean)
        elif intent_type == "create_campaign":
            structured_data = cls._parse_new_campaign(prompt_clean, workspace)
        else:
            structured_data = cls._parse_prospect_icp(prompt_clean, target_campaign)

        # 4. Check for Contradictions
        contradictions = detect_contradictions(structured_data, config_type=intent_type)
        structured_data["has_contradiction"] = bool(contradictions)
        structured_data["contradictions"] = contradictions

        # 5. Build Human-Readable Preview
        preview_text = cls.generate_preview_text(structured_data, intent_type, target_campaign)
        structured_data["preview_text"] = preview_text
        structured_data["intent_type"] = intent_type
        structured_data["campaign_id"] = target_campaign.id if target_campaign else None
        structured_data["campaign_name"] = target_campaign.name if target_campaign else "New / Global Campaign"

        return structured_data

    @classmethod
    def _parse_prospect_icp(cls, text: str, campaign: Optional[Campaign] = None) -> Dict[str, Any]:
        """Extracts prospect ICP fields from natural language text with exact intent fidelity."""
        text_lower = text.lower()

        # Detect Mode
        mode = "REPLACE_EXPLICIT"
        if re.search(r'^(?:add|also\s+include|also\s+target|plus)\b', text_lower.strip()):
            mode = "ADD"
        elif re.search(r'^(?:remove|drop|delete)\b', text_lower.strip()) or re.search(r'\b(?:remove|exclude|drop|delete)\s+(?:uk|united\s+kingdom|australia|us|usa|united\s+states|canada|germany|europe|from\s+locations?|from\s+countries?)\b', text_lower):
            mode = "REMOVE"
        elif re.search(r'^(?:only|just)\b', text_lower.strip()) or "only target" in text_lower or "narrow down" in text_lower:
            mode = "NARROW"
        elif re.search(r'^(?:broaden|expand)\b', text_lower.strip()) or "broaden" in text_lower:
            mode = "BROADEN"

        # Strict word boundary title matching
        raw_title_matches = [
            (r'\b(founder|founders)\b', ["Founder", "Co-Founder"]),
            (r'\b(co-founder|co-founders|cofounder|cofounders)\b', ["Co-Founder"]),
            (r'\b(cto|ctos)\b', ["CTO", "Chief Technology Officer"]),
            (r'\b(chief\s+technology\s+officer)\b', ["Chief Technology Officer", "CTO"]),
            (r'\b(ceo|ceos)\b', ["CEO", "Chief Executive Officer"]),
            (r'\b(chief\s+executive\s+officer)\b', ["Chief Executive Officer", "CEO"]),
            (r'\b(engineering\s+manager|engineering\s+managers)\b', ["Engineering Manager", "Software Engineering Manager"]),
            (r'\b(head\s+of\s+ai|heads\s+of\s+ai|director\s+of\s+ai)\b', ["Head of AI", "Director of AI"]),
            (r'\b(head\s+of\s+engineering|heads\s+of\s+engineering)\b', ["Head of Engineering", "VP of Engineering"]),
            (r'\b(vp\s+of\s+engineering|vps\s+of\s+engineering|vp\s+engineering)\b', ["VP of Engineering", "Vice President of Engineering"]),
            (r'\b(technical\s+director|technical\s+directors)\b', ["Technical Director"]),
            (r'\b(exploration\s+manager|exploration\s+managers)\b', ["Exploration Manager", "Head of Exploration"]),
        ]
        requested_titles = []
        for pat, val in raw_title_matches:
            if re.search(pat, text_lower):
                for v in val:
                    if v not in requested_titles:
                        requested_titles.append(v)

        # Excluded Titles
        excluded_titles = []
        if re.search(r'\b(recruiter|recruiters|technical\s+recruiter)\b', text_lower):
            excluded_titles.extend(["Recruiter", "Senior Recruiter", "Head of Talent", "Technical Recruiter"])
        if re.search(r'\b(student|students|intern|interns)\b', text_lower):
            excluded_titles.extend(["Student", "Intern", "Graduate Student"])
        if re.search(r'\b(agency|agencies|staffing)\b', text_lower):
            excluded_titles.extend(["Agency Owner", "Staffing Consultant"])

        # Check explicit title exclusions in prompt
        exc_match = re.search(r'(?:exclude|avoid|no|not)\s+([a-zA-Z\s,]+?)(?:\.|\n|$|prefer|target|with|for)', text_lower)
        if exc_match:
            exc_phrase = exc_match.group(1)
            for ex in ["recruiters", "recruiter", "students", "student", "agencies", "agency", "interns", "staffing"]:
                if ex in exc_phrase:
                    if "recruit" in ex:
                        excluded_titles.extend(["Recruiter", "Senior Recruiter"])
                    elif "student" in ex or "intern" in ex:
                        excluded_titles.extend(["Student", "Intern"])
                    elif "agenc" in ex or "staffing" in ex:
                        excluded_titles.extend(["Agency Owner", "Staffing Consultant"])

        # Industries
        industries = []
        if re.search(r'\b(ai|artificial\s+intelligence|machine\s+learning)\b', text_lower):
            industries.append("Artificial Intelligence")
        if re.search(r'\b(saas|software|b2b)\b', text_lower):
            industries.append("B2B SaaS")
        if re.search(r'\b(mining|mineral|exploration)\b', text_lower):
            industries.append("Mining Technology")

        # Excluded Industries
        excluded_industries = []
        if re.search(r'\b(recruiting|staffing|agency)\b', text_lower):
            excluded_industries.extend(["Recruiting & Staffing", "Staffing Agencies"])
        if re.search(r'\b(crypto|cryptocurrency|gambling)\b', text_lower):
            excluded_industries.extend(["Cryptocurrency", "Gambling"])

        # Location keywords with strict word boundaries
        loc_patterns = [
            (r'\b(united\s+states|usa|u\.s\.a\.|u\.s\.)\b', "United States"),
            (r'\b(us)\b', "United States"),
            (r'\b(united\s+kingdom|uk|u\.k\.|england|london|great\s+britain)\b', "United Kingdom"),
            (r'\b(australia|sydney|melbourne|brisbane|perth)\b', "Australia"),
            (r'\b(canada|toronto|vancouver|montreal|calgary|ottawa)\b', "Canada"),
            (r'\b(germany|berlin|munich|frankfurt)\b', "Germany"),
            (r'\b(europe)\b', "Europe"),
            (r'\b(new\s+zealand|auckland|wellington)\b', "New Zealand"),
        ]

        extracted_locations = []
        for pat, loc_name in loc_patterns:
            if re.search(pat, text_lower):
                if loc_name not in extracted_locations:
                    extracted_locations.append(loc_name)

        # Excluded Locations
        excluded_locations = []
        loc_exc_match = re.search(r'(?:exclude|avoid|no|not in)\s+(?:locations?|countries?|cities?)?\s*[:\s]+([a-zA-Z\s,]+?)(?:\.|\n|$|prefer|target|with|for)', text_lower)
        if loc_exc_match:
            exc_part = loc_exc_match.group(1)
            for loc in ["germany", "france", "canada", "united states", "india", "pakistan", "china", "uk", "australia"]:
                if loc in exc_part:
                    excluded_locations.append(loc.title())

        # Resolve Existing vs New based on Change Mode
        existing_locations = []
        existing_titles = []
        if campaign:
            if campaign.target_locations:
                existing_locations = [l.strip() for l in campaign.target_locations.split(",") if l.strip()]
            if campaign.target_job_titles:
                existing_titles = [t.strip() for t in campaign.target_job_titles.split(",") if t.strip()]

        if mode == "ADD":
            locations = list(dict.fromkeys(existing_locations + extracted_locations))
            titles = list(dict.fromkeys(existing_titles + requested_titles))
        elif mode == "REMOVE":
            locations = [l for l in existing_locations if l.lower() not in [r.lower() for r in extracted_locations]]
            titles = [t for t in existing_titles if t.lower() not in [r.lower() for r in requested_titles]]
        elif mode in ["NARROW", "REPLACE_EXPLICIT"] and extracted_locations:
            locations = extracted_locations
            titles = requested_titles if requested_titles else (existing_titles or ["Founder", "CTO"])
        elif extracted_locations:
            locations = extracted_locations
            titles = requested_titles if requested_titles else (existing_titles or ["Founder", "CTO"])
        else:
            locations = existing_locations if existing_locations else ["Australia", "United Kingdom", "Canada"]
            titles = requested_titles if requested_titles else (existing_titles or ["Founder", "CTO", "Engineering Manager"])

        if not industries and campaign and campaign.target_industries:
            industries = [i.strip() for i in campaign.target_industries.split(",") if i.strip()]

        # Company Size
        size_min, size_max = None, None
        size_match = re.search(r'(\d+)\s*[-–to]+\s*(\d+)\s*(?:employees|people|emp)?', text_lower)
        if size_match:
            size_min = int(size_match.group(1))
            size_max = int(size_match.group(2))
        elif "under 200" in text_lower or "<200" in text_lower:
            size_min, size_max = 1, 200
        elif "under 500" in text_lower or "<500" in text_lower:
            size_min, size_max = 1, 500
        elif "10-500" in text_lower:
            size_min, size_max = 10, 500

        # Score Threshold & Policy
        threshold = 75
        if "strict" in text_lower or "very strong" in text_lower or "highest quality" in text_lower:
            threshold = 85
        elif "very broad" in text_lower or "broad" in text_lower:
            threshold = 65
        elif "normal" in text_lower or "moderate" in text_lower:
            threshold = 75

        # Campaign Purpose
        purpose = "freelance_clients"
        if "mining" in text_lower or "geosentry" in text_lower:
            purpose = "geosentry_mining"
        elif "job" in text_lower or "hire me" in text_lower or "career" in text_lower:
            purpose = "freelance_clients" if "freelance" in text_lower else "job_outreach"
        elif "scholarship" in text_lower or "masters" in text_lower:
            purpose = "masters_scholarship"

        # Field Provenance Tracking
        provenance = {}
        for loc in locations:
            provenance[f"location:{loc}"] = "EXPLICIT_OPERATOR" if loc in extracted_locations else "EXISTING_CONFIG"
        for t in titles:
            provenance[f"title:{t}"] = "EXPLICIT_OPERATOR" if t in requested_titles else "NORMALIZED_VARIANT"

        return {
            "target_titles": normalize_title_list(titles) if titles else ["Founder", "CTO"],
            "excluded_titles": list(set(excluded_titles)),
            "target_seniority": ["Executive", "C-Level", "Director", "Head", "Manager"],
            "target_industries": industries if industries else ["Artificial Intelligence", "B2B SaaS"],
            "excluded_industries": list(set(excluded_industries)),
            "target_locations": locations,
            "excluded_locations": list(set(excluded_locations)),
            "company_size_min": size_min or 10,
            "company_size_max": size_max or 500,
            "prospect_score_threshold": threshold,
            "campaign_purpose": purpose,
            "connection_goal": "VALUE_OUTREACH",
            "change_mode": mode,
            "provenance": provenance,
        }

    @classmethod
    def _parse_job_targeting(cls, text: str) -> Dict[str, Any]:
        """Extracts job search targeting fields from natural language text."""
        text_lower = text.lower()

        # Job Titles
        target_jobs = []
        if "python" in text_lower or "backend" in text_lower:
            target_jobs.extend(["Python Backend Developer", "Backend Software Engineer", "Python Developer"])
        if "ai" in text_lower or "automation" in text_lower:
            target_jobs.extend(["AI Engineer", "AI Automation Engineer", "ML Platform Engineer"])
        if "full-stack" in text_lower or "full stack" in text_lower:
            target_jobs.extend(["Full Stack Software Engineer", "Full Stack Developer"])
        if "django" in text_lower or "fastapi" in text_lower:
            target_jobs.extend(["Django Developer", "FastAPI Developer"])

        if not target_jobs:
            target_jobs = ["Python Backend Developer", "AI Engineer", "Backend Software Engineer", "Full Stack Engineer"]

        # Locations with word boundary regex
        locations = []
        loc_patterns = [
            (r'\b(united\s+states|usa|u\.s\.a\.|u\.s\.)\b', "United States"),
            (r'\b(us)\b', "United States"),
            (r'\b(united\s+kingdom|uk|u\.k\.|england|london|great\s+britain)\b', "United Kingdom"),
            (r'\b(australia|sydney|melbourne|brisbane)\b', "Australia"),
            (r'\b(canada|toronto|vancouver)\b', "Canada"),
            (r'\b(germany|berlin)\b', "Germany"),
            (r'\b(europe)\b', "Europe"),
            (r'\b(new\s+zealand|auckland)\b', "New Zealand"),
            (r'\b(worldwide|remote\s+worldwide|global)\b', "Remote Worldwide"),
        ]
        for pat, loc_name in loc_patterns:
            if re.search(pat, text_lower):
                if loc_name not in locations:
                    locations.append(loc_name)

        if not locations:
            locations = ["United States", "United Kingdom", "Europe", "Australia", "New Zealand", "Remote Worldwide"]

        # Work Modes
        remote_mode = "any"
        if "only remote" in text_lower or "remote only" in text_lower:
            remote_mode = "remote"
        elif "hybrid" in text_lower:
            remote_mode = "hybrid_or_remote"

        relocation_allowed = True if ("relocation" in text_lower or "onsite" in text_lower or "relocate" in text_lower) else False
        sponsorship = "possible_or_required" if ("sponsorship" in text_lower or "visa" in text_lower) else "flexible"

        # Match Threshold
        min_match = 70
        match_search = re.search(r'(?:match|threshold)\s*(?:at|of|to)?\s*(\d{2})%?', text_lower)
        if match_search:
            min_match = max(70, int(match_search.group(1)))

        return {
            "target_job_titles": target_jobs,
            "target_locations": locations,
            "remote_mode": remote_mode,
            "relocation_allowed": relocation_allowed,
            "sponsorship_strategy": sponsorship,
            "minimum_match_score": min_match,
            "easy_apply_priority": True,
            "daily_target": 50,
            "daily_ceiling": 60,
            "interview_first": True,
        }

    @classmethod
    def _parse_new_campaign(cls, text: str, workspace: Optional[Workspace] = None) -> Dict[str, Any]:
        """Builds a new campaign draft structure from natural language."""
        icp = cls._parse_prospect_icp(text)
        name = "New Audience Campaign"
        if "mining" in text.lower():
            name = "GeoSentry Mining Exploration"
        elif "ai" in text.lower():
            name = "AI Automation & Backend Leadership"
        elif "freelance" in text.lower():
            name = "Freelance Software Clients"

        return {
            "name": name,
            "icp": icp,
            "is_new_campaign": True,
            "workspace_slug": workspace.slug if workspace else "default",
        }

    @classmethod
    def generate_preview_text(cls, data: Dict[str, Any], intent_type: str, campaign: Optional[Campaign] = None) -> str:
        """Generates the structured interpretation preview shown to the operator."""
        if intent_type == "job_target":
            titles_str = "\n".join(f"  • {t}" for t in data.get("target_job_titles", []))
            locs_str = ", ".join(data.get("target_locations", []))
            return (
                "🎯 **JOB SEARCH TARGETING PREVIEW**\n\n"
                f"**Target Job Families:**\n{titles_str}\n\n"
                f"**Easy Apply:** Strongly Preferred (Prioritized)\n"
                f"**Locations:** {locs_str}\n"
                f"**Remote Mode:** {data.get('remote_mode', 'Any').title()}\n"
                f"**Relocation Allowed:** {'Yes' if data.get('relocation_allowed') else 'No'}\n"
                f"**Sponsorship:** {data.get('sponsorship_strategy', 'Flexible').title()}\n"
                f"**Minimum Match Score:** {data.get('minimum_match_score', 70)}% (Mandatory Hard Floor)\n"
                f"**Daily Successful Target:** {data.get('daily_target', 50)} submissions / day\n"
                f"**Daily Provider Ceiling:** {data.get('daily_ceiling', 60)} attempts / day\n"
                f"**Interview-First:** Active\n"
                f"**Truthful Screening:** Required (Unknown screening questions trigger Needs Your Answer)\n\n"
                "👉 *Click **Activate Job Target** to apply these search preferences, or refine with more details.*"
            )

        elif intent_type == "create_campaign":
            icp = data.get("icp", {})
            titles_str = ", ".join(icp.get("target_titles", []))
            ind_str = ", ".join(icp.get("target_industries", []))
            locs_str = ", ".join(icp.get("target_locations", []))
            size_str = f"{icp.get('company_size_min', 10)}–{icp.get('company_size_max', 500)} employees"
            exc_str = ", ".join(icp.get("excluded_titles", []) + icp.get("excluded_industries", [])) or "None specified"
            return (
                f"✨ **NEW CAMPAIGN PROPOSAL: {data.get('name')}**\n\n"
                f"**Target Titles:** {titles_str}\n"
                f"**Industries:** {ind_str}\n"
                f"**Locations:** {locs_str}\n"
                f"**Company Size:** {size_str}\n"
                f"**Exclusions:** {exc_str}\n"
                f"**Score Floor:** >={icp.get('prospect_score_threshold', 75)} fit score\n\n"
                "👉 *Click **Activate Campaign** to create and enable this campaign.*"
            )

        else:
            camp_name = campaign.name if campaign else "Target Audience"
            titles_str = ", ".join(data.get("target_titles", []))
            ind_str = ", ".join(data.get("target_industries", []))
            locs_str = ", ".join(data.get("target_locations", []))
            size_str = f"{data.get('company_size_min', 10)}–{data.get('company_size_max', 500)} employees"
            exc_str = ", ".join(data.get("excluded_titles", []) + data.get("excluded_industries", [])) or "None"
            threshold = data.get("prospect_score_threshold", 75)
            tier_str = "STRICT (>=85)" if threshold >= 85 else ("BROAD (>=65)" if threshold <= 65 else "NORMAL (>=75)")
            mode = data.get("change_mode", "REPLACE_EXPLICIT")

            mode_str = f"**Mode:** {mode}\n" if mode != "REPLACE_EXPLICIT" else ""

            return (
                f"🎯 **TARGET AUDIENCE PREVIEW ({camp_name})**\n\n"
                f"{mode_str}"
                f"**Titles:** {titles_str}\n"
                f"**Industries:** {ind_str}\n"
                f"**Locations:** {locs_str}\n"
                f"**Company Size:** {size_str}\n"
                f"**Exclusions:** {exc_str}\n"
                f"**Score Threshold:** {tier_str}\n"
                f"**Campaign Goal:** {data.get('campaign_purpose', 'freelance_clients').replace('_', ' ').title()}\n\n"
                "👉 *Click **Activate Targeting** to update this campaign, or say 'Make it broader', 'Only CTOs', etc.*"
            )

    @classmethod
    def score_prospect_against_icp(
        cls,
        profile: Dict[str, Any],
        icp_config: Dict[str, Any],
    ) -> Tuple[int, bool, Dict[str, int], List[str]]:
        """
        Explainable 0-100 Prospect Scoring with Hard Gate Prioritization.
        
        Breakdown:
        - Title Fit: 0-30
        - Seniority Fit: 0-15
        - Industry Fit: 0-15
        - Company Fit (Size/Keywords): 0-15
        - Location Fit: 0-10
        - Context / Keywords Fit: 0-10
        - Campaign Relevance: 0-5
        Total: 0-100
        
        Hard Exclusions (Excluded Title, Excluded Location, Excluded Industry, Excluded Company, Suppression):
        Instantly set Score = 0 and is_qualified = False.
        """
        title = (profile.get("title") or profile.get("current_title") or profile.get("headline") or "").strip().lower()
        company = (profile.get("company") or "").strip().lower()
        industry = (profile.get("industry") or "").strip().lower()
        location = (profile.get("location") or profile.get("country_code") or "").strip().lower()
        summary = (profile.get("summary") or "").strip().lower()

        reasons = []
        breakdown = {
            "title": 0,
            "seniority": 0,
            "industry": 0,
            "company_fit": 0,
            "location": 0,
            "keywords": 0,
            "campaign_fit": 0,
        }

        # ── HARD GATES (Point of Failure) ──
        # 1. Excluded Titles Hard Gate
        excluded_titles = [t.strip().lower() for t in icp_config.get("excluded_titles", []) if t.strip()]
        for exc_t in excluded_titles:
            if re.search(r'\b' + re.escape(exc_t) + r'\b', title):
                reasons.append(f"HARD EXCLUSION: Title '{title}' matches excluded title '{exc_t}'")
                return 0, False, breakdown, reasons

        # 2. Excluded Industries Hard Gate
        excluded_industries = [i.strip().lower() for i in icp_config.get("excluded_industries", []) if i.strip()]
        for exc_i in excluded_industries:
            if exc_i in industry or exc_i in company:
                reasons.append(f"HARD EXCLUSION: Industry/Company '{industry or company}' matches excluded industry '{exc_i}'")
                return 0, False, breakdown, reasons

        # 3. Excluded Locations Hard Gate
        excluded_locations = [l.strip().lower() for l in icp_config.get("excluded_locations", []) if l.strip()]
        for exc_l in excluded_locations:
            if exc_l in location:
                reasons.append(f"HARD EXCLUSION: Location '{location}' matches excluded location '{exc_l}'")
                return 0, False, breakdown, reasons

        # ── SOFT SCORING (Additive Fit) ──
        # 1. Title Fit (0-30)
        target_titles = [t.strip().lower() for t in icp_config.get("target_titles", []) if t.strip()]
        if any(re.search(r'\b' + re.escape(t) + r'\b', title) for t in target_titles):
            breakdown["title"] = 30
            reasons.append(f"Title Fit: Exact target title matched (+30)")
        elif any(t in title for t in target_titles):
            breakdown["title"] = 20
            reasons.append(f"Title Fit: Partial target title matched (+20)")
        elif title:
            breakdown["title"] = 5
            reasons.append(f"Title Fit: Unlisted title (+5)")

        # 2. Seniority Fit (0-15)
        if any(w in title for w in ["founder", "co-founder", "owner"]):
            breakdown["seniority"] = 15
            reasons.append("Seniority: Executive/Founder tier (+15)")
        elif any(w in title for w in ["ceo", "cto", "cfo", "coo", "chief"]):
            breakdown["seniority"] = 15
            reasons.append("Seniority: C-Level executive tier (+15)")
        elif any(w in title for w in ["vp", "vice president", "head", "director"]):
            breakdown["seniority"] = 12
            reasons.append("Seniority: Director/VP/Head tier (+12)")
        elif any(w in title for w in ["manager", "lead", "principal"]):
            breakdown["seniority"] = 10
            reasons.append("Seniority: Management/Lead tier (+10)")
        else:
            breakdown["seniority"] = 5

        # 3. Industry Fit (0-15)
        target_industries = [i.strip().lower() for i in icp_config.get("target_industries", []) if i.strip()]
        combined_ind = f"{industry} {company}".lower()
        if any(i in combined_ind for i in target_industries):
            breakdown["industry"] = 15
            reasons.append(f"Industry: Target industry matched (+15)")
        elif industry or company:
            breakdown["industry"] = 8
            reasons.append(f"Industry: General tech/industry detected (+8)")

        # 4. Company Fit / Size (0-15)
        emp_count = profile.get("company_size_count")
        min_sz = icp_config.get("company_size_min", 10)
        max_sz = icp_config.get("company_size_max", 500)
        if emp_count is not None:
            if min_sz <= emp_count <= max_sz:
                breakdown["company_fit"] = 15
                reasons.append(f"Company Fit: Size ({emp_count}) within ideal range [{min_sz}-{max_sz}] (+15)")
            else:
                breakdown["company_fit"] = 5
                reasons.append(f"Company Fit: Size ({emp_count}) outside ideal range (+5)")
        else:
            breakdown["company_fit"] = 12
            reasons.append("Company Fit: Standard startup/SME company profile (+12)")

        # 5. Location Fit (0-10)
        target_locations = [l.strip().lower() for l in icp_config.get("target_locations", []) if l.strip()]
        if any(l in location for l in target_locations):
            breakdown["location"] = 10
            reasons.append(f"Location Fit: Target region '{location}' matched (+10)")
        elif location:
            breakdown["location"] = 4
            reasons.append(f"Location Fit: Non-excluded location '{location}' (+4)")

        # 6. Keywords / Tech Context (0-10)
        combined_text = f"{title} {summary} {company}".lower()
        if any(k in combined_text for k in ["ai", "python", "backend", "saas", "automation", "mining", "software"]):
            breakdown["keywords"] = 10
            reasons.append("Keywords: High-intent domain keywords detected (+10)")
        else:
            breakdown["keywords"] = 4

        # 7. Campaign Relevance (0-5)
        purpose = icp_config.get("campaign_purpose", "freelance_clients")
        if purpose == "geosentry_mining" and ("mining" in combined_text or "exploration" in combined_text):
            breakdown["campaign_fit"] = 5
            reasons.append("Campaign Fit: High-affinity GeoSentry prospect (+5)")
        elif purpose == "freelance_clients" and any(k in combined_text for k in ["founder", "cto", "software", "product"]):
            breakdown["campaign_fit"] = 5
            reasons.append("Campaign Fit: Software client decision maker (+5)")
        else:
            breakdown["campaign_fit"] = 3

        total_score = sum(breakdown.values())
        threshold = icp_config.get("prospect_score_threshold", 75)
        is_qualified = total_score >= threshold

        return total_score, is_qualified, breakdown, reasons

    @classmethod
    @transaction.atomic
    def activate_targeting_config(
        cls,
        workspace: Workspace,
        campaign: Optional[Campaign],
        config_data: Dict[str, Any],
        operator_prompt: str = "",
        config_type: str = "prospect_icp",
    ) -> TargetingConfigVersion:
        """
        Applies validated targeting to Campaign / Workspace and saves a version record.
        """
        # Determine next version number
        latest_ver = TargetingConfigVersion.objects.filter(
            workspace=workspace,
            campaign=campaign,
            config_type=config_type,
        ).order_by("-version_number").first()
        next_ver_num = (latest_ver.version_number + 1) if latest_ver else 1

        # Mark older versions inactive
        TargetingConfigVersion.objects.filter(
            workspace=workspace,
            campaign=campaign,
            config_type=config_type,
        ).update(is_active=False)

        # Apply to persistent database models
        if config_type == "job_target":
            auto_settings = WorkspaceAutomationSettings.get_for_workspace(workspace)
            auto_settings.daily_job_application_target = config_data.get("daily_target", 50)
            auto_settings.easy_apply_priority = config_data.get("easy_apply_priority", True)
            auto_settings.save(update_fields=["daily_job_application_target", "easy_apply_priority", "updated_at"])

            if campaign:
                from openoutreach.career.models import JobSearchPreference
                pref = getattr(campaign, "job_search_preference", None)
                if pref:
                    pref.job_titles = config_data.get("target_job_titles", [])
                    pref.locations = config_data.get("target_locations", [])
                    pref.match_threshold = config_data.get("minimum_match_score", 70)
                    pref.easy_apply_only = config_data.get("easy_apply_priority", True)
                    pref.save()

        elif config_type == "prospect_icp" and campaign:
            campaign.target_job_titles = ", ".join(config_data.get("target_titles", []))
            campaign.target_industries = ", ".join(config_data.get("target_industries", []))
            campaign.target_locations = ", ".join(config_data.get("target_locations", []))
            campaign.target_locations_excluded = ", ".join(config_data.get("excluded_locations", []))
            min_s = config_data.get("company_size_min", 10)
            max_s = config_data.get("company_size_max", 500)
            campaign.target_company_size = f"{min_s}-{max_s}"
            campaign.save()

        # Create Version Snapshot
        version_obj = TargetingConfigVersion.objects.create(
            workspace=workspace,
            campaign=campaign,
            version_number=next_ver_num,
            config_type=config_type,
            config_data=config_data,
            operator_prompt=operator_prompt,
            ai_summary=config_data.get("preview_text", ""),
            is_active=True,
        )
        logger.info("Activated TargetingConfigVersion v%d for %s", next_ver_num, campaign or workspace)
        return version_obj

    @classmethod
    @transaction.atomic
    def rollback_targeting_config(
        cls,
        workspace: Workspace,
        campaign: Optional[Campaign] = None,
        config_type: str = "prospect_icp",
    ) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
        """
        Rolls back the targeting configuration to the immediately preceding version.
        """
        versions = list(TargetingConfigVersion.objects.filter(
            workspace=workspace,
            campaign=campaign,
            config_type=config_type,
        ).order_by("-version_number"))

        if len(versions) < 2:
            return False, "No previous targeting version exists to roll back to.", None

        current_ver = versions[0]
        prev_ver = versions[1]

        # Apply previous version data
        if config_type == "job_target":
            auto_settings = WorkspaceAutomationSettings.get_for_workspace(workspace)
            auto_settings.daily_job_application_target = prev_ver.config_data.get("daily_target", 50)
            auto_settings.easy_apply_priority = prev_ver.config_data.get("easy_apply_priority", True)
            auto_settings.save(update_fields=["daily_job_application_target", "easy_apply_priority", "updated_at"])

            if campaign:
                pref = getattr(campaign, "job_search_preference", None)
                if pref:
                    pref.job_titles = prev_ver.config_data.get("target_job_titles", [])
                    pref.locations = prev_ver.config_data.get("target_locations", [])
                    pref.match_threshold = prev_ver.config_data.get("minimum_match_score", 70)
                    pref.save()

        elif config_type == "prospect_icp" and campaign:
            campaign.target_job_titles = ", ".join(prev_ver.config_data.get("target_titles", []))
            campaign.target_industries = ", ".join(prev_ver.config_data.get("target_industries", []))
            campaign.target_locations = ", ".join(prev_ver.config_data.get("target_locations", []))
            campaign.target_locations_excluded = ", ".join(prev_ver.config_data.get("excluded_locations", []))
            campaign.target_company_size = f"{prev_ver.config_data.get('company_size_min', 10)}-{prev_ver.config_data.get('company_size_max', 500)}"
            campaign.save()

        # Update version active states
        current_ver.is_active = False
        current_ver.save(update_fields=["is_active"])
        prev_ver.is_active = True
        prev_ver.save(update_fields=["is_active"])

        return True, f"Successfully rolled back targeting to Version {prev_ver.version_number}.", prev_ver.config_data

    @classmethod
    def preview_prospect_matches(
        cls,
        workspace: Workspace,
        campaign: Optional[Campaign],
        icp_config: Dict[str, Any],
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """
        Returns a sample of ranked prospect matches against the ICP without external writes.
        """
        from openoutreach.crm.models import Lead
        leads = Lead.objects.filter(workspace=workspace)[:50]
        results = []

        for lead in leads:
            p_dict = {
                "title": lead.headline or lead.job_title or "Founder",
                "company": lead.company_name or "Startup",
                "industry": lead.industry or "Software",
                "location": lead.location or "United States",
                "summary": lead.summary or "",
                "company_size_count": 50,
            }
            score, is_qual, breakdown, reasons = cls.score_prospect_against_icp(p_dict, icp_config)
            results.append({
                "lead_id": lead.id,
                "name": lead.full_name or f"{lead.first_name} {lead.last_name}".strip() or "Prospect",
                "title": p_dict["title"],
                "company": p_dict["company"],
                "location": p_dict["location"],
                "score": score,
                "is_qualified": is_qual,
                "reasons": reasons,
                "breakdown": breakdown,
            })

        results.sort(key=lambda x: x["score"], reverse=True)
        return results[:limit]

    @classmethod
    def preview_job_matches(
        cls,
        workspace: Workspace,
        job_target_config: Dict[str, Any],
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """
        Returns a sample of matching jobs against the Job Target without external writes.
        """
        from openoutreach.career.models import Job
        jobs = Job.objects.filter(is_synthetic=False).order_by("-discovered_at")[:50]
        results = []

        target_titles = [t.lower() for t in job_target_config.get("target_job_titles", [])]
        min_score = job_target_config.get("minimum_match_score", 70)

        for j in jobs:
            title_lower = j.title.lower()
            fit = 85 if any(t in title_lower for t in target_titles) else 72
            results.append({
                "job_id": j.id,
                "title": j.title,
                "company": j.company,
                "location": j.location,
                "easy_apply": j.easy_apply,
                "match_score": fit,
                "meets_threshold": fit >= min_score,
            })

        results.sort(key=lambda x: x["match_score"], reverse=True)
        return results[:limit]

    @classmethod
    def _handle_job_status_query(cls, workspace: Optional[Workspace]) -> Dict[str, Any]:
        """Returns canonical live active job targeting state without proposing configuration changes."""
        if not workspace:
            return {"intent_type": "status_query", "reply": "Please select a workspace to view job targeting status."}

        from openoutreach.career.models import CandidateProfile, CandidateApplicationProfile, JobApplication
        from openoutreach.core.models import WorkspaceAutomationSettings
        from openoutreach.core.services.quota_service import (
            get_daily_successful_easy_apply_count,
            get_daily_job_provider_attempt_count,
        )
        from django.utils import timezone

        auto_settings = WorkspaceAutomationSettings.get_for_workspace(workspace)
        cand = (
            CandidateProfile.objects.filter(user__workspace_memberships__workspace=workspace).first()
            or CandidateProfile.objects.filter(user__campaigns__workspace=workspace).first()
            or (CandidateProfile.objects.filter(user=workspace.owner).first() if hasattr(workspace, "owner") and workspace.owner else None)
        )
        app_prof = (
            CandidateApplicationProfile.objects.filter(candidate_profile=cand).first()
            if cand
            else None
        )

        # Canonical Relocation & Sponsorship Truth
        if app_prof:
            reloc_allowed = bool(app_prof.willing_to_relocate or app_prof.relocation_willingness == "yes")
            reloc_str = "Yes (where relocation allowed)" if reloc_allowed else "No"
            if app_prof.application_strategy == "INTERVIEW_FIRST_SPONSORSHIP":
                sponsorship_str = "Interview-First (Sponsorship flexible)"
            elif app_prof.visa_sponsorship_now == "no":
                sponsorship_str = "No sponsorship needed"
            else:
                sponsorship_str = "Sponsorship required"
            target_jobs = app_prof.target_job_titles if app_prof.target_job_titles else [
                "Python Backend Developer", "AI Engineer", "Backend Software Engineer", "AI Automation Engineer", "Full Stack Software Engineer"
            ]
            target_locs = app_prof.target_locations if app_prof.target_locations else [
                "United States", "United Kingdom", "Europe", "Australia", "New Zealand", "Remote Worldwide"
            ]
        else:
            reloc_str = "Yes (where relocation allowed)"
            sponsorship_str = "Interview-First (Sponsorship flexible)"
            target_jobs = ["Python Backend Developer", "AI Engineer", "Backend Software Engineer", "AI Automation Engineer", "Full Stack Software Engineer"]
            target_locs = ["United States", "United Kingdom", "Europe", "Australia", "New Zealand", "Remote Worldwide"]

        successful_easy_apply = get_daily_successful_easy_apply_count(workspace=workspace)
        provider_attempts = get_daily_job_provider_attempt_count(workspace=workspace)
        successful_target = getattr(auto_settings, "daily_job_application_target", 50) or 50
        provider_ceiling = getattr(auto_settings, "max_daily_job_applications", 60) or 60

        today_start = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
        app_qs = JobApplication.objects.filter(
            candidate_profile__user__campaigns__workspace=workspace,
            created_at__gte=today_start
        ).distinct()

        confirmed = app_qs.filter(status="externally_confirmed").count()
        unverified = app_qs.filter(status="submission_unverified").count()
        uncertain = app_qs.filter(status="submission_uncertain").count()
        needs_answer = app_qs.filter(status="needs_your_answer").count()
        blocked = app_qs.filter(status="blocked_by_gate").count()

        job_titles_str = "\n".join(f"  • {t}" for t in target_jobs)
        locs_str = ", ".join(target_locs)

        reply = (
            "🎯 **ACTIVE JOB TARGET STATUS**\n\n"
            f"**Target Job Families:**\n{job_titles_str}\n\n"
            f"**Easy Apply:** Strongly Preferred (Prioritized)\n"
            f"**Locations:** {locs_str}\n"
            f"**Minimum Match Score:** 70% (Mandatory Hard Floor)\n"
            f"**Interview-First:** Active\n"
            f"**Current Relocation Policy:** {reloc_str}\n"
            f"**Current Sponsorship Policy:** {sponsorship_str}\n\n"
            f"**Today's Application Progress:**\n"
            f"• **Successful Submissions:** {successful_easy_apply} / {successful_target} target\n"
            f"• **Provider Attempts:** {provider_attempts} / {provider_ceiling} safety ceiling\n"
            f"• **Externally Confirmed:** {confirmed}\n"
            f"• **Submission Unverified:** {unverified}\n"
            f"• **Submission Uncertain:** {uncertain}\n"
            f"• **Needs Your Answer:** {needs_answer}\n"
            f"• **Blocked Before Provider:** {blocked}\n\n"
            f"*Autonomous discovery continues searching continuously until {successful_target} successful Easy Apply applications are achieved or inventory is safely exhausted.*"
        )
        return {
            "intent_type": "status_query",
            "reply": reply,
        }

    @classmethod
    def _handle_active_targeting_query(cls, workspace: Optional[Workspace], campaign: Optional[Campaign]) -> Dict[str, Any]:
        """Returns current active targeting configuration."""
        if not workspace:
            return {"intent_type": "status_query", "reply": "Please select a workspace to view active targeting."}
        auto_settings = WorkspaceAutomationSettings.get_for_workspace(workspace)
        active_camp = campaign or Campaign.objects.filter(workspace=workspace, is_active=True).first() or Campaign.objects.filter(workspace=workspace).first()

        camp_info = f"**Active Campaign:** {active_camp.name if active_camp else 'None'}\n"
        if active_camp:
            camp_info += (
                f"- Titles: {active_camp.target_job_titles or 'Default (Founder, CTO)'}\n"
                f"- Locations: {active_camp.target_locations or 'US, UK, AU, CA'}\n"
                f"- Excluded Locations: {active_camp.target_locations_excluded or 'None'}\n"
                f"- Industries: {active_camp.target_industries or 'B2B SaaS, AI'}\n"
                f"- Company Size: {active_camp.target_company_size or '10-500'}\n"
            )

        job_info = (
            f"\n**Job Search Settings:**\n"
            f"- Daily Target: {auto_settings.daily_job_application_target} / day\n"
            f"- Daily Ceiling: {auto_settings.max_daily_job_applications} / day\n"
            f"- Easy Apply Priority: {'ON' if auto_settings.easy_apply_priority else 'OFF'}\n"
            f"- Minimum Match Floor: 70%\n"
        )
        return {
            "intent_type": "status_query",
            "reply": f"📊 **CURRENT HUNTFLOW TARGETING CONFIGURATION**\n\n{camp_info}{job_info}",
        }

    @classmethod
    def _handle_counter_query(cls, workspace: Optional[Workspace], channel: str) -> Dict[str, Any]:
        """Returns live counter progress from quota_service."""
        if not workspace:
            return {"intent_type": "status_query", "reply": "Please select a workspace to view counters."}
        from openoutreach.core.services.quota_service import get_workspace_quota_summary
        auto_settings = WorkspaceAutomationSettings.get_for_workspace(workspace)
        summary = get_workspace_quota_summary(workspace, auto_settings)

        if channel == "connections":
            c = summary["connections"]
            return {
                "intent_type": "status_query",
                "reply": f"📊 **LINKEDIN CONNECTIONS TODAY:** {c['consumed']} / {c['ceiling']} (Remaining: {c['remaining']})",
            }
        elif channel == "jobs":
            j = summary["jobs"]
            target = getattr(auto_settings, "daily_job_application_target", 50)
            return {
                "intent_type": "status_query",
                "reply": f"🎯 **JOB APPLICATIONS TODAY:** {j['successful_easy_apply']} / {target} target ({j['provider_attempts']} / {j['provider_ceiling']} safety ceiling)",
            }
        return {"intent_type": "status_query", "reply": "Counter requested."}

    @classmethod
    def _handle_job_shortfall_query(cls, workspace: Optional[Workspace]) -> Dict[str, Any]:
        """Explains why daily job application target has not yet been reached."""
        if not workspace:
            return {"intent_type": "status_query", "reply": "Please select a workspace."}
        from openoutreach.career.models import JobApplication
        from openoutreach.core.services.quota_service import (
            get_daily_successful_easy_apply_count,
            get_daily_job_provider_attempt_count,
        )
        from django.utils import timezone
        today_start = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)

        needs_answer = JobApplication.objects.filter(candidate_profile__user__campaigns__workspace=workspace, status="needs_your_answer", created_at__gte=today_start).distinct().count()
        uncertain = JobApplication.objects.filter(candidate_profile__user__campaigns__workspace=workspace, status="submission_uncertain", submission_date__gte=today_start).distinct().count()
        successful = get_daily_successful_easy_apply_count(workspace=workspace)
        attempts = get_daily_job_provider_attempt_count(workspace=workspace)

        reasons = []
        if needs_answer > 0:
            reasons.append(f"{needs_answer} applications require operator answers for custom screening questions.")
        if uncertain > 0:
            reasons.append(f"{uncertain} applications had uncertain provider responses.")
        if successful < 50:
            reasons.append("Autonomous discovery is actively cycling through search queries across configured job families.")

        reasons_text = "\n- ".join(reasons) if reasons else "Autonomous discovery is running continuously throughout the day."
        return {
            "intent_type": "status_query",
            "reply": f"ℹ️ **50-JOB PROGRESS DIAGNOSTIC**\n\nSuccessful Easy Apply: {successful} / 50 target\nProvider Attempts: {attempts} / 60 ceiling\n\n**Current State:**\n- {reasons_text}\n\n*HuntFlow never fabricates applications and will continue searching until 50 successful Easy Apply applications are reached or inventory is exhausted.*",
        }

    @classmethod
    def _handle_rollback_intent(cls, workspace: Optional[Workspace], campaign: Optional[Campaign]) -> Dict[str, Any]:
        """Handles operator rollback command."""
        if not workspace:
            return {"reply": "Please select a workspace to perform rollback."}
        success, msg, _ = cls.rollback_targeting_config(workspace, campaign)
        return {
            "intent_type": "rollback_command",
            "reply": f"⏪ {msg}",
        }

    @classmethod
    def _handle_preview_intent(cls, workspace: Optional[Workspace], campaign: Optional[Campaign], preview_type: str) -> Dict[str, Any]:
        """Handles operator preview matches request."""
        if not workspace:
            return {"reply": "Please select a workspace to preview matches."}
        if preview_type == "prospects":
            icp = {
                "target_titles": [t.strip() for t in (campaign.target_job_titles or "Founder, CTO").split(",") if t.strip()] if campaign else ["Founder", "CTO"],
                "target_industries": [i.strip() for i in (campaign.target_industries or "B2B SaaS, AI").split(",") if i.strip()] if campaign else ["B2B SaaS", "AI"],
                "target_locations": [l.strip() for l in (campaign.target_locations or "United States, United Kingdom").split(",") if l.strip()] if campaign else ["United States"],
                "prospect_score_threshold": 75,
            }
            sample = cls.preview_prospect_matches(workspace, campaign, icp, limit=5)
            if not sample:
                return {"intent_type": "preview", "reply": "No prospects currently stored in CRM to preview. Autonomous discovery will ingest and score fresh prospects."}
            lines = [f"• **{p['name']}** ({p['title']} @ {p['company']}, {p['location']}) — Score: **{p['score']}/100** ({'Eligible' if p['is_qualified'] else 'Skipped'})" for p in sample]
            return {
                "intent_type": "preview",
                "reply": "👥 **SAMPLE AUDIENCE MATCH PREVIEW (Read-Only)**\n\n" + "\n".join(lines),
            }
        else:
            job_target = {
                "target_job_titles": ["Python Backend Developer", "AI Engineer", "Backend Software Engineer"],
                "minimum_match_score": 70,
            }
            sample = cls.preview_job_matches(workspace, job_target, limit=5)
            if not sample:
                return {"intent_type": "preview", "reply": "No discovered jobs currently stored in database. Autonomous job discovery will fetch and rank new Easy Apply jobs."}
            lines = [f"• **{j['title']}** @ {j['company']} ({j['location']}) — Match: **{j['match_score']}%** (Easy Apply: {'Yes' if j['easy_apply'] else 'No'})" for j in sample]
            return {
                "intent_type": "preview",
                "reply": "💼 **SAMPLE JOB MATCH PREVIEW (Read-Only)**\n\n" + "\n".join(lines),
            }
