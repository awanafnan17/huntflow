import pytest
from datetime import timedelta
from django.utils import timezone
from django.test import Client
from django.contrib.auth.models import User
from openoutreach.core.models import Workspace, WorkspaceAutomationSettings, OperatorActionApproval, Campaign
from openoutreach.career.models import (
    CandidateProfile, CandidateProfileVersion, CandidateIdentityVerification,
    CandidateApplicationProfile, CandidateFact, Job, JobApplication
)
from openoutreach.crm.models import Deal, Lead
from openoutreach.core.services.external_action_gates import evaluate_external_action_gates
from openoutreach.career.services.job_application_engine import evaluate_external_action_gates as eval_canary_gates

@pytest.mark.django_db
def test_workspace_automation_settings_defaults_and_modes():
    workspace = Workspace.objects.create(name="Test WS", slug="test-ws-gov")
    settings = WorkspaceAutomationSettings.get_for_workspace(workspace)

    assert settings.mode == "COMMISSIONING"
    assert settings.max_daily_job_applications == 2
    assert settings.max_daily_connections == 5
    assert settings.max_daily_messages == 10
    assert settings.max_daily_emails == 0
    assert settings.max_pending_invitations == 100
    assert settings.connection_backlog_behavior == "PAUSE_NEW_CONNECTIONS"

    # Heartbeat testing
    assert not settings.is_heartbeat_healthy(threshold_seconds=60)
    settings.record_heartbeat()
    assert settings.is_heartbeat_healthy(threshold_seconds=60)

    # Stale heartbeat
    settings.last_daemon_heartbeat = timezone.now() - timedelta(seconds=120)
    settings.save(update_fields=["last_daemon_heartbeat"])
    assert not settings.is_heartbeat_healthy(threshold_seconds=60)


@pytest.mark.django_db
def test_backlog_governance_gate_pauses_connections_not_jobs():
    user = User.objects.create(username="test_op_gov")
    workspace = Workspace.objects.create(name="Test Backlog WS", slug="test-backlog-ws")
    settings = WorkspaceAutomationSettings.get_for_workspace(workspace)
    settings.max_pending_invitations = 5
    settings.save()

    candidate = CandidateProfile.objects.create(
        user=user,
        full_name="Gov Candidate",
        email="gov@example.com",
        phone="+12025550102",
    )
    CandidateIdentityVerification.objects.create(
        candidate_profile=candidate,
        status="verified",
        extracted_name=candidate.full_name,
        confirmed_name=candidate.full_name,
        verification_source="manual"
    )

    ver = CandidateProfileVersion.objects.create(
        profile=candidate,
        version_number=1,
        full_name=candidate.full_name,
        email=candidate.email,
        phone=candidate.phone,
        state="approved",
        is_finalized=True,
        fact_snapshot=[{"fact_type": "skill", "category": "Python", "value": "Python", "verified": True}]
    )
    candidate.active_version = ver
    candidate.save(update_fields=["active_version"])

    app_prof, _ = CandidateApplicationProfile.objects.get_or_create(candidate_profile=candidate)
    app_prof.full_name = candidate.full_name
    app_prof.email = candidate.email
    app_prof.phone = candidate.phone
    app_prof.city = "New York"
    app_prof.country = "United States"
    app_prof.authorized_countries = ["US"]
    app_prof.visa_sponsorship_now = "yes"
    app_prof.visa_sponsorship_future = "yes"
    app_prof.relocation_willingness = "yes"
    app_prof.remote_willingness = "yes"
    app_prof.hybrid_willingness = "yes"
    app_prof.onsite_willingness = "yes"
    app_prof.travel_willingness = "yes"
    app_prof.background_check_willingness = "yes"
    app_prof.work_mode_preference = "remote"
    app_prof.minimum_salary = 100000
    app_prof.expected_salary = 120000
    app_prof.salary_period = "annual"
    app_prof.currency = "USD"
    app_prof.confirmed = True
    app_prof.save()

    job, _ = Job.upsert_discovered_job(
        provider="linkedin",
        external_job_id="9990001",
        title="Python Engineer",
        company="Tech Corp",
        location="New York, NY",
        description="Must have Python, Django, PostgreSQL experience. 5+ years building backend systems.",
        easy_apply=True,
        provenance="REAL_PROVIDER",
        provider_identity_status="VERIFIED"
    )

    campaign = Campaign.objects.create(name="Test Camp", workspace=workspace)

    # Populate 5 pending deals in CRM
    for i in range(5):
        lead = Lead.objects.create(
            workspace=workspace,
            public_identifier=f"lead_gov_{i}",
            linkedin_url=f"https://www.linkedin.com/in/lead_gov_{i}/",
            first_name=f"Lead{i}",
            last_name="Test"
        )
        Deal.objects.create(campaign=campaign, lead=lead, state="Pending")

    # Verify pending deal count triggers backlog reason
    pending_count = Deal.objects.filter(campaign__workspace=workspace, state="Pending").count()
    assert pending_count >= settings.max_pending_invitations

    # Jobs must still be eligible with approval
    approval = OperatorActionApproval.objects.create(
        workspace=workspace,
        operator=user,
        action_type="job_application",
        candidate=candidate,
        job=job,
        external_job_id=job.external_job_id,
        status="approved"
    )

    allowed, reason = eval_canary_gates(
        workspace=workspace,
        candidate_profile=candidate,
        job=job,
        operator_approval=approval,
        external_action_authorized=True,
        action_type="job_application"
    )
    assert allowed is True, f"Jobs should remain eligible under backlog pause, got reasons: {reason}"


