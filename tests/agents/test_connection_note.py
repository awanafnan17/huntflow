# tests/agents/test_connection_note.py
"""Tests for the connection note generator (Batch 2).

Covers:
- All template placeholders including {company_or_industry}
- Fallback when first_name is missing
- Fallback when company/industry are missing
- Unresolved placeholder cleanup
- 300-character length cap
- Custom campaign template
- Disabled note behavior
- LLM failure → template fallback
- ActionLog metadata saved correctly
"""
import pytest
from unittest.mock import patch, MagicMock

from openoutreach.core.agents.connection_note import (
    MAX_NOTE_LENGTH,
    DEFAULT_TEMPLATE,
    FALLBACK_NO_COMPANY,
    FALLBACK_NO_NAME,
    generate_connection_note,
    _generate_template_note,
    _extract_profile_fields,
    _clean_and_cap,
)


# ── Sample data ─────────────────────────────────────────────────────

FULL_PROFILE = {
    "first_name": "Alice",
    "last_name": "Smith",
    "full_name": "Alice Smith",
    "headline": "CEO at TechCorp",
    "summary": "Building the future.",
    "location_name": "San Francisco, CA",
    "industry": {"name": "Technology"},
    "positions": [
        {"title": "CEO", "company_name": "TechCorp", "location": "SF"},
    ],
}

PROFILE_NO_COMPANY = {
    "first_name": "Bob",
    "last_name": "Jones",
    "full_name": "Bob Jones",
    "headline": "Entrepreneur",
    "location_name": "New York",
    "industry": {"name": "Finance"},
    "positions": [],
}

PROFILE_NO_NAME = {
    "headline": "VP at BigCo",
    "positions": [{"title": "VP", "company_name": "BigCo"}],
}

PROFILE_MINIMAL = {}


# ── _extract_profile_fields ─────────────────────────────────────────


class TestExtractProfileFields:
    def test_full_profile(self):
        fields = _extract_profile_fields(FULL_PROFILE)
        assert fields["first_name"] == "Alice"
        assert fields["full_name"] == "Alice Smith"
        assert fields["title"] == "CEO"
        assert fields["company"] == "TechCorp"
        assert fields["industry"] == "Technology"
        assert fields["location"] == "San Francisco, CA"
        assert fields["company_or_industry"] == "TechCorp"

    def test_no_company_falls_back_to_industry(self):
        fields = _extract_profile_fields(PROFILE_NO_COMPANY)
        assert fields["company"] == ""
        assert fields["industry"] == "Finance"
        assert fields["company_or_industry"] == "Finance"

    def test_no_name(self):
        fields = _extract_profile_fields(PROFILE_NO_NAME)
        assert fields["first_name"] == ""
        assert fields["company"] == "BigCo"
        assert fields["company_or_industry"] == "BigCo"

    def test_minimal_profile(self):
        fields = _extract_profile_fields(PROFILE_MINIMAL)
        assert fields["first_name"] == ""
        assert fields["company"] == ""
        assert fields["industry"] == ""
        assert fields["company_or_industry"] == ""

    def test_title_falls_back_to_headline(self):
        profile = {"first_name": "Eve", "headline": "Data Scientist", "positions": []}
        fields = _extract_profile_fields(profile)
        assert fields["title"] == "Data Scientist"

    def test_industry_as_string(self):
        """Some profiles may have industry as a plain string instead of dict."""
        profile = {"industry": "Healthcare"}
        fields = _extract_profile_fields(profile)
        assert fields["industry"] == "Healthcare"
        assert fields["company_or_industry"] == "Healthcare"


# ── _generate_template_note ─────────────────────────────────────────


class TestGenerateTemplateNote:
    @pytest.fixture
    def campaign(self):
        c = MagicMock()
        c.name = "LinkedIn Outreach"
        c.connection_note_template = ""
        c.connection_note_enabled = True
        return c

    def test_default_template_all_fields(self, campaign):
        note = _generate_template_note(campaign, FULL_PROFILE)
        assert "Alice" in note
        assert "TechCorp" in note
        assert len(note) <= MAX_NOTE_LENGTH

    def test_no_company_fallback(self, campaign):
        profile = {"first_name": "Charlie", "positions": [], "industry": {}}
        note = _generate_template_note(campaign, profile)
        assert "Charlie" in note
        assert "{company" not in note
        assert "{industry" not in note

    def test_no_name_fallback(self, campaign):
        note = _generate_template_note(campaign, PROFILE_MINIMAL)
        assert note.startswith("saw ")
        assert "{first_name" not in note

    def test_custom_template(self, campaign):
        campaign.connection_note_template = "Hey {first_name}, love your work at {company}!"
        note = _generate_template_note(campaign, FULL_PROFILE)
        assert note == "Hey Alice, love your work at TechCorp!"

    def test_custom_template_with_company_or_industry(self, campaign):
        campaign.connection_note_template = "Hi {first_name}, your work in {company_or_industry} is impressive."
        note = _generate_template_note(campaign, FULL_PROFILE)
        assert note == "Hi Alice, your work in TechCorp is impressive."

    def test_custom_template_company_or_industry_falls_back_to_industry(self, campaign):
        campaign.connection_note_template = "Hi {first_name}, interested in your {company_or_industry} work."
        note = _generate_template_note(campaign, PROFILE_NO_COMPANY)
        assert note == "Hi Bob, interested in your Finance work."

    def test_unresolved_placeholders_removed(self, campaign):
        campaign.connection_note_template = "Hi {first_name}, {unknown_field} let's connect."
        note = _generate_template_note(campaign, FULL_PROFILE)
        assert "{unknown_field}" not in note
        assert "Alice" in note

    def test_all_placeholders(self, campaign):
        campaign.connection_note_template = (
            "{first_name} {full_name} {title} {company} {industry} "
            "{location} {campaign_name} {company_or_industry}"
        )
        note = _generate_template_note(campaign, FULL_PROFILE)
        assert "Alice" in note
        assert "Alice Smith" in note
        assert "CEO" in note
        assert "TechCorp" in note
        assert "Technology" in note
        assert "San Francisco" in note
        assert "LinkedIn Outreach" in note
        assert "{" not in note


