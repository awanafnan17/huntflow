# openoutreach/core/agents/connection_note.py
"""Connection note generator for LinkedIn connection requests.

Generates a personalised note for each connection request using:
1. LLM (if configured and working) — natural, context-aware note
2. Template fallback — placeholder-based note from campaign settings

The note is capped at LinkedIn's 300-character limit and cleaned of
unresolved placeholders, double spaces, and broken punctuation.
"""
from __future__ import annotations

import logging
import re
from typing import Tuple

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# LinkedIn connection note character limit (as of 2026).
MAX_NOTE_LENGTH = 300

DEFAULT_TEMPLATE = (
    "hey {first_name}, saw what you guys are building at {company_or_industry}... "
    "curious what you're focused on rn?"
)

FALLBACK_NO_COMPANY = "hey {first_name}, saw what you're working on... curious what you're focused on rn?"
FALLBACK_NO_NAME = "saw what you guys are building... curious what you're focused on rn?"


def generate_terse_candidate_fallback(profile: dict) -> str:
    """Generate a terse, authentic Candidate-style connection note fallback without LLM.

    Adheres strictly to Candidate voice: casual, direct, short, lowercase-friendly,
    with no corporate buzzwords or resume dumps.
    """
    first_name = (profile.get("first_name") or "").strip()
    company = (profile.get("company") or profile.get("company_name") or "").strip()
    positions = profile.get("positions") or []
    if not company and positions:
        company = (positions[0].get("company_name") or "").strip()

    title = (profile.get("title") or profile.get("job_title") or profile.get("headline") or "").strip()
    if not title and positions:
        title = (positions[0].get("title") or "").strip()

    if first_name and company:
        return f"hey {first_name}, saw what you guys are building at {company}... curious what you're focused on rn?"
    elif first_name and title:
        return f"hey {first_name}, saw your work around {title.lower()}... curious what you're focused on rn?"
    elif first_name:
        return f"hey {first_name}, saw what you're working on... curious what you're focused on rn?"
    elif company:
        return f"saw what you guys are building at {company}... curious what you're focused on rn?"
    else:
        return "saw what you guys are building... curious what you're focused on rn?"


class ConnectionNote(BaseModel):
    """Structured output from the LLM note generator."""
    note: str = Field(description="A short, natural LinkedIn connection note (max 300 characters).")


def generate_connection_note(
    session,
    campaign,
    profile: dict,
) -> Tuple[str, str]:
    """Generate a connection note for the given campaign + profile.

    Returns ``(note_text, source)`` where source is one of:
    ``"llm"``, ``"template"``, or ``"disabled"``.
    """
    if not campaign.connection_note_enabled:
        return ("", "disabled")

    from openoutreach.core.content_style import (
        validate_user_facing_content_style,
        validate_generated_claims,
    )

    # Try LLM first if configured
    try:
        from openoutreach.core.models import SiteConfig
        cfg = SiteConfig.load()
        if cfg.is_llm_configured():
            note = _generate_llm_note(session, campaign, profile)
            if note:
                cleaned_note = _clean_and_cap(note)
                style_res = validate_user_facing_content_style(cleaned_note, "connection_note")
                claims_res = validate_generated_claims(cleaned_note, context=profile)
                if style_res["valid"] and claims_res["valid"]:
                    return (cleaned_note, "llm")
                logger.warning(
                    "LLM note failed validation (style: %s, claims: %s) — falling back to template",
                    style_res.get("errors"), claims_res.get("errors"),
                )
    except Exception:
        logger.exception("LLM note generation failed — falling back to template")

    # Fallback to template
    template_note = _generate_template_note(campaign, profile)
    cleaned_template = _clean_and_cap(template_note)
    style_res = validate_user_facing_content_style(cleaned_template, "connection_note")
    claims_res = validate_generated_claims(cleaned_template, context=profile)
    if style_res["valid"] and claims_res["valid"]:
        return (cleaned_template, "template")

    logger.error("Template connection note also failed validation — blocking note output")
    return ("", "blocked")


def _extract_profile_fields(profile: dict) -> dict:
    """Extract known fields from a profile dict for placeholder resolution."""
    p = profile or {}
    first_name = p.get("first_name", "") or ""
    last_name = p.get("last_name", "") or ""
    full_name = p.get("full_name", "") or f"{first_name} {last_name}".strip()
    headline = p.get("headline", "") or ""
    location = p.get("location_name", "") or ""

    # Company: from the first position
    company = ""
    positions = p.get("positions") or []
    if positions:
        company = positions[0].get("company_name", "") or ""

    # Title: from the first position, fall back to headline
    title = ""
    if positions:
        title = positions[0].get("title", "") or ""
    if not title:
        title = headline

    # Industry
    industry_raw = p.get("industry") or {}
    if isinstance(industry_raw, dict):
        industry = industry_raw.get("name", "") or ""
    else:
        industry = str(industry_raw) if industry_raw else ""

    # Computed placeholder: company_or_industry
    company_or_industry = company or industry or ""

    return {
        "first_name": first_name,
        "full_name": full_name,
        "title": title,
        "company": company,
        "industry": industry,
        "location": location,
        "company_or_industry": company_or_industry,
    }


