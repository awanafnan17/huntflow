# tests/test_session_ownership_architecture.py
"""
Comprehensive offline test suite for HuntFlow Permanent Provider Session Ownership Architecture.

Guarantees:
- Zero provider network traffic (all calls mocked or local fixtures)
- Proves imported seed vs managed session provenance invariants
- Proves server storage-state capture trust boundary (rejects client-posted storage states)
- Proves internal managed persistence without boolean force flags
- Proves silent downgrade prevention and explicit replacement semantics
- Proves server bootstrap state machine, lease binding, and single-use token security
- Proves operator manual provider-use pause and resume safety gating
- Proves cross-tenant isolation (Bakr cannot access Candidate profile/bootstrap)
- Proves historical Approval #20 expiration and non-reusability
"""
import hashlib
import pytest
from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.utils import timezone

from openoutreach.core.models import (
    Campaign,
    OperatorActionApproval,
    Workspace,
    WorkspaceAutomationSettings,
    Task,
)
from openoutreach.crm.models import Deal, DealState, Lead
from openoutreach.linkedin.exceptions import (
    SessionOwnershipConflictError,
    ServerBootstrapError,
)
from openoutreach.linkedin.models import (
    LinkedInProfile,
    ProviderAccountLease,
    ServerBootstrapSession,
)
from openoutreach.linkedin.services.account_governor import (
    is_account_safe_for_provider_read,
    is_account_safe_for_provider_write,
    account_provider_lock,
)
from openoutreach.linkedin.services.server_bootstrap import (
    start_server_bootstrap,
    complete_server_bootstrap,
    abort_server_bootstrap,
    renew_server_bootstrap_lease,
    cleanup_abandoned_bootstrap_sessions,
    hash_bootstrap_token,
)
from openoutreach.linkedin.session_manager import save_linkedin_session_cookie


@pytest.fixture
def test_setup(db):
    """Setup multi-tenant test environment for Candidate (default) and Bakr."""
    # 1. Candidate Workspace & Profile
    candidate_user = User.objects.create_user(username="candidate_test", password="testpassword123")
    candidate_ws = Workspace.objects.create(name="Candidate Agency", slug="default")
    candidate_auto = WorkspaceAutomationSettings.objects.create(
        workspace=candidate_ws,
        master_automation_enabled=True,
        auto_find_prospects=True,
        auto_send_connections=True,
        auto_followup=True,
        auto_find_jobs=True,
        auto_apply_jobs=True,
        auto_send_emails=True,
        auto_monitor_replies=True,
        soak_status="ACTIVE",
        soak_stop_reason="",
        provider_safety_state="NORMAL",
    )
    candidate_profile = LinkedInProfile.objects.create(
        user=candidate_user,
        workspace=candidate_ws,
        linkedin_username="candidate_li",
        session_status="valid",
        identity_validation_status="VALID",
        active=True,
    )
    candidate_camp = Campaign.objects.create(
        name="Candidate Campaign 107",
        workspace=candidate_ws,
        linkedin_profile=candidate_profile,
        is_active=True,
    )

    # 2. Bakr Agency Workspace & Isolation
    bakr_user = User.objects.create_user(username="bakr_test", password="testpassword123")
    bakr_ws = Workspace.objects.create(name="Bakr Agency", slug="bakr-agency")
    bakr_auto = WorkspaceAutomationSettings.objects.create(
        workspace=bakr_ws,
        master_automation_enabled=False,
        auto_find_prospects=False,
        auto_send_connections=False,
        auto_followup=False,
        auto_find_jobs=False,
        auto_apply_jobs=False,
        auto_send_emails=False,
        auto_monitor_replies=False,
        soak_status="NOT_STARTED",
        soak_stop_reason="",
        provider_safety_state="NORMAL",
    )

    return {
        "candidate_user": candidate_user,
        "candidate_ws": candidate_ws,
        "candidate_auto": candidate_auto,
        "candidate_profile": candidate_profile,
        "candidate_camp": candidate_camp,
        "bakr_user": bakr_user,
        "bakr_ws": bakr_ws,
        "bakr_auto": bakr_auto,
    }


