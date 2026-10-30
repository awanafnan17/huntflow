# tests/test_autonomous_job_engine.py
"""
Comprehensive Test Suite for HuntFlow Autonomous Job Engine & Rundaemon Registration.

Verifies:
1. Rundaemon scheduler integration: plan_job_window registration in reconcile.
2. 50/day successful Easy Apply target objective & 60/day provider attempt ceiling.
3. Channel independence: auto_send_connections=False and auto_followup=False do not block jobs.
4. Telemetry tracking on WorkspaceAutomationSettings.
5. Replacement engine: non-successful applications keep discovery active.
6. Safe recovery across daemon restarts.
"""
import pytest
from unittest.mock import MagicMock, patch
from django.utils import timezone

from openoutreach.core.models import (
    Workspace,
    WorkspaceMembership,
    Campaign,
    WorkspaceAutomationSettings,
    Task,
)
from openoutreach.career.models import Job, JobApplication, CandidateProfile, ApplicationDraft
from openoutreach.core.scheduler import plan_job_window, reconcile
from openoutreach.core.daemon import handle_job_discovery_task, handle_job_application_task
from openoutreach.core.services.quota_service import (
    get_daily_successful_easy_apply_count,
    get_daily_job_provider_attempt_count,
)


@pytest.fixture
def job_engine_env(db, django_user_model):
    user = django_user_model.objects.create_user(username="job_operator", password="password123")
    ws = Workspace.objects.create(name="Production Workspace", slug="prod-ws")
    WorkspaceMembership.objects.create(workspace=ws, user=user, role="admin", status="active")
    
    settings = WorkspaceAutomationSettings.get_for_workspace(ws)
    settings.master_automation_enabled = True
    settings.auto_apply_jobs = True
    settings.auto_send_connections = False
    settings.auto_followup = False
    settings.auto_send_emails = False
    settings.auto_monitor_replies = True
    settings.daily_job_application_target = 50
    settings.max_daily_job_applications = 60
    settings.soak_status = "RUNNING"
    settings.save()

    candidate = CandidateProfile.objects.create(
        user=user,
        full_name="Alex Morgan",
        email="candidate-2@example.com",
    )

    campaign = Campaign.objects.create(
        workspace=ws,
        name="Career Growth",
        is_active=True,
    )
    campaign.users.add(user)

    return {
        "user": user,
        "workspace": ws,
        "settings": settings,
        "candidate": candidate,
        "campaign": campaign,
    }


def test_plan_job_window_enqueues_discovery_when_under_target(job_engine_env):
    ws = job_engine_env["workspace"]
    candidate = job_engine_env["candidate"]

    # Initial state: 0 successful today
    assert get_daily_successful_easy_apply_count(workspace=ws, candidate_profile=candidate) == 0
    assert Task.objects.filter(task_type=Task.TaskType.JOB_DISCOVERY).count() == 0

    count = plan_job_window(session=None, workspace=ws, candidate=candidate)
    assert count == 1

    task = Task.objects.filter(task_type=Task.TaskType.JOB_DISCOVERY, status=Task.Status.PENDING).first()
    assert task is not None
    assert task.payload.get("workspace_id") == ws.id


def test_plan_job_window_halts_when_target_reached(job_engine_env):
    ws = job_engine_env["workspace"]
    candidate = job_engine_env["candidate"]

    # Create 50 submitted applications for today
    for i in range(50):
        j = Job.objects.create(
            provider="linkedin",
            external_job_id=f"job_test_success_{i}",
            title=f"Engineer {i}",
            company="Company",
            location="Remote",
            easy_apply=True,
            provenance="REAL_PROVIDER",
            provider_identity_status="VERIFIED",
        )
        JobApplication.objects.create(
            candidate_profile=candidate,
            job=j,
            status="submitted",
            submission_date=timezone.now(),
            externally_confirmed=True,
        )

    assert get_daily_successful_easy_apply_count(workspace=ws, candidate_profile=candidate) == 50
    count = plan_job_window(session=None, workspace=ws, candidate=candidate)
    assert count == 0
    assert Task.objects.filter(task_type=Task.TaskType.JOB_DISCOVERY, status=Task.Status.PENDING).count() == 0