def _generate_template_note(campaign, profile: dict) -> str:
    """Generate a note from the campaign template with placeholder substitution."""
    fields = _extract_profile_fields(profile)

    # Add campaign_name
    fields["campaign_name"] = campaign.name or ""

    # Pick the right template
    template = campaign.connection_note_template.strip() if campaign.connection_note_template else ""
    if not template:
        # Use built-in defaults with graceful fallback
        if not fields["company_or_industry"]:
            if not fields["first_name"]:
                template = FALLBACK_NO_NAME
            else:
                template = FALLBACK_NO_COMPANY
        elif not fields["first_name"]:
            # Has company_or_industry but no name — strip {first_name} from default
            template = DEFAULT_TEMPLATE
        else:
            template = DEFAULT_TEMPLATE

    # Substitute placeholders
    note = template
    for key, value in fields.items():
        note = note.replace("{" + key + "}", value)

    # Remove any remaining unresolved {placeholder} tokens
    note = re.sub(r"\{[a-z_]+\}", "", note)

    return note


def _generate_llm_note(session, campaign, profile: dict) -> str | None:
    """Generate a note using the configured LLM.

    Returns the note text on success, or None on failure (caller falls back).
    """
    from pydantic_ai import Agent

    from openoutreach.core.agents.prompt import render
    from openoutreach.core.llm import get_llm_model, run_agent_sync

    fields = _extract_profile_fields(profile)

    context = {
        "product_docs": campaign.product_docs or "",
        "campaign_objective": campaign.campaign_objective or "",
        "first_name": fields["first_name"],
        "full_name": fields["full_name"],
        "title": fields["title"],
        "company": fields["company"],
        "industry": fields["industry"],
        "location": fields["location"],
        "extra_instructions": (campaign.connection_note_instructions or "").strip(),
        "max_length": MAX_NOTE_LENGTH,
    }

    system_prompt = render("connection_note.j2", **context)

    from openoutreach.core.content_style import validate_user_facing_content_style
    from pydantic_ai.exceptions import UnexpectedModelBehavior

    # 1. Try structured Pydantic output first
    try:
        agent = Agent(
            get_llm_model(),
            output_type=ConnectionNote,
            model_settings={"temperature": 0.7, "timeout": 30},
        )
        for attempt in range(3):
            result = run_agent_sync(agent.run(system_prompt))
            if result and result.output and result.output.note:
                note_candidate = result.output.note
                res = validate_user_facing_content_style(note_candidate, "connection_note")
                if res["valid"]:
                    return note_candidate
                logger.warning(f"Connection note style validation failed on attempt {attempt+1}: {res['errors']}")
    except Exception as exc:
        logger.warning(f"Structured ConnectionNote output failed ({exc}) — attempting string output mode")

    # 2. String output mode fallback (for local LLMs like Ollama)
    try:
        str_agent = Agent(
            get_llm_model(),
            system_prompt=system_prompt,
            model_settings={"temperature": 0.7, "timeout": 30},
        )
        for attempt in range(2):
            result = run_agent_sync(str_agent.run("Generate the LinkedIn connection note."))
            if result and result.output:
                note_candidate = str(result.output).strip()
                cleaned = _clean_and_cap(note_candidate)
                res = validate_user_facing_content_style(cleaned, "connection_note")
                if res["valid"]:
                    return cleaned
                logger.warning(
                    "String mode connection note style validation failed on attempt %d: %s",
                    attempt + 1, res["errors"],
                )
    except Exception as exc:
        logger.warning("String mode fallback generation failed: %s", exc)

    return None


def _clean_and_cap(note: str) -> str:
    """Clean up a note and enforce the LinkedIn character limit."""
    # Strip bracketed placeholders if left by LLM
    note = re.sub(r"\[.*?\]", "", note)
    # Replace em dashes and en dashes with comma or hyphen
    note = note.replace("—", ", ").replace("–", " - ")
    # Strip leading/trailing surrounding quotes
    note = note.strip("'\"`")
    # Collapse whitespace
    note = re.sub(r"[ \t]+", " ", note).strip()
    # Fix broken punctuation (e.g. ", ," or ". .")
    note = re.sub(r"[,\s]+,", ",", note)
    # Collapse 4+ consecutive dots to an ellipsis "..."
    note = re.sub(r"\.{4,}", "...", note)
    # Collapse double dots ".." to single "." if not part of an ellipsis
    note = re.sub(r"(?<!\.)\.\.(?!\.)", ".", note)
    # Remove leading/trailing commas that result from empty placeholders
    note = re.sub(r"^[,\s]+", "", note)
    note = re.sub(r"\s+([,])", r"\1", note)
    # Cap length — truncate at last word boundary within limit
    if len(note) > MAX_NOTE_LENGTH:
        note = note[:MAX_NOTE_LENGTH].rsplit(" ", 1)[0].rstrip(" ,.")
    return note.strip()