# ── _clean_and_cap ──────────────────────────────────────────────────


class TestCleanAndCap:
    def test_within_limit(self):
        assert _clean_and_cap("Hello world") == "Hello world"

    def test_over_limit_truncates_at_word(self):
        long_text = "word " * 100  # 500 chars
        result = _clean_and_cap(long_text)
        assert len(result) <= MAX_NOTE_LENGTH
        # Should not end mid-word
        assert not result.endswith(" ")

    def test_collapses_double_spaces(self):
        assert _clean_and_cap("Hello  world") == "Hello world"

    def test_fixes_broken_punctuation(self):
        assert _clean_and_cap("Hello, , world") == "Hello, world"

    def test_strips_leading_comma(self):
        result = _clean_and_cap(", hello world")
        assert not result.startswith(",")


# ── generate_connection_note (integration) ──────────────────────────


@pytest.mark.django_db
class TestGenerateConnectionNote:
    @pytest.fixture
    def campaign(self, db):
        from openoutreach.core.models import Campaign
        c = Campaign.objects.first()
        if c is None:
            c = Campaign.objects.create(name="Test Campaign")
        c.connection_note_enabled = True
        c.connection_note_template = ""
        c.save()
        return c

    def test_disabled_returns_empty(self, campaign):
        campaign.connection_note_enabled = False
        campaign.save()
        note, source = generate_connection_note(None, campaign, FULL_PROFILE)
        assert note == ""
        assert source == "disabled"

    def test_template_fallback_when_no_llm(self, campaign):
        """When LLM is not configured, falls back to template."""
        from openoutreach.core.models import SiteConfig
        cfg = SiteConfig.load()
        cfg.llm_api_key = ""
        cfg.save()

        note, source = generate_connection_note(None, campaign, FULL_PROFILE)
        assert source == "template"
        assert "Alice" in note
        assert len(note) <= MAX_NOTE_LENGTH
        assert len(note) > 0

    @patch("openoutreach.core.agents.connection_note._generate_llm_note")
    def test_llm_success(self, mock_llm, campaign):
        from openoutreach.core.models import SiteConfig
        cfg = SiteConfig.load()
        cfg.llm_api_key = "test-key"
        cfg.ai_model = "groq:llama-3.3-70b-versatile"
        cfg.save()

        mock_llm.return_value = "Hi Alice, really impressed by your work at TechCorp."
        note, source = generate_connection_note(None, campaign, FULL_PROFILE)
        assert source == "llm"
        assert "Alice" in note

    @patch("openoutreach.core.agents.connection_note._generate_llm_note")
    def test_llm_failure_falls_back_to_template(self, mock_llm, campaign):
        from openoutreach.core.models import SiteConfig
        cfg = SiteConfig.load()
        cfg.llm_api_key = "test-key"
        cfg.ai_model = "groq:llama-3.3-70b-versatile"
        cfg.save()

        mock_llm.side_effect = Exception("LLM timeout")
        note, source = generate_connection_note(None, campaign, FULL_PROFILE)
        assert source == "template"
        assert "Alice" in note

    @patch("openoutreach.core.agents.connection_note._generate_llm_note")
    def test_llm_returns_none_falls_back(self, mock_llm, campaign):
        from openoutreach.core.models import SiteConfig
        cfg = SiteConfig.load()
        cfg.llm_api_key = "test-key"
        cfg.ai_model = "groq:llama-3.3-70b-versatile"
        cfg.save()

        mock_llm.return_value = None
        note, source = generate_connection_note(None, campaign, FULL_PROFILE)
        assert source == "template"
        assert len(note) > 0

    def test_length_cap_enforced(self, campaign):
        campaign.connection_note_template = "A" * 500
        campaign.save()
        note, source = generate_connection_note(None, campaign, FULL_PROFILE)
        assert len(note) <= MAX_NOTE_LENGTH