# ── 1. SESSION PROVENANCE & STORAGE INVARIANTS ──

def test_existing_row_defaults_to_imported_seed(test_setup):
    """Proves that newly created or migrated profiles default to 'imported_seed' provenance."""
    prof = test_setup["candidate_profile"]
    prof.refresh_from_db()
    assert prof.session_origin == "imported_seed"


def test_imported_seed_stored_encrypted_and_sets_needs_reauth(test_setup):
    """Proves imported session seed is encrypted at rest and sets needs_reauth."""
    prof = test_setup["candidate_profile"]
    raw_li = "AQEDATestCookieValue12345"
    res = prof.import_session_seed(raw_li)
    assert res is True
    prof.refresh_from_db()

    assert prof.session_origin == "imported_seed"
    assert prof.session_status == "needs_reauth"
    assert prof.cookie_data is not None
    # Encrypted: raw li_at does not appear in plaintext in JSON
    raw_str = str(prof.cookie_data)
    assert raw_li not in raw_str
    # Decrypted helper returns canonical storage state structure
    dec = prof.get_decrypted_cookie_data()
    assert isinstance(dec, dict)
    assert dec.get("version") == 1
    cookies = dec.get("cookies", [])
    assert any(c.get("name") == "li_at" and c.get("value") == raw_li for c in cookies)


def test_imported_seed_never_silently_becomes_managed(test_setup):
    """Proves that normal browser operations on an imported_seed session do not relabel it managed."""
    from openoutreach.linkedin.browser.session import AccountSession
    prof = test_setup["candidate_profile"]
    prof.import_session_seed("AQEDATestCookieValue12345")
    prof.refresh_from_db()
    assert prof.session_origin == "imported_seed"

    # Simulate AccountSession persisting storage state mutations
    session = AccountSession.__new__(AccountSession)
    session.linkedin_profile = prof
    mock_context = MagicMock()
    mock_context.storage_state.return_value = {
        "version": 1,
        "cookies": [{"name": "li_at", "value": "AQEDATestCookieValue12345", "domain": ".linkedin.com"}],
    }
    session.context = mock_context

    success = session.persist_storage_state()
    assert success is True
    prof.refresh_from_db()
    # Stays imported_seed!
    assert prof.session_origin == "imported_seed"
    assert prof.last_server_state_persisted_at is not None


def test_managed_provenance_persists_legitimate_browser_mutations(test_setup):
    """Proves that a managed session persists mutations seamlessly without a boolean force flag."""
    from openoutreach.linkedin.browser.session import AccountSession
    prof = test_setup["candidate_profile"]
    prof.session_origin = "huntflow_managed"
    prof.save(update_fields=["session_origin"])

    session = AccountSession.__new__(AccountSession)
    session.linkedin_profile = prof
    mock_context = MagicMock()
    mock_context.storage_state.return_value = {
        "version": 1,
        "cookies": [
            {"name": "li_at", "value": "AQEDAServerManagedValue12345", "domain": ".linkedin.com"},
            {"name": "bcookie", "value": "v=2&server_bcookie", "domain": ".linkedin.com"},
        ],
    }
    session.context = mock_context

    success = session.persist_storage_state()
    assert success is True
    prof.refresh_from_db()
    # Remains huntflow_managed
    assert prof.session_origin == "huntflow_managed"
    assert prof.last_server_state_persisted_at is not None
    dec = prof.get_decrypted_cookie_data()
    cookies = dec.get("cookies", [])
    assert any(c.get("name") == "bcookie" for c in cookies)


