# tests/test_enrichment_service.py
import pytest
from openoutreach.core.models import Workspace
from openoutreach.crm.models.lead import Lead
from openoutreach.linkedin.services.enrichment_service import enrich_linkedin_lead


@pytest.fixture
def test_lead(db):
    ws = Workspace.objects.create(name="Enrich WS", slug="enrich-ws")
    lead = Lead.objects.create(
        public_identifier="test-lead-enrich",
        linkedin_url="https://www.linkedin.com/in/test-lead-enrich/",
        workspace=ws
    )
    return lead


@pytest.mark.django_db
def test_enrich_linkedin_lead_voyager_success(mocker, test_lead):
    """Verify Voyager API success populates Lead fields."""
    mock_session = mocker.MagicMock()
    mock_api = mocker.MagicMock()
    mock_api.get_profile.return_value = (
        {
            "first_name": "Alice",
            "last_name": "Smith",
            "current_title": "CTO & Co-Founder",
            "company": "TechCorp",
            "country_code": "gb",
            "location": "London, United Kingdom",
            "public_identifier": "test-lead-enrich",
        },
        {}
    )
    mocker.patch("linkedin_cli.api.client.PlaywrightLinkedinAPI", return_value=mock_api)

    res = enrich_linkedin_lead(test_lead, mock_session)
    assert res["status"] == "ENRICHED"
    assert res["method"] == "VOYAGER"

    test_lead.refresh_from_db()
    assert test_lead.first_name == "Alice"
    assert test_lead.last_name == "Smith"
    assert test_lead.current_title == "CTO & Co-Founder"
    assert test_lead.company == "TechCorp"
    assert test_lead.country_code == "gb"
    assert test_lead.location == "London, United Kingdom"


@pytest.mark.django_db
def test_enrich_linkedin_lead_browser_fallback(mocker, test_lead):
    """Verify Playwright browser DOM fallback when Voyager throws HTTP 403."""
    mock_session = mocker.MagicMock()
    mock_api = mocker.MagicMock()
    mock_api.get_profile.side_effect = Exception("HTTP 403 Forbidden")
    mocker.patch("linkedin_cli.api.client.PlaywrightLinkedinAPI", return_value=mock_api)

    # Mock page navigation & inner_text
    mock_page = mocker.MagicMock()
    mock_page.title.return_value = "Bob Jones | LinkedIn"
    
    mock_main = mocker.MagicMock()
    mock_main.inner_text.return_value = "Bob Jones\nHead of Engineering at CloudScale | SaaS\nLondon, United Kingdom\nContact info"
    
    mock_locator = mocker.MagicMock()
    mock_locator.count.return_value = 1
    mock_locator.first = mock_main
    mock_page.locator.return_value = mock_locator
    
    mock_session.page = mock_page

    res = enrich_linkedin_lead(test_lead, mock_session)
    assert res["status"] == "ENRICHED"
    assert res["method"] == "BROWSER_DOM"

    test_lead.refresh_from_db()
    assert test_lead.first_name == "Bob"
    assert test_lead.last_name == "Jones"
    assert "Head of Engineering" in test_lead.current_title
    assert test_lead.company == "CloudScale"
    assert test_lead.location == "London, United Kingdom"
    assert test_lead.country_code == ""


@pytest.mark.django_db
def test_enrich_linkedin_lead_cache_short_circuit(mocker, test_lead):
    """Verify already-enriched lead short-circuits gracefully."""
    test_lead.first_name = "Charlie"
    test_lead.last_name = "Brown"
    test_lead.current_title = "CEO"
    test_lead.company = "Startup"
    test_lead.save()

    mock_session = mocker.MagicMock()
    res = enrich_linkedin_lead(test_lead, mock_session)
    assert res["status"] == "ENRICHED"
    assert res["method"] == "CACHE"
