# tests/test_multi_tenant_safety.py
"""Multi-tenant isolation and user scoping safety regression tests."""
import pytest
from django.contrib.auth.models import User
from openoutreach.career.models import (
    CandidateProfile,
    CandidateApplicationProfile,
    JobApplicationAnswer,
)
from openoutreach.linkedin.models import LinkedInProfile
from openoutreach.core.models import Campaign, Workspace
from openoutreach.dashboard.views import _resolve_operator_context


@pytest.mark.django_db
def test_user_a_cannot_access_user_b_candidate_profile():
    user_a = User.objects.create_user(username="user_a", email="usera@example.com")
    user_b = User.objects.create_user(username="user_b", email="userb@example.com")

    cp_a = CandidateProfile.objects.create(user=user_a, full_name="User A Candidate")
    cp_b = CandidateProfile.objects.create(user=user_b, full_name="User B Candidate")

    # Scoped lookups
    assert CandidateProfile.objects.filter(user=user_a).first() == cp_a
    assert CandidateProfile.objects.filter(user=user_b).first() == cp_b
    assert CandidateProfile.objects.filter(user=user_a).first() != cp_b


@pytest.mark.django_db
def test_user_a_cannot_access_user_b_linkedin_profile():
    user_a = User.objects.create_user(username="user_a_lp", email="usera_lp@example.com")
    user_b = User.objects.create_user(username="user_b_lp", email="userb_lp@example.com")

    lp_a = LinkedInProfile.objects.create(user=user_a, linkedin_username="usera@linkedin.com")
    lp_b = LinkedInProfile.objects.create(user=user_b, linkedin_username="userb@linkedin.com")

    assert LinkedInProfile.objects.filter(user=user_a).first() == lp_a
    assert LinkedInProfile.objects.filter(user=user_b).first() == lp_b


@pytest.mark.django_db
def test_user_answer_bank_isolation():
    user_a = User.objects.create_user(username="user_a_ans", email="usera_ans@example.com")
    user_b = User.objects.create_user(username="user_b_ans", email="userb_ans@example.com")

    cp_a = CandidateProfile.objects.create(user=user_a, full_name="Candidate A")
    cp_b = CandidateProfile.objects.create(user=user_b, full_name="Candidate B")

    ans_a = JobApplicationAnswer.objects.create(
        candidate_profile=cp_a,
        canonical_key="phone",
        answer_type="text",
        answer_value="+15550100",
        confirmed=True,
        source="operator_chat"
    )

    ans_b_query = JobApplicationAnswer.objects.filter(candidate_profile=cp_b)
    assert ans_b_query.count() == 0


@pytest.mark.django_db
def test_admin_delegated_operator_context_resolution(rf):
    admin = User.objects.create_superuser(username="admin_test", email="admin@localhost", password="pw")
    operator = User.objects.create_user(username="operator_test", email="operator@example.com")

    cp_op = CandidateProfile.objects.create(user=operator, full_name="Operator Jango")
    lp_op = LinkedInProfile.objects.create(user=operator, linkedin_username="operator@linkedin.com", cookie_data={"cookie": "value"})

    req = rf.get("/dashboard/")
    req.user = admin

    ctx = _resolve_operator_context(req)
    assert ctx["signed_in_user"] == admin
    assert ctx["operator_user"] == admin
    assert ctx["candidate_profile"] == cp_op
    assert ctx["linkedin_profile"] == lp_op
    assert ctx["admin_operating"] is True


@pytest.mark.django_db
def test_cross_tenant_workspace_mismatch_blocks_provider_read():
    """SaaS Safety: Tenant A cannot access Tenant B's LinkedIn profile; fails closed."""
    from openoutreach.linkedin.services.account_governor import is_account_safe_for_provider_read
    user_a = User.objects.create_user(username="tenant_a_user", email="a@example.com")
    user_b = User.objects.create_user(username="tenant_b_user", email="b@example.com")

    ws_a = Workspace.objects.create(name="Tenant A", slug="tenant-a")
    ws_b = Workspace.objects.create(name="Tenant B", slug="tenant-b")

    lp_b = LinkedInProfile.objects.create(user=user_b, workspace=ws_b, linkedin_username="userb_lp")

    # Accessing Tenant B's profile from Tenant A workspace context fails closed
    is_safe, reason = is_account_safe_for_provider_read(workspace=ws_a, linkedin_profile=lp_b)
    assert is_safe is False
    assert reason == "workspace_mismatch_cross_tenant_access_blocked"

    # Accessing with correct workspace succeeds
    is_safe_b, reason_b = is_account_safe_for_provider_read(workspace=ws_b, linkedin_profile=lp_b)
    assert is_safe_b is True
    assert reason_b == "ok"


