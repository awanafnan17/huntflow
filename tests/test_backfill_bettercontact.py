# tests/test_backfill_bettercontact.py
"""Tests for BetterContact email resolution and backfill command (Batch 5.7)."""
from __future__ import annotations

import pytest
from io import StringIO
from unittest.mock import patch, MagicMock

from django.core.management import call_command
from django.urls import reverse

from openoutreach.core.models import Campaign, SiteConfig, Task
from openoutreach.crm.models import Deal, DealState, Lead
from tests.factories import UserFactory, LeadFactory, DealFactory


@pytest.mark.django_db
class TestBetterContactFieldsExtraction:

    def test_no_fake_name_or_company_from_public_identifier(self):
        """get_bettercontact_fields should not extract first/last name or company from vanity URL identifier."""
        lead = Lead.objects.create(
            public_identifier="sarah-dryrun",
            linkedin_url="https://linkedin.com/in/sarah-dryrun",
        )
        fields = lead.get_bettercontact_fields()
        assert fields["first_name"] == ""
        assert fields["last_name"] == ""
        assert fields["company"] == ""
        assert fields["company_domain"] == ""

    def test_uses_linkedin_url_only_when_no_reliable_facts(self):
        """BetterContact lookup uses only URL if no facts are materialized."""
        lead = Lead.objects.create(
            public_identifier="john-doe",
            linkedin_url="https://linkedin.com/in/john-doe",
        )
        cfg = SiteConfig.load()
        cfg.bettercontact_api_key = "bc-key-123"
        cfg.save()

        with patch("openoutreach.emails.bettercontact.resolve_email") as mock_resolve:
            mock_resolve.return_value = None
            lead.resolve_api_email()
            
            # Assert query structure
            query_arg = mock_resolve.call_args[0][0]
            assert query_arg.linkedin_url == lead.linkedin_url
            assert query_arg.first_name == ""
            assert query_arg.last_name == ""
            assert query_arg.company == ""

    def test_extracts_reliable_facts_when_available(self):
        """Name and company are populated into BetterContactQuery if present in profile_summary facts."""
        lead = Lead.objects.create(
            public_identifier="alice-smith",
            linkedin_url="https://linkedin.com/in/alice-smith",
        )
        campaign = Campaign.objects.create(name="Austin campaign")
        deal = Deal.objects.create(
            lead=lead,
            campaign=campaign,
            state=DealState.QUALIFIED,
            profile_summary={
                "facts": [
                    "First name is Alice.",
                    "Last name is Smith.",
                    "Works at Acme Corp.",
                ]
            }
        )
        
        cfg = SiteConfig.load()
        cfg.bettercontact_api_key = "bc-key-123"
        cfg.save()

        with patch("openoutreach.emails.bettercontact.resolve_email") as mock_resolve:
            mock_resolve.return_value = None
            lead.resolve_api_email()
            
            query_arg = mock_resolve.call_args[0][0]
            assert query_arg.first_name == "Alice"
            assert query_arg.last_name == "Smith"
            assert query_arg.company == "Acme Corp"


@pytest.mark.django_db
class TestResolveEmailNowAction:

    def test_resolve_email_now_requires_login(self, client):
        """Unauthenticated POST to resolve_email redirects to login."""
        lead = LeadFactory(public_identifier="sarah")
        url = reverse("dashboard:lead_detail", kwargs={"pk": lead.pk})
        response = client.post(url, {"action": "resolve_email"})
        assert response.status_code == 302
        assert "/dashboard/login/" in response.url

    def test_resolve_email_now_does_not_send_or_scrape(self, client):
        """Resolve Email Now triggers BetterContact lookup but never sends connections, emails, or scrapes."""
        user = UserFactory(username="staffop", is_staff=True)
        client.force_login(user)

        lead = Lead.objects.create(
            public_identifier="sarah",
            linkedin_url="https://linkedin.com/in/sarah",
        )
        campaign = Campaign.objects.create(name="Email Resolve Campaign")
        deal = Deal.objects.create(lead=lead, campaign=campaign, state=DealState.QUALIFIED)

        url = reverse("dashboard:lead_detail", kwargs={"pk": lead.pk})
        
        with patch("openoutreach.crm.models.lead.Lead.resolve_api_email") as mock_resolve, \
             patch("openoutreach.linkedin.browser.connect_with_note.send_connection_request_with_note") as mock_send, \
             patch("openoutreach.crm.models.lead.Lead.get_profile") as mock_scrape, \
             patch("openoutreach.emails.tasks.send.handle_email") as mock_email:
            
            mock_resolve.return_value = True
            response = client.post(url, {"action": "resolve_email"}, follow=True)
            
            assert response.status_code == 200
            mock_resolve.assert_called_once()
            mock_send.assert_not_called()
            mock_scrape.assert_not_called()
            mock_email.assert_not_called()


