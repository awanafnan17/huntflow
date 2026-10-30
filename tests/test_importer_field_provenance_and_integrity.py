# tests/test_importer_field_provenance_and_integrity.py
"""Regression tests proving importer field provenance and schema integrity (Section 28)."""
import pytest
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from openoutreach.core.models import Workspace, Campaign
from openoutreach.crm.models.lead import Lead
from openoutreach.linkedin.models import LinkedInProfile
from openoutreach.linkedin.services.discovery_service import discover_campaign_targets
from openoutreach.linkedin.services.enrichment_service import enrich_linkedin_lead


@pytest.fixture
def setup_provenance_env(db):
    user = User.objects.create_user(username="prov_user", email="prov@example.com")
    ws = Workspace.objects.create(name="Provenance WS", slug="prov-ws")
    lp = LinkedInProfile.objects.create(
        user=user,
        workspace=ws,
        linkedin_username="prov.user@example.com",
        session_status="valid",
        active=True,
    )
    campaign = Campaign.objects.create(
        workspace=ws,
        name="Provenance Campaign",
        linkedin_profile=lp,
        is_active=True,
    )
    return {"user": user, "workspace": ws, "campaign": campaign, "profile": lp}


@pytest.mark.django_db
def test_discovery_does_not_store_headline_in_country_code(setup_provenance_env):
    """Headline and location must be stored in their canonical fields, never in country_code."""
    env = setup_provenance_env
    campaign = env["campaign"]

    search_cards = [
        {
            "url": "https://www.linkedin.com/in/jane-techfounder",
            "public_identifier": "jane-techfounder",
            "name": "Jane Doe",
            "headline": "Founder & CEO, TechCorp | Building AI Platforms",
            "location": "Toronto, Ontario, Canada",
        }
    ]

    mock_session = MagicMock()
    mock_session.page = MagicMock()
    mock_session.page.url = "https://www.linkedin.com/feed/"

    with patch("linkedin_cli.actions.search.search_people", return_value={"profiles": search_cards}):
        res = discover_campaign_targets(campaign, limit=10, session=mock_session)

    assert res["status"] == "success"
    lead = Lead.objects.get(public_identifier="jane-techfounder")

    assert lead.first_name == "Jane"
    assert lead.last_name == "Doe"
    assert lead.headline == "Founder & CEO, TechCorp | Building AI Platforms"
    assert lead.current_title == "Founder & CEO, TechCorp | Building AI Platforms"
    assert lead.location == "Toronto, Ontario, Canada"
    assert lead.country_code == ""  # Not corrupted with location or headline
    assert len(lead.country_code) <= 2
    assert lead.workspace == env["workspace"]


@pytest.mark.django_db
def test_discovery_rejects_relationship_degree_badges(setup_provenance_env):
    """Relationship degree badges like '· 1st', '· 2nd', '3rd+' must never become current_title."""
    env = setup_provenance_env
    campaign = env["campaign"]

    search_cards = [
        {
            "url": "https://www.linkedin.com/in/bad-degree-lead",
            "public_identifier": "bad-degree-lead",
            "name": "Alex Smith",
            "headline": "· 2nd",
            "title": "· 2nd",
            "location": "London, UK",
        },
        {
            "url": "https://www.linkedin.com/in/degree-lead-2",
            "public_identifier": "degree-lead-2",
            "name": "Bob Taylor",
            "headline": "1st degree",
            "location": "Manchester, UK",
        }
    ]

    mock_session = MagicMock()
    mock_session.page = MagicMock()
    mock_session.page.url = "https://www.linkedin.com/feed/"

    with patch("linkedin_cli.actions.search.search_people", return_value={"profiles": search_cards}):
        res = discover_campaign_targets(campaign, limit=10, session=mock_session)

    assert res["status"] == "success"
    lead1 = Lead.objects.get(public_identifier="bad-degree-lead")
    assert lead1.current_title == ""
    assert lead1.headline == ""
    assert lead1.location == "London, UK"
    assert lead1.country_code == ""

    lead2 = Lead.objects.get(public_identifier="degree-lead-2")
    assert lead2.current_title == ""
    assert lead2.headline == ""
    assert lead2.location == "Manchester, UK"


@pytest.mark.django_db
def test_enrichment_preserves_field_provenance_and_strict_country_code(setup_provenance_env, mocker):
    """Enrichment service maps location to location, and only populates 2-letter ISO country_code."""
    env = setup_provenance_env
    ws = env["workspace"]
    lead = Lead.objects.create(
        public_identifier="enrich-lead-test",
        linkedin_url="https://www.linkedin.com/in/enrich-lead-test/",
        workspace=ws,
    )

    mock_session = mocker.MagicMock()
    mock_api = mocker.MagicMock()
    mock_api.get_profile.return_value = (
        {
            "first_name": "Devon",
            "last_name": "Miles",
            "headline": "VP Engineering",
            "current_title": "· 1st",  # Malformed title from badge
            "company": "AutoCorp",
            "country_code": "us",
            "location": "Austin, Texas, United States",
            "public_identifier": "enrich-lead-test",
        },
        {}
    )
    mocker.patch("linkedin_cli.api.client.PlaywrightLinkedinAPI", return_value=mock_api)

    res = enrich_linkedin_lead(lead, mock_session)
    assert res["status"] == "ENRICHED"

    lead.refresh_from_db()
    assert lead.first_name == "Devon"
    assert lead.last_name == "Miles"
    assert lead.headline == "VP Engineering"
    assert lead.current_title == ""  # Rejected relationship badge
    assert lead.company == "AutoCorp"
    assert lead.country_code == "us"
    assert lead.location == "Austin, Texas, United States"


@pytest.mark.django_db
def test_missing_fields_remain_empty_without_column_shift(setup_provenance_env):
    """Missing fields in search card payload remain empty rather than shifting into wrong columns."""
    env = setup_provenance_env
    campaign = env["campaign"]

    search_cards = [
        {
            "url": "https://www.linkedin.com/in/sparse-lead",
            "public_identifier": "sparse-lead",
            "name": "Only Name",
            # headline missing
            # location missing
            # company missing
        }
    ]

    mock_session = MagicMock()
    mock_session.page = MagicMock()
    mock_session.page.url = "https://www.linkedin.com/feed/"

    with patch("linkedin_cli.actions.search.search_people", return_value={"profiles": search_cards}):
        discover_campaign_targets(campaign, limit=10, session=mock_session)

    lead = Lead.objects.get(public_identifier="sparse-lead")
    assert lead.first_name == "Only"
    assert lead.last_name == "Name"
    assert lead.headline == ""
    assert lead.current_title == ""
    assert lead.company == ""
    assert lead.location == ""
    assert lead.country_code == ""
    assert lead.workspace == env["workspace"]