@pytest.mark.django_db
def test_burst_read_limit_cooling_down_blocks_read():
    """SaaS Safety: Rapid burst of live reads triggers cooling down protection."""
    from openoutreach.linkedin.services.account_governor import (
        is_account_safe_for_provider_read,
        BURST_READ_LIMIT_5M,
    )
    from openoutreach.linkedin.models import ProviderReadLedger, ProviderOperationType
    user = User.objects.create_user(username="burst_user", email="burst@example.com")
    ws = Workspace.objects.create(name="Burst WS", slug="burst-ws")
    lp = LinkedInProfile.objects.create(user=user, workspace=ws, linkedin_username="burst_lp")

    # Record BURST_READ_LIMIT_5M reads
    for i in range(BURST_READ_LIMIT_5M):
        ProviderReadLedger.objects.create(
            workspace=ws,
            linkedin_profile=lp,
            operation_type=ProviderOperationType.JOB_SEARCH,
            resource_identifier=f"query_{i}",
            cache_hit=False,
        )

    is_safe, reason = is_account_safe_for_provider_read(workspace=ws, linkedin_profile=lp)
    assert is_safe is False
    assert reason == "burst_read_limit_exceeded_cooling_down"


@pytest.mark.django_db
def test_account_provider_lock_context_manager():
    """SaaS Safety: account_provider_lock context manager serializes account access."""
    from openoutreach.linkedin.services.account_governor import account_provider_lock
    user = User.objects.create_user(username="lock_user", email="lock@example.com")
    ws = Workspace.objects.create(name="Lock WS", slug="lock-ws")
    lp = LinkedInProfile.objects.create(user=user, workspace=ws, linkedin_username="lock_lp")

    with account_provider_lock(linkedin_profile=lp) as locked_obj:
        assert locked_obj is not None
        assert locked_obj.pk == lp.pk


@pytest.mark.django_db
def test_missing_workspace_fails_closed():
    """SaaS Invariant: Missing workspace fails closed immediately."""
    from openoutreach.linkedin.services.account_governor import (
        is_account_safe_for_provider_read,
        is_account_safe_for_provider_write,
    )
    user = User.objects.create_user(username="no_ws_user", email="nows@example.com")
    lp = LinkedInProfile.objects.create(user=user, workspace=None, linkedin_username="nows_lp")

    is_safe_read, reason_read = is_account_safe_for_provider_read(workspace=None, linkedin_profile=lp)
    assert is_safe_read is False
    assert reason_read == "missing_workspace_fail_closed"

    is_safe_write, reason_write = is_account_safe_for_provider_write(workspace=None, linkedin_profile=lp)
    assert is_safe_write is False
    assert reason_write == "missing_workspace_fail_closed"


@pytest.mark.django_db
def test_zero_workspace_db_fails_closed():
    """SaaS Invariant: Empty DB with zero workspaces fails closed on provider read/write."""
    from openoutreach.linkedin.services.account_governor import (
        is_account_safe_for_provider_read,
        is_account_safe_for_provider_write,
    )
    Workspace.objects.all().delete()

    is_safe_read, reason_read = is_account_safe_for_provider_read(workspace=None, linkedin_profile=None)
    assert is_safe_read is False
    assert reason_read == "missing_workspace_fail_closed"

    is_safe_write, reason_write = is_account_safe_for_provider_write(workspace=None, linkedin_profile=None)
    assert is_safe_write is False
    assert reason_write == "missing_workspace_fail_closed"


