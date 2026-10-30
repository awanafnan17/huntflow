# tests/test_session_read_accounting.py
"""
Tests for Session-Start Provider Read Ledger Accounting.

Verifies:
1. Session launch with saved cookies records a ProviderReadLedger entry (SESSION_HEALTH, success=True).
2. Session launch with navigation timeout records a ProviderReadLedger entry (SESSION_HEALTH, success=False).
3. Fresh login session launch records a ProviderReadLedger entry (SESSION_HEALTH, success=True/False).
4. No unledgered browser session launch navigations.
"""
from unittest.mock import MagicMock, patch
import pytest
from django.contrib.auth.models import User

from openoutreach.core.models import Workspace
from openoutreach.linkedin.models import LinkedInProfile, ProviderReadLedger, ProviderOperationType
from openoutreach.linkedin.browser.session import AccountSession
from openoutreach.linkedin.browser.launch import start_browser_session


@pytest.fixture
def session_accounting_env(db):
    user = User.objects.create_user(username="sess_acct_user", email="sess_acct@example.com")
    workspace = Workspace.objects.create(name="Session Acct WS", slug="sess-acct-ws")
    linkedin_profile = LinkedInProfile.objects.create(
        user=user,
        workspace=workspace,
        linkedin_username="sess_acct_member",
        active=True,
        cookie_data={"cookies": [{"name": "li_at", "value": "test_cookie", "domain": ".linkedin.com"}]},
    )
    return {
        "user": user,
        "workspace": workspace,
        "linkedin_profile": linkedin_profile,
    }


@pytest.mark.django_db
def test_saved_session_launch_records_success_read_ledger(session_accounting_env):
    """Successful browser session start records exactly 1 ProviderReadLedger entry."""
    env = session_accounting_env
    profile = env["linkedin_profile"]

    mock_page = MagicMock()
    mock_page.url = "https://www.linkedin.com/feed/"
    mock_context = MagicMock()
    mock_browser = MagicMock()
    mock_playwright = MagicMock()

    session = AccountSession(profile)

    with patch("openoutreach.linkedin.browser.launch.launch_browser", return_value=(mock_page, mock_context, mock_browser, mock_playwright)), \
         patch("openoutreach.linkedin.browser.launch.dismiss_comply_gate"), \
         patch("openoutreach.linkedin.browser.launch.goto_page"):

        reads_before = ProviderReadLedger.objects.filter(linkedin_profile=profile).count()
        start_browser_session(session)
        reads_after = ProviderReadLedger.objects.filter(linkedin_profile=profile).count()

        assert reads_after == reads_before + 1

        entry = ProviderReadLedger.objects.filter(linkedin_profile=profile).latest("created_at")
        assert entry.operation_type == ProviderOperationType.SESSION_HEALTH
        assert entry.resource_identifier == "session_init:/feed"
        assert entry.success is True
        assert entry.read_cost == 1
        assert entry.workspace_id == env["workspace"].id
        assert entry.linkedin_profile_id == profile.id


@pytest.mark.django_db
def test_saved_session_launch_records_timeout_failure_read_ledger(session_accounting_env):
    """Session start navigation timeout records a failed ProviderReadLedger entry."""
    env = session_accounting_env
    profile = env["linkedin_profile"]

    mock_page = MagicMock()
    mock_page.url = "https://www.linkedin.com/feed/"
    mock_page.goto.side_effect = TimeoutError("Timeout 25000ms exceeded.")
    mock_context = MagicMock()
    mock_browser = MagicMock()
    mock_playwright = MagicMock()

    session = AccountSession(profile)

    with patch("openoutreach.linkedin.browser.launch.launch_browser", return_value=(mock_page, mock_context, mock_browser, mock_playwright)), \
         patch("openoutreach.linkedin.browser.launch.dismiss_comply_gate"), \
         patch("openoutreach.linkedin.browser.launch.goto_page", side_effect=TimeoutError("Timeout 25000ms exceeded.")):

        reads_before = ProviderReadLedger.objects.filter(linkedin_profile=profile).count()

        with pytest.raises(TimeoutError) as exc_info:
            start_browser_session(session)

        assert "Timeout 25000ms exceeded" in str(exc_info.value)

        reads_after = ProviderReadLedger.objects.filter(linkedin_profile=profile).count()
        assert reads_after == reads_before + 1

        entry = ProviderReadLedger.objects.filter(linkedin_profile=profile).latest("created_at")
        assert entry.operation_type == ProviderOperationType.SESSION_HEALTH
        assert entry.resource_identifier == "session_init:/feed"
        assert entry.success is False
        assert entry.read_cost == 1
        assert entry.workspace_id == env["workspace"].id
        assert entry.linkedin_profile_id == profile.id
        assert "Timeout 25000ms exceeded" in entry.metadata.get("error", "")
        assert entry.metadata.get("stage") == "session_init_feed"


@pytest.mark.django_db
def test_fresh_login_launch_records_read_ledger(session_accounting_env):
    """Fresh login (no saved cookies) records SESSION_HEALTH ledger entry."""
    env = session_accounting_env
    profile = env["linkedin_profile"]
    profile.cookie_data = None
    profile.save(update_fields=["cookie_data"])

    mock_page = MagicMock()
    mock_page.url = "https://www.linkedin.com/feed/"
    mock_context = MagicMock()
    mock_context.storage_state.return_value = {"cookies": [{"name": "li_at", "value": "fresh_val"}]}
    mock_browser = MagicMock()
    mock_playwright = MagicMock()

    session = AccountSession(profile)

    with patch("openoutreach.linkedin.browser.launch.launch_browser", return_value=(mock_page, mock_context, mock_browser, mock_playwright)), \
         patch("openoutreach.linkedin.browser.launch.authenticate") as mock_auth, \
         patch("openoutreach.linkedin.browser.launch._save_cookies"):

        reads_before = ProviderReadLedger.objects.filter(linkedin_profile=profile).count()
        start_browser_session(session)
        reads_after = ProviderReadLedger.objects.filter(linkedin_profile=profile).count()

        assert reads_after == reads_before + 1
        assert mock_auth.called

        entry = ProviderReadLedger.objects.filter(linkedin_profile=profile).latest("created_at")
        assert entry.operation_type == ProviderOperationType.SESSION_HEALTH
        assert entry.resource_identifier == "session_login"
        assert entry.success is True
        assert entry.read_cost == 1
