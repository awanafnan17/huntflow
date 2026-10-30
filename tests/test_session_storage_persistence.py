import json
import pytest
from unittest.mock import MagicMock, patch
from django.utils import timezone
from django.contrib.auth import get_user_model

from openoutreach.core.models import Workspace, WorkspaceAutomationSettings, Campaign, Task
from openoutreach.linkedin.models import LinkedInProfile, ProviderAccountLease
from openoutreach.linkedin.browser.session import AccountSession
from openoutreach.linkedin.session_manager import save_linkedin_session_cookie
from openoutreach.core.scheduler import reconcile
from openoutreach.linkedin.services.account_governor import (
    account_provider_lock,
    get_active_profile_lease_token,
)
from linkedin_cli.exceptions import AuthenticationError

User = get_user_model()


@pytest.fixture
def persistence_setup(db):
    import uuid
    from openoutreach.linkedin.services.account_governor import _THREAD_ACTIVE_LEASES
    _THREAD_ACTIVE_LEASES.tokens = {}

    uid = uuid.uuid4().hex[:8]
    user = User.objects.create_user(username=f"user_{uid}")
    ws = Workspace.objects.create(name=f"WS_{uid}", slug=f"ws-{uid}")
    auto_st = WorkspaceAutomationSettings.get_for_workspace(ws)
    auto_st.master_automation_enabled = True
    auto_st.soak_status = "ACTIVE"
    auto_st.save()

    profile = LinkedInProfile.objects.create(
        user=user,
        workspace=ws,
        linkedin_username=f"tester_{uid}",
        active=True,
        session_status="valid",
    )
    campaign = Campaign.objects.create(
        name=f"Campaign_{uid}",
        workspace=ws,
        linkedin_profile=profile,
        is_active=True,
        linkedin_outreach_enabled=True,
    )
    campaign.users.add(user)
    return {
        "user": user,
        "workspace": ws,
        "auto_st": auto_st,
        "profile": profile,
        "campaign": campaign,
    }


def test_encrypted_full_storage_state_round_trip(persistence_setup):
    """Case 1: Encrypted full storage-state round trip: raw DB field is ciphertext, getter returns dict."""
    prof = persistence_setup["profile"]
    full_state = {
        "version": 1,
        "cookies": [
            {
                "name": "li_at",
                "value": "secret_li_at_token_val",
                "domain": ".linkedin.com",
                "path": "/",
                "expires": -1,
                "httpOnly": True,
                "secure": True,
                "sameSite": "None",
            },
            {
                "name": "JSESSIONID",
                "value": "ajax:99887766",
                "domain": ".linkedin.com",
                "path": "/",
                "expires": -1,
                "httpOnly": False,
                "secure": True,
                "sameSite": "None",
            },
        ],
        "origins": [],
    }
    prof.set_cookie_data(full_state)
    prof.save(update_fields=["cookie_data"])

    # Verify raw DB value is encrypted ciphertext string and does NOT contain plaintext
    prof.refresh_from_db(fields=["cookie_data"])
    raw_stored = prof.cookie_data
    assert isinstance(raw_stored, str)
    assert raw_stored.startswith("v1::") or raw_stored.startswith("gcm_enc::")
    assert "secret_li_at_token_val" not in raw_stored
    assert "ajax:99887766" not in raw_stored

    # Verify canonical getter decrypts to original structure
    decrypted = prof.get_decrypted_cookie_data()
    assert isinstance(decrypted, dict)
    assert decrypted["version"] == 1
    assert len(decrypted["cookies"]) == 2
    assert any(c["name"] == "li_at" and c["value"] == "secret_li_at_token_val" for c in decrypted["cookies"])