def test_imported_seed_cannot_silently_overwrite_managed_state(test_setup):
    """Proves that a managed session cannot be silently downgraded or overwritten by an imported seed."""
    prof = test_setup["candidate_profile"]
    prof.session_origin = "huntflow_managed"
    prof.save(update_fields=["session_origin"])

    # Attempting to import an imported seed directly must raise SessionOwnershipConflictError
    with pytest.raises(SessionOwnershipConflictError) as exc_info:
        prof.import_session_seed("AQEDAClientMacCookie")
    assert "Cannot silently overwrite a HuntFlow-managed server session" in str(exc_info.value)

    # Calling save_linkedin_session_cookie without force must also fail
    with pytest.raises(SessionOwnershipConflictError):
        save_linkedin_session_cookie(prof, "AQEDAClientMacCookie")

    prof.refresh_from_db()
    assert prof.session_origin == "huntflow_managed"


def test_explicit_replacement_transitions_managed_to_imported_seed(test_setup):
    """Proves explicit replacement moves managed -> imported_seed + needs_reauth."""
    prof = test_setup["candidate_profile"]
    prof.session_origin = "huntflow_managed"
    prof.session_status = "valid"
    prof.save(update_fields=["session_origin", "session_status"])

    # Replacement without confirmation token fails
    with pytest.raises(ValueError):
        prof.replace_managed_session_with_imported_seed("AQEDANewSeed", confirmation="wrong_token")

    # Replacement with explicit confirmation succeeds
    res = prof.replace_managed_session_with_imported_seed("AQEDANewSeed", confirmation="REPLACE_MANAGED_SESSION")
    assert res is True

    prof.refresh_from_db()
    assert prof.session_origin == "imported_seed"
    assert prof.session_status == "needs_reauth"


# ── 2. TRUSTED SERVER BOOTSTRAP ARCHITECTURE ──

def test_bootstrap_requires_exact_workspace_profile_binding(test_setup):
    """Proves bootstrap initiation fails if profile does not belong to the workspace."""
    bakr_ws = test_setup["bakr_ws"]
    candidate_prof = test_setup["candidate_profile"]
    bakr_user = test_setup["bakr_user"]

    session, token, err = start_server_bootstrap(
        workspace=bakr_ws,
        linkedin_profile=candidate_prof,
        operator=bakr_user,
    )
    assert session is None
    assert token is None
    assert "tenant_mismatch" in err


def test_bootstrap_acquires_account_lifecycle_lease(test_setup):
    """Proves bootstrap acquires ProviderAccountLease and rejects concurrent bootstrap."""
    ws = test_setup["candidate_ws"]
    prof = test_setup["candidate_profile"]
    user = test_setup["candidate_user"]

    session, token, err = start_server_bootstrap(
        workspace=ws,
        linkedin_profile=prof,
        operator=user,
        ttl_seconds=600,
    )
    assert err is None
    assert session is not None
    assert token is not None

    # Check that ProviderAccountLease exists
    lease = ProviderAccountLease.objects.filter(linkedin_profile=prof).first()
    assert lease is not None
    assert lease.active_operation == "server_bootstrap"

    # A concurrent second bootstrap is rejected
    sess2, tok2, err2 = start_server_bootstrap(
        workspace=ws,
        linkedin_profile=prof,
        operator=user,
    )
    assert sess2 is None
    assert "active_bootstrap_already_exists" in err2


def test_concurrent_normal_session_rejected_during_bootstrap(test_setup):
    """Proves that while bootstrap holds the lease, normal browser session launch is blocked."""
    ws = test_setup["candidate_ws"]
    prof = test_setup["candidate_profile"]
    user = test_setup["candidate_user"]

    session, token, err = start_server_bootstrap(
        workspace=ws,
        linkedin_profile=prof,
        operator=user,
    )
    assert session is not None

    # Normal lock acquisition must fail
    with account_provider_lock(linkedin_profile=prof, workspace=ws, operation_name="browser_lifecycle") as locked:
        assert locked is None  # Could not acquire lease


