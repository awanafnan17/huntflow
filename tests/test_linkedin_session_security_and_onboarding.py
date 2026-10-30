import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from django.contrib.contenttypes.models import ContentType

from openoutreach.core.models import Workspace, WorkspaceMembership
from openoutreach.linkedin.models import LinkedInProfile
from openoutreach.career.models import IncidentHold, IncidentHoldTarget


@pytest.mark.django_db
def test_linkedin_cookie_secret_never_leaked_in_html_or_responses():
    """Verify that a submitted LinkedIn session cookie is strictly masked and never echoed in HTML, JS, or redirects."""
    user = User.objects.create_user(username="secure_operator", email="secure@huntflow.ai", password="pw", is_staff=True)
    ws = Workspace.objects.create(name="Secure Workspace", slug="secure-ws")
    WorkspaceMembership.objects.create(user=user, workspace=ws, role="admin")

    client = Client()
    client.force_login(user)

    sentinel_cookie = "sentinel_secret_cookie_token_9876543210_xyz"

    # 1. GET settings/linkedin/ - Ensure page loads and does not contain sentinel
    resp = client.get(reverse("dashboard:settings_linkedin"))
    assert resp.status_code == 200
    assert sentinel_cookie not in resp.content.decode("utf-8")
    assert 'type="password"' in resp.content.decode("utf-8")

    # 2. POST sentinel cookie to verify endpoint
    # Mock validate_linkedin_session to simulate valid feed response
    from unittest.mock import patch
    with patch("openoutreach.linkedin.session_manager.validate_linkedin_session", return_value=("valid", None)):
        post_resp = client.post(
            reverse("dashboard:settings_linkedin_verify"),
            {"li_at_cookie": sentinel_cookie},
            follow=True
        )
        assert post_resp.status_code == 200
        html_content = post_resp.content.decode("utf-8")

        # Sentinel cookie value must NEVER appear anywhere in the rendered HTML output or attributes
        assert sentinel_cookie not in html_content
        # Password field must NOT have the value populated
        assert f'value="{sentinel_cookie}"' not in html_content
        assert sentinel_cookie not in post_resp.request["PATH_INFO"]
        assert sentinel_cookie not in post_resp.request.get("QUERY_STRING", "")


@pytest.mark.django_db
def test_linkedin_onboarding_checkpoint_hold_release():
    """Verify that validating a legitimate session releases any active checkpoint incident hold."""
    user = User.objects.create_user(username="checkpoint_user", email="cp@huntflow.ai", password="pw", is_staff=True)
    ws = Workspace.objects.create(name="CP Workspace", slug="cp-ws")
    WorkspaceMembership.objects.create(user=user, workspace=ws, role="admin")

    profile = LinkedInProfile.objects.create(
        user=user,
        linkedin_username="cp@huntflow.ai",
        session_status="checkpoint",
        active=True
    )

    # Attach checkpoint hold
    profile_ct = ContentType.objects.get_for_model(LinkedInProfile)
    hold = IncidentHold.objects.create(
        reason="LINKEDIN_ACCOUNT_CHECKPOINT",
        description="Checkpoint hit during live worker run",
        active=True
    )
    IncidentHoldTarget.objects.create(
        incident_hold=hold,
        content_type=profile_ct,
        object_id=profile.pk
    )

    assert IncidentHold.objects.filter(active=True, targets__object_id=profile.pk).exists()

    client = Client()
    client.force_login(user)

    from unittest.mock import patch
    with patch("openoutreach.linkedin.session_manager.validate_linkedin_session", return_value=("valid", None)):
        resp = client.post(
            reverse("dashboard:settings_linkedin_verify"),
            {"li_at_cookie": "valid_session_cookie_mock_123"},
            follow=True
        )
        assert resp.status_code == 200

        profile.refresh_from_db()
        assert profile.session_status == "valid"
        assert profile.identity_validation_status == "VALID_IDENTITY_MATCH"
        assert profile.last_authenticated_at is not None

        # Checkpoint hold must now be resolved/inactive
        assert not IncidentHold.objects.filter(active=True, targets__object_id=profile.pk).exists()


@pytest.mark.django_db
def test_linkedin_disconnect_action():
    """Verify that disconnect action safely clears cookies and marks session expired."""
    user = User.objects.create_user(username="disc_user", email="disc@huntflow.ai", password="pw", is_staff=True)
    profile = LinkedInProfile.objects.create(
        user=user,
        linkedin_username="disc@huntflow.ai",
        session_status="valid",
        cookie_data={"cookies": [{"name": "li_at", "value": "some_cookie"}]},
        active=True
    )

    client = Client()
    client.force_login(user)

    resp = client.post(reverse("dashboard:settings_linkedin_disconnect"), follow=True)
    assert resp.status_code == 200

    profile.refresh_from_db()
    assert profile.session_status == "expired"
    assert profile.cookie_data is None


@pytest.mark.django_db
def test_saas_settings_endpoints_render_cleanly():
    """Verify all modern SaaS settings pages return HTTP 200."""
    user = User.objects.create_user(username="saas_tester", email="test@huntflow.ai", password="pw", is_staff=True, is_superuser=True)
    client = Client()
    client.force_login(user)

    endpoints = [
        reverse("dashboard:home"),
        reverse("dashboard:campaigns"),
        reverse("career:job_list"),
        reverse("dashboard:operator_inbox"),
        reverse("dashboard:action_logs"),
        reverse("dashboard:settings_account"),
        reverse("dashboard:settings_linkedin"),
        reverse("dashboard:settings_email"),
        reverse("dashboard:settings_job_profile"),
        reverse("dashboard:settings_writing_style"),
        reverse("dashboard:settings_automation"),
        reverse("dashboard:settings_advanced"),
    ]

    for url in endpoints:
        resp = client.get(url, follow=True)
        assert resp.status_code == 200, f"Endpoint {url} returned {resp.status_code}"