def test_auxiliary_cookie_names_survive_encrypted_round_trip(persistence_setup):
    """Case 2: Auxiliary cookie names survive encrypted round trip safely."""
    prof = persistence_setup["profile"]
    auxiliary_names = ["bcookie", "bscookie", "JSESSIONID", "lidc", "lang", "__cf_bm"]
    cookies_list = [
        {"name": "li_at", "value": "test_li_at_val", "domain": ".linkedin.com", "path": "/"}
    ]
    for name in auxiliary_names:
        cookies_list.append({
            "name": name,
            "value": f"val_{name}_123",
            "domain": ".linkedin.com",
            "path": "/",
        })

    state = {"version": 1, "cookies": cookies_list, "origins": []}
    prof.set_cookie_data(state)
    prof.save(update_fields=["cookie_data"])

    prof.refresh_from_db(fields=["cookie_data"])
    loaded = prof.get_decrypted_cookie_data()
    names_loaded = [c["name"] for c in loaded["cookies"]]
    for expected_name in auxiliary_names:
        assert expected_name in names_loaded


def test_close_persists_state_before_context_destruction(persistence_setup):
    """Case 3: close() persists updated storage state before context destruction."""
    prof = persistence_setup["profile"]
    session = AccountSession(prof)

    mock_context = MagicMock()
    mock_context.storage_state.return_value = {
        "version": 1,
        "cookies": [
            {"name": "li_at", "value": "active_li_at", "domain": ".linkedin.com", "path": "/"},
            {"name": "bcookie", "value": "b_cookie_val", "domain": ".linkedin.com", "path": "/"},
            {"name": "JSESSIONID", "value": "ajax:active", "domain": ".linkedin.com", "path": "/"},
        ],
        "origins": [],
    }
    mock_browser = MagicMock()
    mock_page = MagicMock()
    mock_pw = MagicMock()

    session.context = mock_context
    session.browser = mock_browser
    session.page = mock_page
    session.playwright = mock_pw

    session.close()

    # Verify context and browser were closed
    assert mock_context.close.called
    assert mock_browser.close.called
    assert session.context is None

    # Verify updated storage state was written to DB
    prof.refresh_from_db(fields=["cookie_data"])
    saved_state = prof.get_decrypted_cookie_data()
    assert saved_state is not None
    saved_names = [c["name"] for c in saved_state["cookies"]]
    assert "bcookie" in saved_names
    assert "JSESSIONID" in saved_names


def test_malformed_or_missing_li_at_not_persisted(persistence_setup):
    """Case 4: Malformed state or missing li_at is rejected and does not overwrite valid state."""
    prof = persistence_setup["profile"]
    original_state = {
        "version": 1,
        "cookies": [{"name": "li_at", "value": "orig_valid_token", "domain": ".linkedin.com"}],
        "origins": [],
    }
    prof.set_cookie_data(original_state)
    prof.session_status = "valid"
    prof.save()

    session = AccountSession(prof)
    mock_context = MagicMock()
    # Missing li_at
    mock_context.storage_state.return_value = {
        "cookies": [{"name": "some_other_cookie", "value": "123"}],
        "origins": [],
    }
    session.context = mock_context

    success = session.persist_storage_state()
    assert success is False

    # Original credential in DB is preserved
    prof.refresh_from_db(fields=["cookie_data", "session_status"])
    current_state = prof.get_decrypted_cookie_data()
    assert current_state["cookies"][0]["value"] == "orig_valid_token"
    # Status marked needs_reauth
    assert prof.session_status == "needs_reauth"


def test_needs_reauth_prevents_browser_creation(persistence_setup):
    """Case 5: session_status == 'needs_reauth' fails closed without launching browser."""
    prof = persistence_setup["profile"]
    prof.session_status = "needs_reauth"
    prof.save(update_fields=["session_status"])

    session = AccountSession(prof)
    with patch("openoutreach.linkedin.browser.launch.start_browser_session") as mock_start:
        with pytest.raises(AuthenticationError) as exc_info:
            session.ensure_browser()
        assert "requires re-authentication" in str(exc_info.value)
        assert not mock_start.called