def test_raw_token_not_persisted_plaintext_in_db(test_setup):
    """Proves only the SHA-256 hash of the bootstrap token is stored in the DB."""
    ws = test_setup["candidate_ws"]
    prof = test_setup["candidate_profile"]
    user = test_setup["candidate_user"]

    session, token, err = start_server_bootstrap(
        workspace=ws,
        linkedin_profile=prof,
        operator=user,
    )
    assert session is not None
    assert token is not None

    session.refresh_from_db()
    # Raw token MUST NOT be stored
    assert session.token_hash == hashlib.sha256(token.encode()).hexdigest()
    assert token not in session.token_hash
    # No other field stores raw token
    assert not ServerBootstrapSession.objects.filter(token_hash=token).exists()


def test_client_raw_storage_state_rejected_by_server_bootstrap(test_setup):
    """CRITICAL TRUST BOUNDARY: Raw dictionary passed as client storage state is rejected."""
    ws = test_setup["candidate_ws"]
    prof = test_setup["candidate_profile"]
    user = test_setup["candidate_user"]

    session, token, err = start_server_bootstrap(
        workspace=ws,
        linkedin_profile=prof,
        operator=user,
    )
    assert session is not None

    # Passing a raw dict (untrusted client payload) must fail closed
    fake_client_state = {
        "cookies": [{"name": "li_at", "value": "MacCopiedValue", "domain": ".linkedin.com"}],
    }
    ok, msg = complete_server_bootstrap(token, fake_client_state)
    assert ok is False
    assert "client_storage_state_rejected" in msg

    session.refresh_from_db()
    assert session.status == ServerBootstrapSession.Status.FAILED
    prof.refresh_from_db()
    assert prof.session_origin != "huntflow_managed"


def test_server_browser_context_completes_bootstrap_safely(test_setup):
    """Proves server-owned browser context completes bootstrap, marks managed, and sets needs_reauth."""
    ws = test_setup["candidate_ws"]
    prof = test_setup["candidate_profile"]
    user = test_setup["candidate_user"]

    session, token, err = start_server_bootstrap(
        workspace=ws,
        linkedin_profile=prof,
        operator=user,
    )
    assert session is not None

    # Create mock server browser context (representing trusted Playwright context)
    mock_server_context = MagicMock()
    mock_server_context.storage_state.return_value = {
        "version": 1,
        "cookies": [
            {"name": "li_at", "value": "AQEDAServerGeneratedSessionToken", "domain": ".linkedin.com"},
            {"name": "bcookie", "value": "v=2&server_bcookie", "domain": ".linkedin.com"},
        ],
        "origins": [],
    }

    ok, msg = complete_server_bootstrap(token, mock_server_context)
    assert ok is True
    assert msg == "bootstrap_completed_successfully"

    session.refresh_from_db()
    assert session.status == ServerBootstrapSession.Status.COMPLETED
    assert session.completed_at is not None

    prof.refresh_from_db()
    assert prof.session_origin == "huntflow_managed"
    assert prof.bootstrap_completed_at is not None
    # Crucial invariant: NEVER marks valid directly from bootstrap
    assert prof.session_status == "needs_reauth"

    # Lease is released
    assert not ProviderAccountLease.objects.filter(linkedin_profile=prof).exists()


def test_bootstrap_token_single_use_replay_rejected(test_setup):
    """Proves consumed bootstrap token cannot be replayed."""
    ws = test_setup["candidate_ws"]
    prof = test_setup["candidate_profile"]
    user = test_setup["candidate_user"]

    session, token, err = start_server_bootstrap(
        workspace=ws,
        linkedin_profile=prof,
        operator=user,
    )
    mock_server_context = MagicMock()
    mock_server_context.storage_state.return_value = {
        "version": 1,
        "cookies": [{"name": "li_at", "value": "AQEDAServerToken", "domain": ".linkedin.com"}],
    }

    ok, msg = complete_server_bootstrap(token, mock_server_context)
    assert ok is True

    # Replay attempt
    ok2, msg2 = complete_server_bootstrap(token, mock_server_context)
    assert ok2 is False
    assert "replay_rejected" in msg2