@pytest.mark.django_db
def test_daily_job_application_limit_enforced():
    user = User.objects.create(username="test_daily_op")
    workspace = Workspace.objects.create(name="Daily WS", slug="test-daily-ws")
    settings = WorkspaceAutomationSettings.get_for_workspace(workspace)
    settings.max_daily_job_applications = 1
    settings.master_automation_enabled = True
    settings.auto_apply_jobs = True
    settings.save()

    candidate = CandidateProfile.objects.create(
        user=user,
        full_name="Daily Candidate",
        email="daily@example.com",
        phone="+12025550101",
    )
    CandidateIdentityVerification.objects.create(
        candidate_profile=candidate,
        status="verified",
        extracted_name=candidate.full_name,
        confirmed_name=candidate.full_name,
        verification_source="manual"
    )

    ver = CandidateProfileVersion.objects.create(
        profile=candidate,
        version_number=1,
        full_name=candidate.full_name,
        email=candidate.email,
        phone=candidate.phone,
        state="approved",
        is_finalized=True,
        fact_snapshot=[{"fact_type": "skill", "category": "Python", "value": "Python", "verified": True}]
    )
    candidate.active_version = ver
    candidate.save(update_fields=["active_version"])

    app_prof, _ = CandidateApplicationProfile.objects.get_or_create(candidate_profile=candidate)
    app_prof.full_name = candidate.full_name
    app_prof.email = candidate.email
    app_prof.phone = candidate.phone
    app_prof.city = "New York"
    app_prof.country = "United States"
    app_prof.authorized_countries = ["US"]
    app_prof.visa_sponsorship_now = "yes"
    app_prof.visa_sponsorship_future = "yes"
    app_prof.relocation_willingness = "yes"
    app_prof.remote_willingness = "yes"
    app_prof.hybrid_willingness = "yes"
    app_prof.onsite_willingness = "yes"
    app_prof.travel_willingness = "yes"
    app_prof.background_check_willingness = "yes"
    app_prof.work_mode_preference = "remote"
    app_prof.minimum_salary = 100000
    app_prof.expected_salary = 120000
    app_prof.salary_period = "annual"
    app_prof.currency = "USD"
    app_prof.confirmed = True
    app_prof.save()

    job1, _ = Job.upsert_discovered_job(
        provider="linkedin",
        external_job_id="9990002",
        title="Python Engineer 1",
        company="Tech Corp 1",
        location="New York, NY",
        description="Must have Python, Django, PostgreSQL experience.",
        easy_apply=True,
        provenance="REAL_PROVIDER",
        provider_identity_status="VERIFIED"
    )

    job2, _ = Job.upsert_discovered_job(
        provider="linkedin",
        external_job_id="9990003",
        title="Python Engineer 2",
        company="Tech Corp 2",
        location="New York, NY",
        description="Must have Python, Django, PostgreSQL experience.",
        easy_apply=True,
        provenance="REAL_PROVIDER",
        provider_identity_status="VERIFIED"
    )

    # Record 1 application submitted today
    JobApplication.objects.create(
        candidate_profile=candidate,
        job=job1,
        status="submitted",
        externally_confirmed=True,
        submission_date=timezone.now()
    )

    # Without explicit one-time approval, 2nd application must be blocked by daily limit
    allowed, reason = evaluate_external_action_gates(
        workspace=workspace,
        candidate_profile=candidate,
        job=job2,
        operator_approval=None,
        external_action_authorized=True,
        action_type="job_application"
    )
    assert allowed is False
    assert "daily_job_application_limit_reached" in reason


@pytest.mark.django_db
def test_healthz_endpoint_reports_runtime_services():
    client = Client()
    response = client.get("/healthz/")
    assert response.status_code in (200, 503)
    data = response.json()
    assert data["status"] in ("ok", "degraded")
    assert "services" in data
    assert "web" in data["services"]
    assert "database" in data["services"]
    assert "ollama" in data["services"]
    assert "rundaemon" in data["services"]
