# tests/test_diagnose_outreach.py
"""Tests for the diagnose_outreach management command (Batch 5.5).

Covers:
- Command runs without errors on an empty database
- Command runs with sample data and produces expected sections
- No secrets appear in output
- Note preview section appears when connection notes are enabled
"""
import pytest
from io import StringIO

from django.core.management import call_command

from openoutreach.core.models import Campaign, SiteConfig, Task
from openoutreach.crm.models import Deal, DealState, Lead
from openoutreach.linkedin.models import ActionLog, LinkedInProfile, SearchKeyword
from tests.factories import UserFactory


@pytest.mark.django_db
class TestDiagnoseOutreachCommand:

    def _run(self) -> str:
        """Run diagnose_outreach and capture stdout."""
        out = StringIO()
        call_command("diagnose_outreach", stdout=out)
        return out.getvalue()

    def test_runs_on_empty_db(self):
        """Command should not crash with no data."""
        output = self._run()
        assert "Diagnostic report complete." in output

    def test_sections_present(self):
        """Expected section headers appear in output."""
        output = self._run()
        for section in ["Accounts", "Campaigns", "Funnel", "Tasks", "Recent Logs", "Blocking Reasons"]:
            assert section in output

    def test_with_sample_data(self):
        """Command shows campaign and account data."""
        user = UserFactory(username="operator1")
        profile = LinkedInProfile.objects.create(
            user=user,
            linkedin_username="operator1@example.com",
            linkedin_password="secret_password_123",
            active=True,
            connect_daily_limit=20,
        )
        campaign = Campaign.objects.create(
            name="Austin Outreach",
            connection_note_enabled=True,
        )
        campaign.users.add(user)

        lead = Lead.objects.create(
            public_identifier="jane-doe",
            linkedin_url="https://linkedin.com/in/jane-doe",
        )
        Deal.objects.create(
            lead=lead, campaign=campaign, state=DealState.QUALIFIED,
        )

        output = self._run()
        assert "Austin Outreach" in output
        assert "operator1@example.com" in output
        assert "Qualified" in output

    def test_no_secrets_in_output(self):
        """LinkedIn passwords, LLM keys, and BetterContact keys must not appear."""
        user = UserFactory(username="secuser")
        LinkedInProfile.objects.create(
            user=user,
            linkedin_username="secuser@test.com",
            linkedin_password="my_super_secret_pw",
            active=True,
        )
        cfg = SiteConfig.load()
        cfg.llm_api_key = "test-1-aaaaaaaa"
        cfg.bettercontact_api_key = "test-1-aaaaaaaa"
        cfg.save()

        output = self._run()
        assert "my_super_secret_pw" not in output
        assert "test-1-aaaaaaaa" not in output
        assert "test-1-aaaaaaaa" not in output
        # LLM status should show as Configured
        assert "Configured" in output

    def test_note_preview_when_enabled(self):
        """Note preview section appears when connection notes are enabled."""
        user = UserFactory(username="noteuser")
        campaign = Campaign.objects.create(
            name="Note Preview Campaign",
            connection_note_enabled=True,
            connection_note_template="Hi {first_name}, let us connect.",
        )
        campaign.users.add(user)

        output = self._run()
        assert "Sample note preview" in output

    def test_blocking_reasons_shown(self):
        """Blocking reasons are displayed when conditions are met."""
        campaign = Campaign.objects.create(name="Blocked Campaign")
        # No users assigned -> "No assigned users"

        output = self._run()
        assert "Blocked Campaign" in output
        assert "No assigned users" in output

    def test_no_ready_leads_blocking(self):
        """When leads are qualified but none are ready, show 'No ready leads available'."""
        user = UserFactory(username="readyuser")
        profile = LinkedInProfile.objects.create(
            user=user,
            linkedin_username="readyuser@test.com",
            linkedin_password="pw",
            active=True,
        )
        campaign = Campaign.objects.create(name="Ready Test")
        campaign.users.add(user)

        lead = Lead.objects.create(
            public_identifier="qualified-lead",
            linkedin_url="https://linkedin.com/in/qualified-lead",
        )
        Deal.objects.create(lead=lead, campaign=campaign, state=DealState.QUALIFIED)

        output = self._run()
        assert "No ready leads available" in output

    def test_llm_not_configured_blocking(self):
        """When LLM key is empty, blocking reason is shown."""
        user = UserFactory(username="llmuser")
        LinkedInProfile.objects.create(
            user=user,
            linkedin_username="llmuser@test.com",
            linkedin_password="pw",
            active=True,
        )
        campaign = Campaign.objects.create(name="LLM Check")
        campaign.users.add(user)

        cfg = SiteConfig.load()
        cfg.llm_api_key = ""
        cfg.save()

        output = self._run()
        assert "LLM not configured" in output

    def test_search_keywords_shown(self):
        """Recent search keywords appear in output."""
        from django.utils import timezone

        user = UserFactory(username="kwuser")
        campaign = Campaign.objects.create(name="KW Campaign")
        campaign.users.add(user)

        SearchKeyword.objects.create(
            campaign=campaign, keyword="marketing director", used=True, used_at=timezone.now(),
        )

        output = self._run()
        assert "marketing director" in output