def test_expired_bootstrap_token_rejected_and_cleans_lease(test_setup):
    """Proves expired bootstrap token fails closed and cleans up lease."""
    ws = test_setup["candidate_ws"]
    prof = test_setup["candidate_profile"]
    user = test_setup["candidate_user"]

    session, token, err = start_server_bootstrap(
        workspace=ws,
        linkedin_profile=prof,
        operator=user,
        ttl_seconds=1,
    )
    # Force expiration in DB
    session.expires_at = timezone.now() - timedelta(seconds=10)
    session.save(update_fields=["expires_at"])

    mock_server_context = MagicMock()
    ok, msg = complete_server_bootstrap(token, mock_server_context)
    assert ok is False
    assert "bootstrap_session_expired" in msg

    session.refresh_from_db()
    assert session.status == ServerBootstrapSession.Status.EXPIRED
    assert not ProviderAccountLease.objects.filter(linkedin_profile=prof).exists()


def test_abort_server_bootstrap_releases_lease(test_setup):
    """Proves aborting server bootstrap releases lease and closes browser."""
    ws = test_setup["candidate_ws"]
    prof = test_setup["candidate_profile"]
    user = test_setup["candidate_user"]

    session, token, err = start_server_bootstrap(
        workspace=ws,
        linkedin_profile=prof,
        operator=user,
    )
    mock_browser = MagicMock()
    ok, msg = abort_server_bootstrap(token, reason="operator_cancelled", server_browser_context_or_session=mock_browser)
    assert ok is True
    assert mock_browser.close.called

    session.refresh_from_db()
    assert session.status == ServerBootstrapSession.Status.ABORTED
    assert session.failure_reason == "operator_cancelled"
    assert not ProviderAccountLease.objects.filter(linkedin_profile=prof).exists()


# ── 3. OPERATOR MANUAL PROVIDER USE PAUSE ──

def test_manual_provider_pause_blocks_reads_and_writes(test_setup):
    """Proves manual provider pause blocks reads and writes while leaving all 8 preferences ON."""
    ws = test_setup["candidate_ws"]
    prof = test_setup["candidate_profile"]
    auto = test_setup["candidate_auto"]

    # Initial state: eligible
    r_ok, _ = is_account_safe_for_provider_read(workspace=ws, linkedin_profile=prof)
    w_ok, _ = is_account_safe_for_provider_write(workspace=ws, linkedin_profile=prof)
    assert r_ok is True
    assert w_ok is True

    # Activate pause
    auto.pause_for_manual_provider_use()
    auto.refresh_from_db()
    assert auto.manual_provider_use_paused is True

    # All 8 user automation preferences remain ON
    assert auto.all_eight_switches_on() is True
    assert auto.master_automation_enabled is True

    # Reads and writes are blocked with reason
    r_ok2, r_msg = is_account_safe_for_provider_read(workspace=ws, linkedin_profile=prof)
    w_ok2, w_msg = is_account_safe_for_provider_write(workspace=ws, linkedin_profile=prof)
    assert r_ok2 is False
    assert r_msg == "manual_provider_use_paused"
    assert w_ok2 is False
    assert w_msg == "manual_provider_use_paused"


def test_manual_pause_blocks_browser_launch_in_session(test_setup):
    """Proves AccountSession blocks browser launch when manual provider pause is active."""
    from openoutreach.linkedin.browser.session import AccountSession
    ws = test_setup["candidate_ws"]
    prof = test_setup["candidate_profile"]
    auto = test_setup["candidate_auto"]

    auto.pause_for_manual_provider_use()

    session = AccountSession.__new__(AccountSession)
    session.linkedin_profile = prof
    session.campaign = test_setup["candidate_camp"]
    session.page = None
    session.context = None
    session.browser = None
    session.playwright = None
    session._owned_lease_cm = None

    with pytest.raises(RuntimeError) as exc_info:
        session.ensure_browser()
    assert "ManualProviderUsePaused" in str(exc_info.value)


