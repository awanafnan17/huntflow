# tests/test_llm_testing.py
"""Tests for LLM configuration testing and campaign readiness checks (Batch 5.6)."""
from __future__ import annotations

import pytest
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.urls import reverse

from openoutreach.core.models import Campaign, SiteConfig, Task
from openoutreach.crm.models import Deal, DealState, Lead
from openoutreach.linkedin.models import LinkedInProfile
from openoutreach.emails.models import Mailbox
from tests.factories import UserFactory


@pytest.mark.django_db
class TestLLMConfigurationTesting:

    def test_test_llm_config_command_success(self):
        """test_llm_config command outputs details and calls verify_llm_credentials."""
        cfg = SiteConfig.load()
        cfg.ai_model = "openai:gpt-4o"
        cfg.llm_api_key = "test-key-123"
        cfg.save()

        out = StringIO()
        with patch("openoutreach.core.llm.verify_llm_credentials") as mock_verify:
            mock_verify.return_value = None
            call_command("test_llm_config", stdout=out)
            mock_verify.assert_called_once_with("openai:gpt-4o", "test-key-123", "")

        output = out.getvalue()
        assert "LLM Configuration Test" in output
        assert "Model: openai:gpt-4o" in output
        assert "Success: LLM Configuration works correctly." in output
        # Secrets check: the key is never printed
        assert "test-key-123" not in output

    def test_test_llm_config_command_failure(self):
        """test_llm_config command prints sanitized failure category and details."""
        cfg = SiteConfig.load()
        cfg.ai_model = "anthropic:claude-sonnet"
        cfg.llm_api_key = "secret_key_abc"
        cfg.save()

        out = StringIO()
        err_out = StringIO()
        with patch("openoutreach.core.llm.verify_llm_credentials") as mock_verify:
            mock_verify.return_value = "API key invalid: secret_key_abc is bad"
            call_command("test_llm_config", stdout=out, stderr=err_out)

        err_output = err_out.getvalue()
        assert "Authentication/API key error" in err_output
        assert "secret_key_abc" not in err_output
        assert "********" in err_output

    def test_test_llm_config_command_missing_config(self):
        """Command handles missing config gracefully."""
        cfg = SiteConfig.load()
        cfg.ai_model = ""
        cfg.llm_api_key = ""
        cfg.save()

        out = StringIO()
        err_out = StringIO()
        call_command("test_llm_config", stdout=out, stderr=err_out)

        assert "LLM is not configured" in err_out.getvalue()

    def test_dashboard_llm_test_success(self, client):
        """Clicking 'Test Saved Configuration' on settings triggers verification and redirects."""
        user = UserFactory(username="diagop")
        client.force_login(user)

        cfg = SiteConfig.load()
        cfg.ai_model = "openai:gpt-4o"
        cfg.llm_api_key = "saved-key-xyz"
        cfg.save()

        url = reverse("dashboard:settings_llm")
        with patch("openoutreach.core.llm.verify_llm_credentials") as mock_verify:
            mock_verify.return_value = None
            response = client.post(url, {"action": "test_config"}, follow=True)
            mock_verify.assert_called_once_with("openai:gpt-4o", "saved-key-xyz", "")

        assert response.status_code == 200
        content = response.content.decode("utf-8")
        assert "Success: LLM Configuration works correctly." in content
        assert "saved-key-xyz" not in content

    def test_dashboard_llm_test_error_sanitization(self, client):
        """Dashboard LLM test sanitizes keys and classifies errors safely."""
        user = UserFactory(username="diagop")
        client.force_login(user)

        cfg = SiteConfig.load()
        cfg.ai_model = "google:gemini-pro"
        cfg.llm_api_key = "super_secret_gemini_key"
        cfg.save()

        url = reverse("dashboard:settings_llm")
        with patch("openoutreach.core.llm.verify_llm_credentials") as mock_verify:
            mock_verify.return_value = "unauthorized request with key super_secret_gemini_key"
            response = client.post(url, {"action": "test_config"}, follow=True)

        assert response.status_code == 200
        content = response.content.decode("utf-8")
        assert "Authentication/API key error" in content
        assert "super_secret_gemini_key" not in content
        assert "********" in content


@pytest.mark.django_db
class TestCampaignReadinessChecklist:

    def test_readiness_checklist_renders(self, client):
        """Readiness checklist renders with appropriate Blocked/Warning/Pass statuses."""
        user = UserFactory(username="diagop")
        client.force_login(user)

        campaign = Campaign.objects.create(
            name="Ready Campaign",
            product_docs="",
            campaign_objective="",
            connection_note_enabled=True,
        )
        campaign.users.add(user)

        # No active LinkedIn account -> Blocked
        # Empty docs/objective -> Warning
        # LLM not set -> Blocked
        cfg = SiteConfig.load()
        cfg.llm_api_key = ""
        cfg.save()

        url = reverse("dashboard:diagnostics")
        response = client.get(url)
        assert response.status_code == 200
        content = response.content.decode("utf-8")

        assert "Has assigned active LinkedIn account" in content
        assert "Has LLM configured" in content
        assert "Has campaign product docs" in content
        assert "Has campaign objective" in content
        assert "Blocked" in content
        assert "Warning" in content

    def test_no_outreach_or_scrapers_called_on_diagnostics(self, client):
        """Loading the diagnostics page must not invoke any LLM, scraper or LinkedIn send methods."""
        user = UserFactory(username="diagop")
        client.force_login(user)

        campaign = Campaign.objects.create(name="Ready Check Campaign", connection_note_enabled=True)
        campaign.users.add(user)

        with patch("openoutreach.linkedin.browser.connect_with_note.send_connection_request_with_note") as mock_send, \
             patch("openoutreach.crm.models.lead.Lead.get_profile") as mock_scrape, \
             patch("openoutreach.core.llm.verify_llm_credentials") as mock_verify:
            
            response = client.get(reverse("dashboard:diagnostics"))
            assert response.status_code == 200
            
            mock_send.assert_not_called()
            mock_scrape.assert_not_called()
            mock_verify.assert_not_called()

    def test_qualification_visibility_lists(self, client):
        """Diagnostics page renders up to 10 qualified/ready, disqualified leads, and keywords."""
        user = UserFactory(username="diagop")
        client.force_login(user)

        campaign = Campaign.objects.create(name="Funnel View Campaign")
        campaign.users.add(user)

        # Add 11 qualified leads
        from django.utils import timezone
        from datetime import timedelta
        for i in range(11):
            lead = Lead.objects.create(
                public_identifier=f"lead-q-{i}",
                linkedin_url=f"https://linkedin.com/in/lead-q-{i}",
            )
            deal = Deal.objects.create(lead=lead, campaign=campaign, state=DealState.QUALIFIED)
            Deal.objects.filter(pk=deal.pk).update(update_date=timezone.now() - timedelta(minutes=(20 - i)))

        url = reverse("dashboard:diagnostics")
        response = client.get(url)
        content = response.content.decode("utf-8")
        assert response.status_code == 200

        # Verify links for qualified leads
        # The 10 newest (lead-q-10 to lead-q-1) should be present
        assert "lead-q-10" in content
        assert "lead-q-1" in content
        # The oldest (lead-q-0) should have been sliced off because of the [:10] limit
        assert "lead-q-0" not in content
