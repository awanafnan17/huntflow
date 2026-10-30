# tests/test_campaign_preflight.py
import pytest
from openoutreach.core.models import Campaign, Workspace
from openoutreach.crm.models.deal import Deal, DealState
from openoutreach.crm.models.lead import Lead
from openoutreach.linkedin.models import LinkedInProfile
from openoutreach.core.campaign_preflight import check_campaign_resume_readiness


@pytest.fixture
def preflight_campaign(db):
    ws = Workspace.objects.create(name="Preflight WS", slug="preflight-ws")
    from django.contrib.auth.models import User
    user = User.objects.create(username="preflight_user")
    p3 = LinkedInProfile.objects.create(
        user=user,
        linkedin_username="p3@example.com",
        session_status="valid"
    )
    c = Campaign.objects.create(
        name="Preflight Test Campaign",
        workspace=ws,
        linkedin_profile=p3,
        linkedin_outreach_enabled=True,
        target_job_titles="Founder, CEO",
        target_locations="United Kingdom",
        is_active=False
    )
    return c


@pytest.mark.django_db
def test_campaign_preflight_discovery_ready_but_not_outreach_ready(preflight_campaign):
    """Verify campaign is DISCOVERY READY when paused with zero ready deals, but NOT OUTREACH READY."""
    res = check_campaign_resume_readiness(preflight_campaign)
    assert res["discovery_ready"] is True
    assert res["outreach_ready"] is False
    assert "Campaign is PAUSED (is_active=False)" in res["reasons"]


@pytest.mark.django_db
def test_campaign_preflight_outreach_ready(preflight_campaign):
    """Verify campaign becomes OUTREACH READY when activated and ready deals exist."""
    preflight_campaign.is_active = True
    preflight_campaign.save()

    lead = Lead.objects.create(
        public_identifier="ready-lead",
        workspace=preflight_campaign.workspace
    )
    Deal.objects.create(
        lead=lead,
        campaign=preflight_campaign,
        state=DealState.READY_TO_CONNECT
    )

    res = check_campaign_resume_readiness(preflight_campaign)
    assert res["discovery_ready"] is True
    assert res["outreach_ready"] is True
    assert res["ready_deals_count"] == 1
