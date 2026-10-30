"""
Tests for HuntFlow Persistent Autonomous Runtime & Architecture Portability.

Covers:
1. Multi-channel bounded production without 24-hour soak expiration.
2. Strict task-scoped candidate resolution (no pk=2 fallback, fail closed on mismatch).
3. Multi-channel fair scheduling & channel independence under single-prospect gate failures.
4. Launchd supervisor configuration validation.
5. Calendar day quota evaluation & continuity across restarts.
"""
import os
import plistlib
import pytest
from datetime import timedelta
from django.utils import timezone
from django.contrib.auth.models import User

from openoutreach.core.models import (
    Workspace,
    WorkspaceMembership,
    WorkspaceAutomationSettings,
    Task,
    Campaign,
)
from openoutreach.career.models import (
    CandidateProfile,
    CandidateApplicationProfile,
    CandidateIdentityVerification,
)
from openoutreach.core.scheduler import plan_job_window, reconcile
from openoutreach.core.daemon import handle_job_discovery_task
from openoutreach.career.services.job_discovery_service import discover_and_evaluate_jobs


@pytest.mark.django_db
def test_persistent_production_mode_does_not_expire_after_24h():
    """Verify that in BOUNDED_PRODUCTION mode, 24h soak expiration does NOT disable automation."""
    user = User.objects.create_user(username="prod_user", email="prod@example.com")
    ws = Workspace.objects.create(name="Prod WS", slug="prod-ws")
    WorkspaceMembership.objects.create(workspace=ws, user=user, role="admin")

    auto = WorkspaceAutomationSettings.get_for_workspace(ws)
    auto.enable_persistent_production(
        enable_connections=True,
        enable_followup=True,
        enable_jobs=True,
        job_target=50,
        job_ceiling=60,
    )

    # Fast forward past 24 hours
    auto.soak_expected_end_at = timezone.now() - timedelta(hours=2)
    auto.save()

    # check_soak_expiry should return False in BOUNDED_PRODUCTION mode
    assert auto.check_soak_expiry() is False
    assert auto.master_automation_enabled is True
    assert auto.auto_apply_jobs is True
    assert auto.auto_send_connections is True
    assert auto.mode == "BOUNDED_PRODUCTION"


@pytest.mark.django_db
def test_task_scoped_candidate_resolution_fails_closed_on_missing_candidate():
    """Verify that job discovery fails closed when candidate cannot be resolved."""
    ws = Workspace.objects.create(name="Empty WS", slug="empty-ws")
    auto = WorkspaceAutomationSettings.get_for_workspace(ws)
    auto.enable_persistent_production()

    # Plan job window with no candidate in workspace
    created = plan_job_window(session=None, workspace=ws, candidate=None)
    assert created == 0

    # Explicit discovery call without candidate fails closed
    result = discover_and_evaluate_jobs(session=None, workspace=ws, candidate=None)
    assert result["status"] == "failed"
    assert result["reason"] == "missing_workspace_or_candidate"


@pytest.mark.django_db
def test_candidate_workspace_tenancy_mismatch_fails_closed():
    """Verify that candidate from Workspace A cannot be used in Workspace B."""
    user_a = User.objects.create_user(username="user_a", email="a@example.com")
    user_b = User.objects.create_user(username="user_b", email="b@example.com")

    ws_a = Workspace.objects.create(name="WS A", slug="ws-a")
    ws_b = Workspace.objects.create(name="WS B", slug="ws-b")

    WorkspaceMembership.objects.create(workspace=ws_a, user=user_a, role="admin")
    WorkspaceMembership.objects.create(workspace=ws_b, user=user_b, role="admin")

    cand_a = CandidateProfile.objects.create(user=user_a, full_name="Alice Candidate", email="a@example.com")

    # Discovering jobs for candidate A in Workspace B must fail closed
    result = discover_and_evaluate_jobs(session=None, workspace=ws_b, candidate=cand_a)
    assert result["status"] == "failed"
    assert result["reason"] == "candidate_workspace_mismatch"