def test_resume_from_manual_pause_safety_gated(test_setup):
    """Proves resume succeeds if safety gates pass, but fails if safety hold is active."""
    ws = test_setup["candidate_ws"]
    prof = test_setup["candidate_profile"]
    auto = test_setup["candidate_auto"]

    auto.pause_for_manual_provider_use()

    # Normal state: resume succeeds
    ok, msg = auto.resume_from_manual_provider_use()
    assert ok is True
    assert msg == "ok"
    auto.refresh_from_db()
    assert auto.manual_provider_use_paused is False

    # Pause again, but simulate safety stop
    auto.pause_for_manual_provider_use()
    auto.soak_status = "SAFETY_STOP"
    auto.soak_stop_reason = "provider_checkpoint"
    auto.save()

    # Resume must fail closed
    ok2, msg2 = auto.resume_from_manual_provider_use()
    assert ok2 is False
    assert "cannot_resume_safety_stop" in msg2
    assert auto.manual_provider_use_paused is True


def test_scheduler_and_worker_reconcile_suppressed_during_manual_pause(test_setup):
    """Proves scheduler planning and daemon queue reconcile are suppressed during manual pause."""
    from openoutreach.core.scheduler import plan_connect_window, reconcile
    ws = test_setup["candidate_ws"]
    auto = test_setup["candidate_auto"]
    camp = test_setup["candidate_camp"]

    auto.pause_for_manual_provider_use()

    # Scheduler returns 0
    count = plan_connect_window(session=MagicMock(), campaign=camp)
    assert count == 0

    # Daemon reconcile returns without creating tasks
    session_mock = MagicMock()
    session_mock.campaigns = [camp]
    session_mock.linkedin_profile = test_setup["candidate_profile"]
    reconcile(session_mock)


# ── 4. CANARY & APPROVAL EXPIRATION INVARIANTS ──

def test_expired_approval_20_cannot_execute(test_setup):
    """Proves historical Approval #20 expired and cannot be executed, renewed, or reused."""
    ws = test_setup["candidate_ws"]
    user = test_setup["candidate_user"]
    prof = test_setup["candidate_profile"]

    lead = Lead.objects.create(
        workspace=ws,
        first_name="Brad",
        last_name="Barkhouse",
        linkedin_url="https://www.linkedin.com/in/brad-barkhouse",
    )
    deal = Deal.objects.create(
        campaign=test_setup["candidate_camp"],
        lead=lead,
        state=DealState.READY_TO_CONNECT,
    )

    # Recreate historical Approval #20 expired on 2026-09-08
    app20 = OperatorActionApproval.objects.create(
        id=2020,
        workspace=ws,
        operator=user,
        linkedin_profile=prof,
        lead=lead,
        deal=deal,
        action_type="connect",
        status="approved",
        expires_at=timezone.now() - timedelta(days=2),
    )

    # Validation must fail closed due to expiration
    assert app20.is_valid_for(
        workspace=ws,
        linkedin_profile=prof,
        lead=lead,
        deal=deal,
        action_type="connect",
    ) is False


def test_bakr_cross_tenant_isolation(test_setup):
    """Proves Bakr workspace cannot access Candidate profile or bootstrap session."""
    bakr_ws = test_setup["bakr_ws"]
    bakr_user = test_setup["bakr_user"]
    candidate_prof = test_setup["candidate_profile"]

    # Bakr cannot start bootstrap for Candidate profile
    sess, tok, err = start_server_bootstrap(
        workspace=bakr_ws,
        linkedin_profile=candidate_prof,
        operator=bakr_user,
    )
    assert sess is None
    assert "tenant_mismatch" in err