@pytest.mark.django_db
class TestBackfillBetterContactCommand:

    def test_backfill_command_dry_run(self):
        """Dry-run option prints what would occur and does not persist changes."""
        lead = Lead.objects.create(
            public_identifier="backfill-lead",
            linkedin_url="https://linkedin.com/in/backfill-lead",
        )
        campaign = Campaign.objects.create(name="Backfill Campaign")
        deal = Deal.objects.create(lead=lead, campaign=campaign, state=DealState.QUALIFIED)

        out = StringIO()
        call_command("backfill_bettercontact", "--dry-run", "--limit", "5", stdout=out)
        
        output = out.getvalue()
        assert "MODE: DRY RUN" in output
        assert "Checking Deal" in output
        
        # Verify db state remains unchanged
        deal.refresh_from_db()
        assert deal.state == DealState.QUALIFIED
        assert not deal.lead.api_email

    def test_backfill_command_success_saves_email(self):
        """Successful backfill lookup updates lead.api_email and transitions deal to READY_TO_EMAIL."""
        lead = Lead.objects.create(
            public_identifier="backfill-success",
            linkedin_url="https://linkedin.com/in/backfill-success",
        )
        campaign = Campaign.objects.create(name="Backfill Campaign")
        deal = Deal.objects.create(lead=lead, campaign=campaign, state=DealState.QUALIFIED)

        cfg = SiteConfig.load()
        cfg.bettercontact_api_key = "bc-secret-key"
        cfg.save()

        out = StringIO()
        
        from openoutreach.emails.bettercontact import BetterContactResult
        with patch("openoutreach.emails.bettercontact.resolve_email") as mock_resolve:
            mock_resolve.return_value = BetterContactResult(email="resolved@test.com", status="valid")
            call_command("backfill_bettercontact", "--limit", "5", stdout=out)

        output = out.getvalue()
        assert "SUCCESS: Resolved email: resolved@test.com" in output
        assert "Transitioned Deal state to READY_TO_EMAIL" in output
        
        # Verify db changes
        deal.refresh_from_db()
        assert deal.state == DealState.READY_TO_EMAIL
        assert deal.lead.api_email == "resolved@test.com"

    def test_api_key_not_exposed(self, caplog):
        """API key never appears in outputs or logs."""
        lead = Lead.objects.create(
            public_identifier="backfill-error",
            linkedin_url="https://linkedin.com/in/backfill-error",
        )
        campaign = Campaign.objects.create(name="Backfill Campaign")
        Deal.objects.create(lead=lead, campaign=campaign, state=DealState.QUALIFIED)

        cfg = SiteConfig.load()
        cfg.bettercontact_api_key = "sk-bettercontact-secret-12345"
        cfg.save()

        out = StringIO()
        with patch("openoutreach.emails.bettercontact.resolve_email") as mock_resolve:
            mock_resolve.side_effect = Exception("API key sk-bettercontact-secret-12345 is bad")
            
            import logging
            with caplog.at_level(logging.ERROR):
                call_command("backfill_bettercontact", "--limit", "5", stdout=out)

        output = out.getvalue()
        assert "sk-bettercontact-secret-12345" not in output
        
        # Verify logs contain masked API key
        found_err = False
        for record in caplog.records:
            assert "sk-bettercontact-secret-12345" not in record.message
            if "BetterContact check" in record.message:
                assert "********" in record.message
                found_err = True
        assert found_err


@pytest.mark.django_db
class TestInlineNewlyQualifiedLookup:

    def test_existing_inline_lookup_still_works(self):
        """Newly qualified leads continue triggering inline BetterContact resolution correctly."""
        from openoutreach.linkedin.pipeline.qualify import _resolve_email
        from openoutreach.emails.models import Mailbox
        
        # Setup mailbox to satisfy has_mailbox() requirement
        Mailbox.objects.create(
            host="smtp.test.com", port=587, username="send@test.com",
            password="pwd", from_address="send@test.com",
        )
        
        lead = Lead.objects.create(
            public_identifier="inline-lead",
            linkedin_url="https://linkedin.com/in/inline-lead",
        )
        
        cfg = SiteConfig.load()
        cfg.bettercontact_api_key = "bc-inline-key"
        cfg.save()

        with patch("openoutreach.crm.models.lead.Lead.resolve_api_email") as mock_resolve:
            mock_resolve.return_value = True
            result = _resolve_email(None, lead)
            
            assert result is True
            mock_resolve.assert_called_once()