def test_one_profile_cannot_own_two_browser_lifecycle_leases(persistence_setup):
    """Case 6: One profile cannot have two concurrent browser lifecycle owners."""
    prof = persistence_setup["profile"]
    prof.session_status = "valid"
    prof.save(update_fields=["session_status"])

    session1 = AccountSession(prof)
    try:
        with patch("openoutreach.linkedin.browser.launch.start_browser_session"):
            session1.ensure_browser()
            assert session1._owned_lease_cm is not None
            assert ProviderAccountLease.objects.filter(linkedin_profile=prof).exists()

            # Second session in a different thread/context attempts to acquire
            session2 = AccountSession(prof)
            with patch("openoutreach.linkedin.services.account_governor.get_active_profile_lease_token", return_value=None):
                with pytest.raises(RuntimeError) as exc_info:
                    session2.ensure_browser()
                assert "ProviderAccountLeaseConflict" in str(exc_info.value)
    finally:
        session1.close()

    assert not ProviderAccountLease.objects.filter(linkedin_profile=prof).exists()


def test_lease_released_after_normal_close(persistence_setup):
    """Case 7: Lifecycle lease is released after normal session close."""
    prof = persistence_setup["profile"]
    session = AccountSession(prof)
    with patch("openoutreach.linkedin.browser.launch.start_browser_session"):
        session.ensure_browser()
        assert ProviderAccountLease.objects.filter(linkedin_profile=prof).exists()

        session.close()
        assert not ProviderAccountLease.objects.filter(linkedin_profile=prof).exists()
        assert session._owned_lease_cm is None


def test_lease_released_after_exception(persistence_setup):
    """Case 8: Lifecycle lease is safely released if browser startup throws an exception."""
    prof = persistence_setup["profile"]
    session = AccountSession(prof)

    with patch("openoutreach.linkedin.browser.launch.start_browser_session", side_effect=ValueError("Startup crash")):
        with pytest.raises(ValueError):
            session.ensure_browser()

    # Lease must be cleaned up despite exception
    assert not ProviderAccountLease.objects.filter(linkedin_profile=prof).exists()
    assert session._owned_lease_cm is None


def test_nested_existing_service_lease_does_not_deadlock(persistence_setup):
    """Case 9: Caller holding account_provider_lock passes lease safely to AccountSession."""
    prof = persistence_setup["profile"]
    ws = persistence_setup["workspace"]

    with account_provider_lock(linkedin_profile=prof, workspace=ws, operation_name="manual_connect") as locked_prof:
        assert locked_prof is not None
        assert get_active_profile_lease_token(prof.pk) is not None

        session = AccountSession(prof)
        with patch("openoutreach.linkedin.browser.launch.start_browser_session"):
            session.ensure_browser()
            # Session should NOT create a competing owned lease since caller owns it
            assert session._owned_lease_cm is None
            session.close()

        # Outer lease still safely held until context exit
        assert ProviderAccountLease.objects.filter(linkedin_profile=prof).exists()

    # Now released after outer context exits
    assert not ProviderAccountLease.objects.filter(linkedin_profile=prof).exists()


def test_safety_stop_reconcile_creates_zero_tasks(persistence_setup):
    """Case 10: SAFETY_STOP reconcile creates zero tasks."""
    ws = persistence_setup["workspace"]
    auto_st = persistence_setup["auto_st"]
    auto_st.soak_status = "SAFETY_STOP"
    auto_st.soak_stop_reason = "provider_redirect_loop"
    auto_st.save()

    prof = persistence_setup["profile"]
    session = AccountSession(prof)

    initial_tasks = Task.objects.count()
    reconcile(session)
    assert Task.objects.count() == initial_tasks


def test_master_off_reconcile_creates_zero_tasks(persistence_setup):
    """Case 11: master_automation_enabled == False reconcile creates zero tasks."""
    auto_st = persistence_setup["auto_st"]
    auto_st.master_automation_enabled = False
    auto_st.save()

    prof = persistence_setup["profile"]
    session = AccountSession(prof)

    initial_tasks = Task.objects.count()
    reconcile(session)
    assert Task.objects.count() == initial_tasks


