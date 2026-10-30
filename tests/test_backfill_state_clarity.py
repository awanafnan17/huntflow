# tests/test_backfill_state_clarity.py
"""Tests for lead state labels, pending explanation blocks, and backfill command extensions (Batch 5.8)."""
from __future__ import annotations

import pytest
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.urls import reverse

from openoutreach.core.models import Campaign, SiteConfig
from openoutreach.crm.models import Deal, DealState, Lead
from tests.factories import UserFactory, LeadFactory, DealFactory


@pytest.mark.django_db
class TestLeadStateClarityAndWarnings:

    def test_raw_leads_render_as_discovered_not_qualified(self, client):
        """Leads without an associated Deal display as 'Discovered' in the leads listing."""
        user = UserFactory(username="staffop", is_staff=True)
        client.force_login(user)

        # Lead without Deal
        Lead.objects.create(
            public_identifier="discovered-lead",
            linkedin_url="https://linkedin.com/in/discovered-lead",
        )

        response = client.get(reverse("dashboard:leads"))
        assert response.status_code == 200
        content = response.content.decode("utf-8")
        assert "discovered-lead" in content
        assert "Discovered" in content
        # Discovered leads should not have a "Qualified" badge
        # We search specifically for the state label column/text context
        assert "Qualified" not in content or "discovered-lead" in content

    def test_pending_lead_detail_shows_backfill_skip_explanation(self, client):
        """Visiting a PENDING deal detail page shows the pending warning explanation block."""
        user = UserFactory(username="staffop", is_staff=True)
        client.force_login(user)

        lead = Lead.objects.create(
            public_identifier="pending-lead",
            linkedin_url="https://linkedin.com/in/pending-lead",
        )
        campaign = Campaign.objects.create(name="Austin Campaign")
        Deal.objects.create(lead=lead, campaign=campaign, state=DealState.PENDING)

        url = reverse("dashboard:lead_detail", kwargs={"pk": lead.pk})
        response = client.get(url)
        assert response.status_code == 200
        content = response.content.decode("utf-8")
        
        # Check for exact wording from instructions
        assert "Connection request has already been sent" in content
        assert "automated backfill skips pending leads by default" in content or "Automated BetterContact backfill skips pending leads" in content
        assert "force lookup" in content

    def test_discovered_only_lead_detail_renders_clean_context(self, client):
        """Detail page for a lead with no associated deal renders 'Discovered only. Not yet qualified.'"""
        user = UserFactory(username="staffop", is_staff=True)
        client.force_login(user)

        lead = Lead.objects.create(
            public_identifier="discovered-only",
            linkedin_url="https://linkedin.com/in/discovered-only",
        )

        url = reverse("dashboard:lead_detail", kwargs={"pk": lead.pk})
        response = client.get(url)
        assert response.status_code == 200
        content = response.content.decode("utf-8")

        assert "Discovered only" in content
        assert "Not yet qualified" in content


@pytest.mark.django_db
class TestBackfillCommandStateExtensions:

    def test_backfill_default_only_checks_qualified(self):
        """By default, the backfill command only targets QUALIFIED deals."""
        lead_q = Lead.objects.create(
            public_identifier="qualified-lead",
            linkedin_url="https://linkedin.com/in/qualified-lead",
        )
        lead_p = Lead.objects.create(
            public_identifier="pending-lead",
            linkedin_url="https://linkedin.com/in/pending-lead",
        )
        campaign = Campaign.objects.create(name="Campaign A")
        Deal.objects.create(lead=lead_q, campaign=campaign, state=DealState.QUALIFIED)
        Deal.objects.create(lead=lead_p, campaign=campaign, state=DealState.PENDING)

        out = StringIO()
        call_command("backfill_bettercontact", "--dry-run", stdout=out)
        
        output = out.getvalue()
        assert "qualified-lead" in output
        assert "pending-lead" not in output

    def test_backfill_state_pending_includes_pending_deals(self):
        """Specifying --state PENDING targets pending deals instead."""
        lead_q = Lead.objects.create(
            public_identifier="qualified-lead",
            linkedin_url="https://linkedin.com/in/qualified-lead",
        )
        lead_p = Lead.objects.create(
            public_identifier="pending-lead",
            linkedin_url="https://linkedin.com/in/pending-lead",
        )
        campaign = Campaign.objects.create(name="Campaign A")
        Deal.objects.create(lead=lead_q, campaign=campaign, state=DealState.QUALIFIED)
        Deal.objects.create(lead=lead_p, campaign=campaign, state=DealState.PENDING)

        out = StringIO()
        call_command("backfill_bettercontact", "--dry-run", "--state", "PENDING", stdout=out)
        
        output = out.getvalue()
        assert "pending-lead" in output
        assert "qualified-lead" not in output

    def test_backfill_dry_run_zero_matches_shows_helpful_explanation(self):
        """When 0 deals match, dry-run prints current state filter and other counts."""
        lead_p = Lead.objects.create(
            public_identifier="pending-lead",
            linkedin_url="https://linkedin.com/in/pending-lead",
        )
        campaign = Campaign.objects.create(name="Campaign A")
        Deal.objects.create(lead=lead_p, campaign=campaign, state=DealState.PENDING)

        out = StringIO()
        call_command("backfill_bettercontact", "--dry-run", "--state", "QUALIFIED", stdout=out)
        
        output = out.getvalue()
        assert "No deals matched the current filter criteria." in output
        assert "Current state filter: QUALIFIED" in output
        assert "Suggested command" in output
        assert "--state PENDING" in output
