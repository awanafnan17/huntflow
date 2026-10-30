# openoutreach/linkedin/pipeline/search_keywords.py
"""LLM-based generation of LinkedIn People search keywords."""
from __future__ import annotations

import logging

import jinja2
from pydantic import BaseModel, Field

from openoutreach.core.conf import PROMPTS_DIR

logger = logging.getLogger(__name__)


class SearchKeywords(BaseModel):
    """Structured LLM output for search keyword generation."""
    keywords: list[str] = Field(description="List of LinkedIn People search queries")


def generate_search_keywords(
    product_docs: str = "",
    campaign_objective: str = "",
    n_keywords: int = 10,
    exclude_keywords: list[str] | None = None,
    campaign: any = None,
    target_job_titles: str = "",
    target_seniority: str = "",
    target_industries: str = "",
    target_locations: str = "",
    target_company_size: str = "",
    target_keywords: str = "",
) -> list[str]:
    """Call LLM to generate LinkedIn search keywords from campaign context & structured ICP fields.

    Returns a list of search query strings.
    """
    from pydantic_ai import Agent

    from openoutreach.core.llm import get_llm_model, run_agent_sync

    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(PROMPTS_DIR)))
    template = env.get_template("search_keywords.j2")

    if campaign:
        product_docs = product_docs or campaign.product_docs
        campaign_objective = campaign_objective or campaign.campaign_objective
        target_job_titles = target_job_titles or campaign.target_job_titles
        target_seniority = target_seniority or campaign.target_seniority
        target_industries = target_industries or campaign.target_industries
        target_locations = target_locations or campaign.target_locations
        target_company_size = target_company_size or campaign.target_company_size
        target_keywords = target_keywords or campaign.target_keywords

    prompt = template.render(
        product_docs=product_docs,
        campaign_objective=campaign_objective,
        target_job_titles=target_job_titles,
        target_seniority=target_seniority,
        target_industries=target_industries,
        target_locations=target_locations,
        target_company_size=target_company_size,
        target_keywords=target_keywords,
        n_keywords=n_keywords,
        exclude_keywords=exclude_keywords or [],
    )

    keywords = []
    try:
        agent = Agent(
            get_llm_model(),
            output_type=SearchKeywords,
            model_settings={"temperature": 0.7},
        )
        result = run_agent_sync(agent.run(prompt)).output
        keywords = result.keywords
    except Exception as exc:
        logger.warning("Structured Pydantic-AI search keyword generation failed (%s), falling back to raw text parsing.", exc)
        try:
            str_agent = Agent(
                get_llm_model(),
                output_type=str,
                model_settings={"temperature": 0.7},
            )
            raw_text = run_agent_sync(str_agent.run(prompt + "\nFormat as a simple bulleted list.")).output
            for line in raw_text.splitlines():
                cleaned = line.strip().lstrip("-*1234567890.").strip()
                if cleaned and len(cleaned) > 2:
                    keywords.append(cleaned)
        except Exception:
            keywords = []

    # ── Role Title Intent Verification & Enforcement ──
    titles_list = [t.strip().lower() for t in target_job_titles.split(",") if t.strip()]
    if not titles_list:
        titles_list = ["founder", "ceo", "cto", "head of product"]

    valid_role_keywords = []
    for kw in keywords:
        kw_lower = kw.lower()
        if any(t in kw_lower for t in titles_list) or any(r in kw_lower for r in ["founder", "ceo", "cto", "head", "vp", "director", "manager"]):
            valid_role_keywords.append(kw)
        else:
            # Prepend primary target role if missing from query
            primary_role = titles_list[0].title()
            valid_role_keywords.append(f"{primary_role} {kw}")

    if not valid_role_keywords:
        loc = target_locations.split(",")[0].strip() if target_locations else "UK"
        ind = target_industries.split(",")[0].strip() if target_industries else "Software"
        valid_role_keywords = [f"{t.strip().title()} {ind} {loc}".strip() for t in titles_list]

    keywords = valid_role_keywords[:n_keywords]

    logger.info("Generated %d search keywords via LLM", len(keywords))
    return keywords