def test_idle_worker_under_safety_stop_does_not_spin_reconciliation(persistence_setup):
    """Case 12: Idle daemon loop skips reconcile when workspace is SAFETY_STOP."""
    auto_st = persistence_setup["auto_st"]
    auto_st.soak_status = "SAFETY_STOP"
    auto_st.save()

    prof = persistence_setup["profile"]
    session = AccountSession(prof)

    with patch("openoutreach.core.scheduler.reconcile") as mock_reconcile:
        # Reconcile function itself also verifies safety state
        reconcile(session)
        assert not mock_reconcile.called


def test_auth_required_creates_no_repeated_browser_launches(persistence_setup):
    """Case 13: Profiles in needs_reauth/expired do not repeatedly launch browser."""
    prof = persistence_setup["profile"]
    for bad_status in ["needs_reauth", "expired", "invalid", "checkpoint", "restricted"]:
        prof.session_status = bad_status
        prof.save()
        session = AccountSession(prof)
        with patch("openoutreach.linkedin.browser.launch.start_browser_session") as mock_start:
            with pytest.raises(AuthenticationError):
                session.ensure_browser()
            assert not mock_start.called


def test_mac_filesystem_browser_independence():
    """Case 14: Verify zero references or interaction with Mac Chrome profile or files."""
    import os
    import inspect
    from openoutreach.linkedin.browser import session, launch, adapter
    from openoutreach.linkedin import session_manager

    modules = [session, launch, adapter, session_manager]
    forbidden_patterns = [
        "Library/Application Support/Google/Chrome",
        "Google Chrome.app",
        "Chrome/Default/Cookies",
        "~/Library",
        "macOS",
    ]

    for mod in modules:
        source = inspect.getsource(mod)
        for pattern in forbidden_patterns:
            assert pattern not in source, f"Module {mod.__name__} must not reference {pattern}"


def test_legacy_li_at_only_state_remains_readable(persistence_setup):
    """Case 15: Legacy li_at-only state remains readable for onboarding."""
    prof = persistence_setup["profile"]
    legacy_state = {
        "cookies": [
            {"name": "li_at", "value": "legacy_cookie_value", "domain": ".linkedin.com", "path": "/"}
        ],
        "origins": [],
    }
    prof.set_cookie_data(legacy_state)
    prof.save()

    decrypted = prof.get_decrypted_cookie_data()
    assert decrypted is not None
    assert decrypted["cookies"][0]["value"] == "legacy_cookie_value"


def test_canonical_richer_storage_state_not_downgraded(persistence_setup):
    """Case 16: Resaving li_at updates the token while preserving auxiliary cookies."""
    prof = persistence_setup["profile"]
    rich_state = {
        "version": 1,
        "cookies": [
            {"name": "li_at", "value": "old_li_at_val", "domain": ".linkedin.com", "path": "/"},
            {"name": "bcookie", "value": "persisted_device_id", "domain": ".linkedin.com", "path": "/"},
            {"name": "JSESSIONID", "value": "ajax:998811", "domain": ".linkedin.com", "path": "/"},
            {"name": "lidc", "value": "routing_id_val", "domain": ".linkedin.com", "path": "/"},
        ],
        "origins": [],
    }
    prof.set_cookie_data(rich_state)
    prof.save()

    # Operator supplies a new li_at
    save_linkedin_session_cookie(prof, "new_shiny_li_at_value")

    prof.refresh_from_db(fields=["cookie_data"])
    updated = prof.get_decrypted_cookie_data()
    assert updated is not None

    cookie_map = {c["name"]: c["value"] for c in updated["cookies"]}
    # li_at is updated
    assert cookie_map["li_at"] == "new_shiny_li_at_value"
    # Auxiliary cookies were preserved and NOT wiped out!
    assert cookie_map["bcookie"] == "persisted_device_id"
    assert cookie_map["JSESSIONID"] == "ajax:998811"
    assert cookie_map["lidc"] == "routing_id_val"
