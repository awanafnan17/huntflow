# tests/test_profile_identity.py
"""Tests for profile identity persistence, BetterContact integration, and backfill command (Batch 5.9)."""
from __future__ import annotations

import pytest
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.urls import reverse

from openoutreach.core.models import Campaign, SiteConfig
from openoutreach.crm.models import Deal, DealState, Lead
from tests.factories import UserFactory


@pytest.mark.django_db
class TestProfileIdentityModelFields:

    def test_identity_fields_saved_from_nested_profile_dict(self):
        """Lead.update_identity_from_profile extracts fields from nested profile layout."""
        lead = Lead.objects.create(
            public_identifier="bob-jones",
            linkedin_url="https://linkedin.com/in/bob-jones",
        )
        profile_data = {
            "first_name": "Bob",
            "last_name": "Jones",
            "profile": {
                "headline": "Lead Designer",
                "location_name": "New York",
                "industry": {"name": "Design"},
                "positions": [
                    {
                        "title": "Senior Designer",
                        "company_name": "Acme Design",
                    }
                ]
            }
        }
        lead.update_identity_from_profile(profile_data)
        lead.refresh_from_db()

        assert lead.first_name == "Bob"
        assert lead.last_name == "Jones"
        assert lead.headline == "Lead Designer"
        assert lead.current_title == "Senior Designer"
        assert lead.company == "Acme Design"
        assert lead.location == "New York"
        assert lead.industry == "Design"

    def test_identity_fields_saved_from_flat_profile_dict(self):
        """Lead.update_identity_from_profile extracts fields from flat profile layout (e.g. mocks)."""
        lead = Lead.objects.create(
            public_identifier="bob-jones",
            linkedin_url="https://linkedin.com/in/bob-jones",
        )
        profile_data = {
            "first_name": "Bob",
            "last_name": "Jones",
            "headline": "Lead Designer",
            "positions": [
                {
                    "title": "Senior Designer",
                    "company_name": "Acme Design",
                }
            ],
            "location_name": "New York",
            "industry": "Design",
        }
        lead.update_identity_from_profile(profile_data)
        lead.refresh_from_db()

        assert lead.first_name == "Bob"
        assert lead.last_name == "Jones"
        assert lead.headline == "Lead Designer"
        assert lead.current_title == "Senior Designer"
        assert lead.company == "Acme Design"
        assert lead.location == "New York"
        assert lead.industry == "Design"

    def test_no_identity_inferred_from_public_identifier(self):
        """Lead properties should never infer name/company from the public_identifier vanity slug."""
        lead = Lead.objects.create(
            public_identifier="alice-smith-acme",
            linkedin_url="https://linkedin.com/in/alice-smith-acme",
        )
        assert lead.first_name == ""
        assert lead.last_name == ""
        assert lead.company == ""


@pytest.mark.django_db
class TestBetterContactIntegrationWithIdentityFields:

    def test_bettercontact_gets_identity_fields_when_available(self):
        """BetterContact lookup uses first_name, last_name, and company from database if populated."""
        lead = Lead.objects.create(
            public_identifier="bob-jones",
            linkedin_url="https://linkedin.com/in/bob-jones",
            first_name="Bob",
            last_name="Jones",
            company="Acme Corp",
        )
        cfg = SiteConfig.load()
        cfg.bettercontact_api_key = "bc-key"
        cfg.save()

        with patch("openoutreach.emails.bettercontact.resolve_email") as mock_resolve:
            mock_resolve.return_value = None
            lead.resolve_api_email()

            query_arg = mock_resolve.call_args[0][0]
            assert query_arg.first_name == "Bob"
            assert query_arg.last_name == "Jones"
            assert query_arg.company == "Acme Corp"
            assert query_arg.linkedin_url == lead.linkedin_url

    def test_bettercontact_falls_back_to_url_only_when_unavailable(self):
        """BetterContact lookup falls back to URL-only query if identity fields are blank."""
        lead = Lead.objects.create(
            public_identifier="bob-jones",
            linkedin_url="https://linkedin.com/in/bob-jones",
        )
        cfg = SiteConfig.load()
        cfg.bettercontact_api_key = "bc-key"
        cfg.save()

        with patch("openoutreach.emails.bettercontact.resolve_email") as mock_resolve:
            mock_resolve.return_value = None
            lead.resolve_api_email()

            query_arg = mock_resolve.call_args[0][0]
            assert query_arg.first_name == ""
            assert query_arg.last_name == ""
            assert query_arg.company == ""
            assert query_arg.linkedin_url == lead.linkedin_url


@pytest.mark.django_db
class TestProfileIdentityDashboardDisplays:

    def test_dashboard_displays_saved_title_and_company(self, client):
        """Dashboard Lead Detail displays the persisted identity fields, or full name."""
        user = UserFactory(username="staffop", is_staff=True)
        client.force_login(user)

        lead = Lead.objects.create(
            public_identifier="bob-jones",
            linkedin_url="https://linkedin.com/in/bob-jones",
            first_name="Bob",
            last_name="Jones",
            current_title="Director of Engineering",
            company="Acme Corp",
            location="New York",
            industry="Engineering",
        )

        # 1. Leads listing page
        response = client.get(reverse("dashboard:leads"))
        assert response.status_code == 200
        content = response.content.decode("utf-8")
        assert "Bob Jones" in content
        assert "Director of Engineering" in content
        assert "Acme Corp" in content

        # 2. Lead Detail page
        response = client.get(reverse("dashboard:lead_detail", kwargs={"pk": lead.pk}))
        assert response.status_code == 200
        content_detail = response.content.decode("utf-8")
        assert "Bob Jones" in content_detail
        assert "Director of Engineering" in content_detail
        assert "Acme Corp" in content_detail
        assert "New York" in content_detail
        assert "Engineering" in content_detail


@pytest.mark.django_db
class TestBackfillProfileIdentityCommand:

    def test_backfill_profile_identity_dry_run_does_not_save(self):
        """Running backfill_profile_identity with --dry-run prints updates without saving changes."""
        lead = Lead.objects.create(
            public_identifier="backfill-lead",
            linkedin_url="https://linkedin.com/in/backfill-lead",
            contact_info={
                "first_name": "Sarah",
                "last_name": "Smith",
                "company": "Acme Widgets",
            }
        )

        out = StringIO()
        call_command("backfill_profile_identity", "--dry-run", "--limit", "10", stdout=out)

        output = out.getvalue()
        assert "MODE: DRY RUN" in output
        assert "Sarah" in output
        assert "Acme Widgets" in output

        # Verify database fields remain blank
        lead.refresh_from_db()
        assert lead.first_name == ""
        assert lead.last_name == ""
        assert lead.company == ""

    def test_backfill_profile_identity_success(self):
        """Running backfill_profile_identity persists changes successfully."""
        lead = Lead.objects.create(
            public_identifier="backfill-lead",
            linkedin_url="https://linkedin.com/in/backfill-lead",
            contact_info={
                "first_name": "Sarah",
                "last_name": "Smith",
                "company": "Acme Widgets",
            }
        )

        out = StringIO()
        call_command("backfill_profile_identity", "--limit", "10", stdout=out)

        # Verify database fields are populated
        lead.refresh_from_db()
        assert lead.first_name == "Sarah"
        assert lead.last_name == "Smith"
        assert lead.company == "Acme Widgets"
