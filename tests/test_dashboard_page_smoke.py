# tests/test_dashboard_page_smoke.py
"""Comprehensive page smoke test suite verifying HTTP 200 responses under admin delegated context."""
import pytest
from django.contrib.auth.models import User
from openoutreach.career.models import CandidateProfile, CandidateApplicationProfile
from openoutreach.linkedin.models import LinkedInProfile
from openoutreach.core.models import Campaign
from openoutreach.linkedin.browser_lease import acquire_browser_lease, release_browser_lease, get_browser_lease_status


@pytest.fixture
def setup_operator_data(db):
    admin = User.objects.create_superuser(username="admin", email="admin@example.com", password="password")
    cand_user = User.objects.create_user(username="cand_user", email="candidate-10@example.com")
    cand = CandidateProfile.objects.create(id=1, user=cand_user, full_name="Casey Example", email="candidate-10@example.com")
    app_prof = CandidateApplicationProfile.objects.create(candidate_profile=cand)
    lp = LinkedInProfile.objects.create(
        id=1,
        user=cand_user,
        linkedin_username="candidate-10@example.com",
        cookie_data={"fingerprint": "4d885e1dc314", "cookies": [{"name": "li_at", "value": "test_li_at"}]},
        session_status="checkpoint"
    )
    c3 = Campaign.objects.create(id=3, name="Job Outreach", is_active=True)
    c52 = Campaign.objects.create(id=52, name="Dubai Founder Networking", is_active=True)
    return {
        "admin": admin,
        "cand_user": cand_user,
        "candidate": cand,
        "app_profile": app_prof,
        "linkedin_profile": lp,
        "campaign_3": c3,
        "campaign_52": c52,
    }


@pytest.mark.django_db
def test_dashboard_real_admin_delegated_get_200(client, setup_operator_data):
    client.login(username="admin", password="password")
    resp = client.get("/dashboard/", follow=True)
    assert resp.status_code == 200
    assert "HuntFlow" in resp.content.decode("utf-8")


@pytest.mark.django_db
def test_integrations_real_admin_delegated_get_200(client, setup_operator_data):
    client.login(username="admin", password="password")
    resp = client.get("/dashboard/integrations/", follow=True)
    assert resp.status_code == 200
    content = resp.content.decode("utf-8")
    assert "LinkedIn Account" in content


@pytest.mark.django_db
def test_job_profile_real_admin_delegated_get_200(client, setup_operator_data):
    client.login(username="admin", password="password")
    resp = client.get("/dashboard/settings/job-profile/", follow=True)
    assert resp.status_code == 200


@pytest.mark.django_db
def test_live_work_real_admin_delegated_get_200(client, setup_operator_data):
    client.login(username="admin", password="password")
    resp = client.get("/dashboard/live-work/", follow=True)
    assert resp.status_code == 200


@pytest.mark.django_db
def test_campaigns_real_admin_delegated_get_200(client, setup_operator_data):
    client.login(username="admin", password="password")
    resp = client.get("/dashboard/campaigns/", follow=True)
    assert resp.status_code == 200
    assert "Campaigns" in resp.content.decode("utf-8")


@pytest.mark.django_db
def test_browser_lease_heartbeat(setup_operator_data):
    release_browser_lease(owner="force")
    acquired = acquire_browser_lease(owner="interactive_verification", ttl_seconds=1800)
    assert acquired is True

    status = get_browser_lease_status()
    assert status["is_locked"] is True
    assert status["owner"] == "interactive_verification"

    release_browser_lease(owner="interactive_verification")
    assert get_browser_lease_status()["is_locked"] is False
