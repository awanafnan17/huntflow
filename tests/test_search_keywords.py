# tests/test_search_keywords.py
"""Unit tests for LinkedIn search keyword generation quality and role-title intent."""
import pytest
from unittest.mock import patch, MagicMock

from openoutreach.linkedin.pipeline.search_keywords import generate_search_keywords, SearchKeywords


@pytest.mark.django_db
def test_generate_search_keywords_role_intent_enforcement():
    """Verify generated keywords are sanitized to contain target role title intent."""
    from openoutreach.core.models import SiteConfig
    cfg = SiteConfig.load()
    cfg.ai_model = "ollama:llama3.2"
    cfg.save()

    raw_llm_keywords = SearchKeywords(keywords=[
        "3D Website Europe",
        "Website Redesign",
        "CTO Software Germany",
        "Full Stack Development",
    ])
    
    mock_result = MagicMock()
    mock_result.output = raw_llm_keywords

    mock_agent = MagicMock()
    mock_agent.run.return_value = None

    with patch("pydantic_ai.Agent", return_value=mock_agent), \
         patch("openoutreach.core.llm.run_agent_sync", return_value=mock_result):
        kws = generate_search_keywords(
            target_job_titles="Founder, CEO, CTO, Head of Product",
            target_industries="Software, Design, Web Development",
            target_locations="United Kingdom, Germany, Europe",
            n_keywords=4
        )
        
        assert len(kws) == 4
        # Verify generic keywords are prepended with primary target role title
        assert any(r in kws[0].lower() for r in ["founder", "ceo", "cto", "head", "vp", "director", "manager"])


@pytest.mark.django_db
def test_generate_search_keywords_fallback_construction():
    """Verify fallback query construction produces role-title centered queries when LLM fails."""
    with patch("openoutreach.core.llm.get_llm_model", side_effect=RuntimeError("LLM unavailable")):
        kws = generate_search_keywords(
            target_job_titles="Founder, CEO, CTO",
            target_industries="Software",
            target_locations="United Kingdom",
            n_keywords=3
        )
        assert len(kws) == 3
        assert any(role in kws[0].lower() for role in ["founder", "ceo", "cto", "head", "vp", "director", "manager"])