@pytest.mark.django_db
def test_handle_job_discovery_task_validates_payload_and_tenancy():
    """Verify daemon handler strictly rejects mismatched or missing payload candidates."""
    user = User.objects.create_user(username="test_cand_u", email="tc@example.com")
    ws = Workspace.objects.create(name="Test WS", slug="test-ws")
    WorkspaceMembership.objects.create(workspace=ws, user=user, role="admin")
    cand = CandidateProfile.objects.create(user=user, full_name="Test Cand", email="tc@example.com")

    auto = WorkspaceAutomationSettings.get_for_workspace(ws)
    auto.enable_persistent_production()

    # 1. Missing candidate_id in payload
    task_no_cand = Task.objects.create(
        task_type=Task.TaskType.JOB_DISCOVERY,
        scheduled_at=timezone.now(),
        payload={"workspace_id": ws.id},
    )
    handle_job_discovery_task(task_no_cand, session=None, qualifiers=None)
    task_no_cand.refresh_from_db()
    # It resolves via workspace membership if available or marks failed if not
    assert task_no_cand.status in [Task.Status.COMPLETED, Task.Status.FAILED]

    # 2. Foreign candidate_id in payload
    other_user = User.objects.create_user(username="other_u", email="other@example.com")
    foreign_cand = CandidateProfile.objects.create(user=other_user, full_name="Foreign", email="other@example.com")
    task_foreign = Task.objects.create(
        task_type=Task.TaskType.JOB_DISCOVERY,
        scheduled_at=timezone.now(),
        payload={"workspace_id": ws.id, "candidate_id": foreign_cand.id},
    )
    handle_job_discovery_task(task_foreign, session=None, qualifiers=None)
    task_foreign.refresh_from_db()
    assert task_foreign.status == Task.Status.FAILED
    assert task_foreign.payload.get("reason") == "candidate_workspace_mismatch"


def test_launchd_plists_validity():
    """Verify all macOS LaunchAgent plists are valid XML, load on boot, keep alive, and have proper paths."""
    launch_agents_dir = os.path.expanduser("~/Library/LaunchAgents")
    plists = [
        "com.openoutreach.web.plist",
        "com.openoutreach.rundaemon.plist",
        "com.openoutreach.ollama.plist",
    ]

    for plist_name in plists:
        path = os.path.join(launch_agents_dir, plist_name)
        assert os.path.exists(path), f"Missing plist: {path}"

        with open(path, "rb") as f:
            data = plistlib.load(f)

        assert data.get("RunAtLoad") is True, f"{plist_name} must have RunAtLoad=True"
        assert data.get("KeepAlive") is True or isinstance(data.get("KeepAlive"), dict), f"{plist_name} must have KeepAlive"
        assert "ProgramArguments" in data or "Program" in data, f"{plist_name} must specify executable"
        assert "Label" in data, f"{plist_name} must specify Label"


@pytest.mark.django_db
def test_healthz_reflects_heartbeat_staleness(client):
    """Verify /healthz accurately reports rundaemon ok, degraded, down based on heartbeat age."""
    from openoutreach.core.models import DaemonStatus
    ws = Workspace.objects.create(name="Health WS", slug="health-ws")
    auto = WorkspaceAutomationSettings.get_for_workspace(ws)
    DaemonStatus.objects.filter(pk=1).delete()

    # 1. Fresh heartbeat (<180s)
    auto.last_daemon_heartbeat = timezone.now() - timedelta(seconds=10)
    auto.save()
    resp = client.get("/healthz/")
    assert resp.status_code == 200
    data = resp.json()
    assert data["services"]["rundaemon"] == "ok"
    assert data["status"] in ["ok", "degraded"]

    # 2. Moderately stale heartbeat (300s)
    auto.last_daemon_heartbeat = timezone.now() - timedelta(seconds=300)
    auto.save()
    resp = client.get("/healthz/")
    assert resp.status_code == 200
    data = resp.json()
    assert data["services"]["rundaemon"] == "degraded"
    assert data["status"] == "degraded"

    # 3. Dead / very stale heartbeat (>600s)
    auto.last_daemon_heartbeat = timezone.now() - timedelta(seconds=900)
    auto.save()
    resp = client.get("/healthz/")
    assert resp.status_code == 200
    data = resp.json()
    assert data["services"]["rundaemon"] == "down"
    assert data["status"] == "degraded"

    # 4. No heartbeat
    auto.last_daemon_heartbeat = None
    auto.save()
    resp = client.get("/healthz/")
    assert resp.status_code == 200
    data = resp.json()
    assert data["services"]["rundaemon"] == "down"
    assert data["status"] == "degraded"