@pytest.mark.django_db
def test_campaign_workspace_mismatch_fails_closed():
    """SaaS Invariant: Campaign belonging to Workspace A executed in Workspace B context fails closed."""
    from openoutreach.linkedin.services.account_governor import is_account_safe_for_provider_write
    ws_a = Workspace.objects.create(name="WS A", slug="ws-a")
    ws_b = Workspace.objects.create(name="WS B", slug="ws-b")
    camp_a = Campaign.objects.create(name="Camp A", workspace=ws_a)

    user_b = User.objects.create_user(username="user_b_mismatch", email="userb_mismatch@example.com")
    lp_b = LinkedInProfile.objects.create(user=user_b, workspace=ws_b, linkedin_username="lp_b")

    is_safe, reason = is_account_safe_for_provider_write(
        workspace=ws_b,
        linkedin_profile=lp_b,
        campaign=camp_a,
    )
    assert is_safe is False
    assert reason == "campaign_workspace_mismatch"


@pytest.mark.django_db
def test_write_governor_burst_limiting():
    """SaaS Safety: Rapid burst of writes triggers 10m cooling down limiter."""
    from openoutreach.linkedin.services.account_governor import (
        is_account_safe_for_provider_write,
        BURST_WRITE_LIMIT_10M,
    )
    from openoutreach.linkedin.models import ActionLog
    user = User.objects.create_user(username="write_burst_user", email="wb@example.com")
    ws = Workspace.objects.create(name="WB WS", slug="wb-ws")
    camp = Campaign.objects.create(name="WB Camp", workspace=ws)
    lp = LinkedInProfile.objects.create(
        user=user,
        workspace=ws,
        linkedin_username="wb_lp",
        session_status="valid",
        identity_validation_status="VALID_IDENTITY_MATCH",
        connect_daily_limit=50,
    )

    for i in range(BURST_WRITE_LIMIT_10M):
        ActionLog.objects.create(
            linkedin_profile=lp,
            campaign=camp,
            action_type=ActionLog.ActionType.CONNECT,
        )

    is_safe, reason = is_account_safe_for_provider_write(
        workspace=ws,
        linkedin_profile=lp,
        campaign=camp,
    )
    assert is_safe is False
    assert reason == "burst_write_limit_exceeded_cooling_down"


@pytest.mark.django_db
def test_provider_safety_event_append_only_history():
    """SaaS Safety: Warnings generate append-only ProviderSafetyEvent records."""
    from unittest.mock import MagicMock
    from openoutreach.linkedin.services.account_governor import check_and_record_safety_warning
    from openoutreach.linkedin.models import ProviderSafetyEvent
    from openoutreach.core.models import WorkspaceAutomationSettings

    user = User.objects.create_user(username="event_user", email="ev@example.com")
    ws = Workspace.objects.create(name="Event WS", slug="event-ws")
    lp = LinkedInProfile.objects.create(user=user, workspace=ws, linkedin_username="ev_lp")
    WorkspaceAutomationSettings.get_for_workspace(ws)

    mock_page = MagicMock()
    mock_page.url = "https://www.linkedin.com/checkpoint/challenge/"
    mock_page.evaluate.return_value = "Security checkpoint: verify your identity"

    tripped = check_and_record_safety_warning(mock_page, workspace=ws, linkedin_profile=lp)
    assert tripped is True

    lp.refresh_from_db()
    assert lp.session_status == "checkpoint"

    events = ProviderSafetyEvent.objects.filter(workspace=ws, linkedin_profile=lp)
    assert events.count() == 1
    ev = events.first()
    assert ev.event_type == "linkedin_security_checkpoint"
    assert ev.action_taken in ("SAFETY_STOP", "PROFILE_CHECKPOINT")


@pytest.mark.django_db
def test_restart_preserves_safety_state_and_history():
    """SaaS Safety: Database-persisted safety stop and events survive process restart."""
    from openoutreach.core.models import WorkspaceAutomationSettings
    from openoutreach.linkedin.models import ProviderSafetyEvent
    ws = Workspace.objects.create(name="Restart WS", slug="restart-ws")
    auto_st = WorkspaceAutomationSettings.get_for_workspace(ws)
    auto_st.trigger_safety_stop("high_volume_data_access_warning")

    # Simulate fresh process startup / new query
    fresh_st = WorkspaceAutomationSettings.objects.get(workspace=ws)
    assert fresh_st.soak_status == "SAFETY_STOP"
    assert fresh_st.provider_safety_state == "HIGH_VOLUME_DATA_ACCESS_WARNING"