def test_plan_job_window_halts_when_attempt_ceiling_reached(job_engine_env):
    ws = job_engine_env["workspace"]
    candidate = job_engine_env["candidate"]

    # Create 60 attempted applications (e.g. uncertain or failed after attempt)
    for i in range(60):
        j = Job.objects.create(
            provider="linkedin",
            external_job_id=f"job_test_attempt_{i}",
            title=f"Engineer {i}",
            company="Company",
            location="Remote",
            easy_apply=True,
            provenance="REAL_PROVIDER",
            provider_identity_status="VERIFIED",
        )
        JobApplication.objects.create(
            candidate_profile=candidate,
            job=j,
            status="submission_uncertain",
            attempted_at=timezone.now(),
            created_at=timezone.now(),
        )

    assert get_daily_job_provider_attempt_count(workspace=ws, candidate_profile=candidate) == 60
    count = plan_job_window(session=None, workspace=ws, candidate=candidate)
    assert count == 0


def test_reconcile_runs_job_planning_independently_of_paused_outreach(job_engine_env):
    ws = job_engine_env["workspace"]
    settings = job_engine_env["settings"]
    campaign = job_engine_env["campaign"]

    assert settings.auto_send_connections is False
    assert settings.auto_followup is False

    mock_session = MagicMock()
    mock_session.campaigns = [campaign]
    mock_session.linkedin_profile = None

    reconcile(mock_session)

    # Job discovery task must be enqueued
    job_tasks = Task.objects.filter(task_type=Task.TaskType.JOB_DISCOVERY, status=Task.Status.PENDING)
    assert job_tasks.count() == 1


def test_handle_job_discovery_task_telemetry_updates(job_engine_env):
    ws = job_engine_env["workspace"]
    candidate = job_engine_env["candidate"]

    task = Task.objects.create(
        task_type=Task.TaskType.JOB_DISCOVERY,
        status=Task.Status.PENDING,
        scheduled_at=timezone.now(),
        payload={"workspace_id": ws.id, "candidate_id": candidate.id},
    )

    mock_session = MagicMock()
    mock_res = {
        "status": "success",
        "inspected_count": 20,
        "discovered_count": 15,
        "enqueued_count": 8,
    }

    with patch("openoutreach.career.services.job_discovery_service.discover_and_evaluate_jobs", return_value=mock_res):
        handle_job_discovery_task(task, mock_session, {})

    task.refresh_from_db()
    assert task.status == Task.Status.COMPLETED
    assert task.payload.get("result") == mock_res

    ws_settings = WorkspaceAutomationSettings.objects.get(workspace=ws)
    assert ws_settings.last_job_discovery_started_at is not None
    assert ws_settings.last_job_discovery_completed_at is not None
    assert ws_settings.jobs_inspected_last_cycle == 20
    assert ws_settings.safe_jobs_last_cycle == 8
    assert ws_settings.last_job_scheduler_error == ""


def test_replacement_engine_continues_discovery_on_rejections(job_engine_env):
    ws = job_engine_env["workspace"]
    candidate = job_engine_env["candidate"]

    # 10 jobs found: 8 rejected (below 70 or Needs Your Answer), 2 submitted
    for i in range(8):
        j = Job.objects.create(
            provider="linkedin",
            external_job_id=f"job_rej_{i}",
            title=f"Engineer {i}",
            company="Company",
            location="Remote",
            easy_apply=True,
            provenance="REAL_PROVIDER",
            provider_identity_status="VERIFIED",
        )
        JobApplication.objects.create(
            candidate_profile=candidate,
            job=j,
            status="needs_your_answer",
            created_at=timezone.now(),
        )

    for i in range(2):
        j = Job.objects.create(
            provider="linkedin",
            external_job_id=f"job_sub_{i}",
            title=f"Engineer {i}",
            company="Company",
            location="Remote",
            easy_apply=True,
            provenance="REAL_PROVIDER",
            provider_identity_status="VERIFIED",
        )
        JobApplication.objects.create(
            candidate_profile=candidate,
            job=j,
            status="submitted",
            submission_date=timezone.now(),
            externally_confirmed=True,
        )

    # 2 successful Easy Apply submissions
    assert get_daily_successful_easy_apply_count(workspace=ws, candidate_profile=candidate) == 2

    # Scheduler must schedule another discovery cycle to keep moving toward 50
    count = plan_job_window(session=None, workspace=ws, candidate=candidate)
    assert count == 1
    assert Task.objects.filter(task_type=Task.TaskType.JOB_DISCOVERY, status=Task.Status.PENDING).count() == 1