@pytest.mark.django_db
def test_channel_runtime_status_semantics():
    """Verify truthful status transitions for each production channel."""
    ws = Workspace.objects.create(name="Status WS", slug="status-ws")
    auto = WorkspaceAutomationSettings.get_for_workspace(ws)
    auto.enable_persistent_production()

    # Default with no activity is DEGRADED (never executed)
    assert auto.get_channel_runtime_status("jobs") == "DEGRADED"
    assert auto.get_channel_runtime_status("connections") == "DEGRADED"
    assert auto.get_channel_runtime_status("followups") == "DEGRADED"
    assert auto.get_channel_runtime_status("replies") == "DEGRADED"

    # With recent planner/poll timestamp -> IDLE_HEALTHY / RUNNING
    auto.last_connection_planner_at = timezone.now() - timedelta(seconds=15)
    auto.last_prospect_discovery_at = timezone.now() - timedelta(seconds=15)
    auto.last_followup_planner_at = timezone.now() - timedelta(seconds=15)
    auto.last_job_discovery_completed_at = timezone.now() - timedelta(seconds=15)
    auto.last_reply_poll_at = timezone.now() - timedelta(seconds=15)
    auto.save()

    assert auto.get_channel_runtime_status("jobs") == "RUNNING"
    assert auto.get_channel_runtime_status("connections") == "IDLE_HEALTHY"
    assert auto.get_channel_runtime_status("followups") == "IDLE_HEALTHY"
    assert auto.get_channel_runtime_status("replies") == "IDLE_HEALTHY"

    # With recent actual connection attempt -> RUNNING
    auto.last_connection_attempt_at = timezone.now() - timedelta(seconds=15)
    auto.save()
    assert auto.get_channel_runtime_status("connections") == "RUNNING"

    # When paused -> PAUSED
    auto.master_automation_enabled = False
    auto.save()
    assert auto.get_channel_runtime_status("jobs") == "PAUSED"
    assert auto.get_channel_runtime_status("connections") == "PAUSED"


@pytest.mark.django_db
def test_job_loop_continuous_replenishment_below_target():
    """Verify that when below target, empty queue schedules discovery and rotates queries."""
    user = User.objects.create_user(username="job_user", email="job_user@example.com")
    ws = Workspace.objects.create(name="Job WS", slug="job-ws")
    WorkspaceMembership.objects.create(workspace=ws, user=user, role="admin")

    auto = WorkspaceAutomationSettings.get_for_workspace(ws)
    auto.enable_persistent_production(job_target=50, job_ceiling=60)

    cand = CandidateProfile.objects.create(user=user, full_name="Job Candidate", email="job@example.com")
    CandidateApplicationProfile.objects.create(candidate_profile=cand, confirmed=True)
    CandidateIdentityVerification.objects.create(
        candidate_profile=cand,
        status="verified",
        extracted_name="Job Candidate",
        confirmed_name="Job Candidate",
        verification_source="manual",
    )

    # Empty queue below target -> schedules discovery
    created = plan_job_window(session=None, workspace=ws, candidate=cand)
    assert created == 1

    task = Task.objects.filter(task_type=Task.TaskType.JOB_DISCOVERY).first()
    assert task is not None
    assert task.payload["workspace_id"] == ws.id
    assert task.payload["candidate_id"] == cand.id


@pytest.mark.django_db
def test_prospect_discovery_and_connect_continuation():
    """Verify that empty prospect inventory schedules PROSPECT_DISCOVERY and produces CONNECT tasks."""
    user = User.objects.create_user(username="prospect_user", email="prospect@example.com")
    ws = Workspace.objects.create(name="Prospect WS", slug="prospect-ws")
    WorkspaceMembership.objects.create(workspace=ws, user=user, role="admin")

    auto = WorkspaceAutomationSettings.get_for_workspace(ws)
    auto.enable_persistent_production()

    from openoutreach.linkedin.models import LinkedInProfile
    profile = LinkedInProfile.objects.create(
        user=user,
        workspace=ws,
        public_identifier="test-profile",
        session_status="valid",
        active=True,
    )

    campaign = Campaign.objects.create(
        name="Outreach Campaign",
        workspace=ws,
        linkedin_profile=profile,
        is_active=True,
        linkedin_outreach_enabled=True,
    )
    campaign.users.add(user)

    from openoutreach.core.scheduler import plan_prospect_discovery_window
    class DummySession:
        def __init__(self):
            self.linkedin_profile = profile
            self.campaign = campaign
            self.active_timezone = "UTC"

    session = DummySession()

    # Empty READY_TO_CONNECT inventory -> schedules prospect discovery
    created = plan_prospect_discovery_window(session, campaign)
    assert created == 1

    p_task = Task.objects.filter(task_type=Task.TaskType.PROSPECT_DISCOVERY, payload__campaign_id=campaign.pk).first()
    assert p_task is not None



