# tests/test_linkedin_vnc_verification.py
"""LinkedIn VNC / Native Mac Verification stack health, lease, and status tests."""
import pytest
from unittest.mock import patch, MagicMock
from django.contrib.auth.models import User
from openoutreach.linkedin.models import LinkedInProfile
from openoutreach.linkedin.vnc_service import (
    check_vnc_listener,
    get_credential_fingerprint,
    get_platform_backend,
    get_interactive_verification_status,
    start_interactive_verification,
    stop_interactive_verification,
)
from openoutreach.linkedin.browser_lease import (
    acquire_browser_lease,
    release_browser_lease,
    get_browser_lease_status,
)


@pytest.mark.django_db
def test_checkpoint_status_rendering_and_credential_preservation():
    user = User.objects.create_user(username="vnc_user", email="vnc@example.com", is_staff=True)
    lp = LinkedInProfile.objects.create(
        user=user,
        linkedin_username="vnc_user@example.com",
        cookie_data={"cookies": [{"name": "li_at", "value": "test_cookie_value"}]},
        session_status="checkpoint"
    )

    fingerprint = get_credential_fingerprint(lp.cookie_data)
    assert fingerprint is not None
    assert len(fingerprint) == 12

    # Credential fingerprint remains identical when checkpoint status is active
    lp.refresh_from_db()
    assert lp.session_status == "checkpoint"
    assert get_credential_fingerprint(lp.cookie_data) == fingerprint


def test_fingerprint_ignores_metadata_field():
    """Verify get_credential_fingerprint hashes actual cookie bytes and ignores 'fingerprint' metadata."""
    fake_dict = {
        "fingerprint": "4d885e1dc314",
        "cookies": [{"name": "li_at", "value": "4d885e1dc314_session"}]
    }
    # Synthetic/fabricated values return invalid_fabricated
    assert get_credential_fingerprint(fake_dict) == "invalid_fabricated"

    # Missing credentials return none
    assert get_credential_fingerprint(None) == "none"
    assert get_credential_fingerprint({}) == "none"


def test_tmp_browser_state_contains_no_credential_secrets():
    """Verify that durable /tmp/huntflow_native_browser.json contains no cookie or credential secrets."""
    from openoutreach.linkedin.vnc_service import _write_durable_session_state, _read_durable_session_state, _get_durable_session_file, _clear_durable_session_state
    import os, json

    _write_durable_session_state({"pid": os.getpid(), "profile_id": 1, "launched_at": 1000.0, "status": "Opened"}, profile_id=1)
    filepath = _get_durable_session_file(1)
    assert os.path.exists(filepath)
    with open(filepath, "r") as f:
        content = f.read()

    assert "li_at" not in content
    assert "cookie" not in content
    assert "password" not in content
    assert "token" not in content

    durable = _read_durable_session_state(1)
    assert durable.get("pid") == os.getpid()
    assert durable.get("status") == "Opened"
    _clear_durable_session_state(1)


def test_platform_backend_resolution():
    backend = get_platform_backend()
    assert backend in ["NATIVE_MAC", "NOVNC_LINUX"]


@pytest.mark.django_db
def test_browser_execution_lease_locking():
    # Release any existing lease
    release_browser_lease(owner="force")
    assert get_browser_lease_status()["is_locked"] is False

    # Acquire lease
    acquired = acquire_browser_lease(owner="interactive_verification", ttl_seconds=600)
    assert acquired is True
    assert get_browser_lease_status()["is_locked"] is True
    assert get_browser_lease_status()["owner"] == "interactive_verification"

    # Conflicting lease attempt should fail
    second_attempt = acquire_browser_lease(owner="daemon_worker", ttl_seconds=600)
    assert second_attempt is False

    # Release lease
    released = release_browser_lease(owner="interactive_verification")
    assert released is True
    assert get_browser_lease_status()["is_locked"] is False


@pytest.mark.django_db
def test_interactive_verification_status_structure():
    user = User.objects.create_user(username="status_user", email="status@example.com", is_staff=True)
    lp = LinkedInProfile.objects.create(
        user=user,
        linkedin_username="status@example.com",
        cookie_data={"cookies": [{"name": "li_at", "value": "status_cookie_value"}]},
        session_status="checkpoint"
    )

    status = get_interactive_verification_status(lp)
    assert "backend" in status
    assert "status" in status
    assert "fingerprint" in status
    assert "lease" in status
    assert status["fingerprint"] == get_credential_fingerprint(lp.cookie_data)


@pytest.mark.django_db
@patch("openoutreach.linkedin.session_manager.run_session_health_check")
def test_linkedin_verify_completion_success(mock_health, client):
    user = User.objects.create_user(username="verify_user", email="verify@example.com", password="password", is_staff=True)
    lp = LinkedInProfile.objects.create(
        user=user,
        linkedin_username="verify@example.com",
        cookie_data={"cookies": [{"name": "li_at", "value": "valid_cookie"}]},
        session_status="checkpoint"
    )

    def side_effect(profile):
        profile.session_status = "valid"
        profile.save()

    mock_health.side_effect = side_effect

    client.login(username="verify_user", password="password")
    resp = client.post("/dashboard/linkedin/verify-completion/")

    assert resp.status_code == 302
    assert resp.url == "/dashboard/integrations/"
    lp.refresh_from_db()
    assert lp.session_status == "valid"


@pytest.mark.django_db
def test_multi_account_credential_uniqueness_and_isolation():
    """Verify that a credential belongs to exactly one LinkedInProfile and profiles do not share cookies."""
    u1 = User.objects.create_user(username="user1", email="u1@example.com")
    u2 = User.objects.create_user(username="user2", email="u2@example.com")

    lp1 = LinkedInProfile.objects.create(
        user=u1,
        linkedin_username="candidate-10@example.com",
        cookie_data=None,
        session_status="needs_reauth"
    )
    lp2 = LinkedInProfile.objects.create(
        user=u2,
        linkedin_username="ahmad-farooq-undefined-793750427",
        cookie_data={"cookies": [{"name": "li_at", "value": "unique_ahmad_farooq_cookie"}]},
        session_status="valid"
    )

    fp2 = get_credential_fingerprint(lp2.cookie_data)
    assert fp2 != "none"

    owners = [p for p in LinkedInProfile.objects.all() if get_credential_fingerprint(p.cookie_data) == fp2]
    assert len(owners) == 1
    assert owners[0].id == lp2.id
    assert lp1.cookie_data is None
    assert get_credential_fingerprint(lp1.cookie_data) == "none"
