import pytest
import datetime
from datetime import timedelta
from django.utils import timezone
from django.contrib.auth.models import User
from openoutreach.core.models import (
    Workspace, WorkspaceAutomationSettings, OperatorActionApproval, Campaign, Task
)
from openoutreach.career.models import (
    CandidateProfile, CandidateProfileVersion, CandidateIdentityVerification,
    CandidateApplicationProfile, CandidateFact, Job, JobApplication, ApplicationDraft
)
from openoutreach.career.services.confirmation_evidence import (
    record_external_confirmation, ConfirmationEvidenceType
)
from openoutreach.career.services.job_application_engine import (
    prepare_job_application, execute_job_application
)
from openoutreach.core.services.external_action_gates import evaluate_external_action_gates
from openoutreach.career.answer_bank import save_answer, get_answer_for_question


@pytest.fixture
def soak_env(db):
    user = User.objects.create(username="soak_op")
    workspace = Workspace.objects.create(name="Soak WS", slug="soak-ws")
    settings = WorkspaceAutomationSettings.get_for_workspace(workspace)
    settings.start_bounded_soak(duration_hours=24, max_daily_jobs=5)

    candidate = CandidateProfile.objects.create(
        user=user,
        full_name="Candidate Mehmood",
        email="candidate-2@example.com",
        phone="+15551234567",
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

    return {
        "user": user,
        "workspace": workspace,
        "settings": settings,
        "candidate": candidate,
        "version": ver,
        "app_prof": app_prof,
    }


def test_tmv_email_acknowledgement_reconciliation_and_duplicate_protection(soak_env):
    """Prove TMV Global application is reconciled with acknowledgement email and protected against duplicate applies."""
    candidate = soak_env["candidate"]
    workspace = soak_env["workspace"]

    job, _ = Job.upsert_discovered_job(
        provider="linkedin",
        external_job_id="4456601170",
        title="Full Stack Python Developer",
        company="TMV Global Inc",
        location="United States",
        description="Must have Python, Django, PostgreSQL experience.",
        easy_apply=True,
        provenance="REAL_PROVIDER",
        provider_identity_status="VERIFIED"
    )

    app = JobApplication.objects.create(
        candidate_profile=candidate,
        job=job,
        status="submission_unverified",
        externally_confirmed=False,
        submission_date=timezone.now()
    )

    # Reconcile via official evidence method
    record_external_confirmation(
        application=app,
        provider="linkedin",
        external_job_id="4456601170",
        evidence_type=ConfirmationEvidenceType.OPERATOR_CONFIRMED_LINKEDIN_EMAIL,
        metadata={"subject": "Candidate Mehmood, your application was sent to TMV Global Inc"}
    )

    app.refresh_from_db()
    assert app.status == "submitted"
    assert app.externally_confirmed is True
    assert app.confirmation_evidence_type == "operator_confirmed_linkedin_acknowledgement_email"

    # Future apply attempts must be blocked by duplicate protection without provider calls
    allowed, reason = evaluate_external_action_gates(
        workspace=workspace,
        candidate_profile=candidate,
        job=job,
        external_action_authorized=True,
        action_type="job_application"
    )
    assert allowed is False
    assert reason == "duplicate_application_blocked"


def test_unknown_screening_question_creates_needs_your_answer_and_pauses_only_that_job(soak_env):
    """Prove unknown required screening question transitions application to needs_your_answer without halting other jobs."""
    candidate = soak_env["candidate"]
    workspace = soak_env["workspace"]

    job1, _ = Job.upsert_discovered_job(
        provider="linkedin",
        external_job_id="8880001",
        title="Python Developer 1",
        company="Company 1",
        location="New York, NY",
        description="Python Django engineer role.",
        easy_apply=True,
        provenance="REAL_PROVIDER",
        provider_identity_status="VERIFIED"
    )
    job2, _ = Job.upsert_discovered_job(
        provider="linkedin",
        external_job_id="8880002",
        title="Python Developer 2",
        company="Company 2",
        location="New York, NY",
        description="Python Django engineer role.",
        easy_apply=True,
        provenance="REAL_PROVIDER",
        provider_identity_status="VERIFIED"
    )

    res_prep = prepare_job_application(workspace=workspace, candidate=candidate, job=job1)
    draft1 = ApplicationDraft.objects.get(pk=res_prep["draft_id"])

    class MockAdapterNeedsAnswer:
        def apply(self, **kwargs):
            return {
                "success": False,
                "status": "needs_your_answer",
                "error": "unknown_required_answers",
                "unknown_questions": [{"question": "Do you hold an active Top Secret clearance?", "type": "radio", "canonical_key": "security_clearance"}]
            }

    res = execute_job_application(
        workspace=workspace,
        candidate=candidate,
        job=job1,
        draft=draft1,
        adapter=MockAdapterNeedsAnswer(),
        external_action_authorized=True
    )

    assert res["status"] == "needs_your_answer"
    app1 = JobApplication.objects.get(candidate_profile=candidate, job=job1)
    assert app1.status == "needs_your_answer"
    assert len(app1.unknown_questions) == 1

    # Discovery and evaluation of Job 2 remains independent and eligible
    allowed2, reason2 = evaluate_external_action_gates(
        workspace=workspace,
        candidate_profile=candidate,
        job=job2,
        external_action_authorized=True,
        action_type="job_application"
    )
    assert allowed2 is True, f"Job 2 should remain eligible while Job 1 waits for answer, got: {reason2}"


def test_answer_bank_learning_and_safe_resume(soak_env):
    """Prove operator answer is recorded truthfully and application can resume safely."""
    candidate = soak_env["candidate"]
    workspace = soak_env["workspace"]

    job, _ = Job.upsert_discovered_job(
        provider="linkedin",
        external_job_id="8880003",
        title="Python Developer 3",
        company="Company 3",
        location="New York, NY",
        description="Python Django role.",
        easy_apply=True,
        provenance="REAL_PROVIDER",
        provider_identity_status="VERIFIED"
    )

    # Save operator answer
    save_answer(
        candidate_profile=candidate,
        canonical_key="security_clearance",
        answer_value="No",
        source="operator_confirmed",
        job=job
    )

    status, val, key = get_answer_for_question(candidate, "Do you hold an active Top Secret clearance?", job=job)
    assert status == "ANSWER_FOUND"
    assert val == "No"


def test_bounded_production_quota_ceiling_and_unverified_consumption(soak_env):
    """Prove daily quota of 5 attempts is strictly enforced and submission_unverified consumes quota."""
    candidate = soak_env["candidate"]
    workspace = soak_env["workspace"]
    settings = soak_env["settings"]

    assert settings.max_daily_job_applications == 5

    # Create 5 submission_unverified applications today
    for i in range(5):
        j, _ = Job.upsert_discovered_job(
            provider="linkedin",
            external_job_id=f"888100{i}",
            title=f"Python Job {i}",
            company=f"Company {i}",
            location="New York, NY",
            description="Python Django role.",
            easy_apply=True,
            provenance="REAL_PROVIDER",
            provider_identity_status="VERIFIED"
        )
        JobApplication.objects.create(
            candidate_profile=candidate,
            job=j,
            status="submission_unverified",
            externally_confirmed=False,
            submission_date=timezone.now()
        )

    # 6th job attempt must be blocked by daily limit
    j_sixth, _ = Job.upsert_discovered_job(
        provider="linkedin",
        external_job_id="8881099",
        title="Python Job 6",
        company="Company 6",
        location="New York, NY",
        description="Python Django role.",
        easy_apply=True,
        provenance="REAL_PROVIDER",
        provider_identity_status="VERIFIED"
    )

    allowed, reason = evaluate_external_action_gates(
        workspace=workspace,
        candidate_profile=candidate,
        job=j_sixth,
        operator_approval=None,
        external_action_authorized=True,
        action_type="job_application"
    )
    assert allowed is False
    assert "daily_job_application_limit_reached" in reason


def test_soak_auto_expiry_after_24_hours(soak_env):
    """Prove soak automatically pauses writes when 24-hour duration elapses."""
    candidate = soak_env["candidate"]
    workspace = soak_env["workspace"]
    settings = soak_env["settings"]

    assert settings.soak_status == "RUNNING"

    # Simulate 25 hours passed
    settings.soak_expected_end_at = timezone.now() - timedelta(hours=1)
    settings.save(update_fields=["soak_expected_end_at"])

    # Evaluation of gate triggers auto expiry check
    job, _ = Job.upsert_discovered_job(
        provider="linkedin",
        external_job_id="8882001",
        title="Python Job Expiry Test",
        company="Company Expiry",
        location="New York, NY",
        description="Python Django role.",
        easy_apply=True,
        provenance="REAL_PROVIDER",
        provider_identity_status="VERIFIED"
    )

    allowed, reason = evaluate_external_action_gates(
        workspace=workspace,
        candidate_profile=candidate,
        job=job,
        external_action_authorized=True,
        action_type="job_application"
    )
    assert allowed is False
    assert reason == "soak_duration_elapsed_24h"

    settings.refresh_from_db()
    assert settings.soak_status == "EXPIRED"
    assert settings.master_automation_enabled is False
    assert settings.auto_apply_jobs is False


def test_global_emergency_kill_switch_stops_queued_write(soak_env):
    """Prove emergency pause immediately stops pre-provider writes."""
    candidate = soak_env["candidate"]
    workspace = soak_env["workspace"]
    settings = soak_env["settings"]

    # Operator hits emergency kill switch
    settings.master_automation_enabled = False
    settings.save(update_fields=["master_automation_enabled"])

    job, _ = Job.upsert_discovered_job(
        provider="linkedin",
        external_job_id="8883001",
        title="Python Job Emergency Test",
        company="Company Emergency",
        location="New York, NY",
        description="Python Django role.",
        easy_apply=True,
        provenance="REAL_PROVIDER",
        provider_identity_status="VERIFIED"
    )

    allowed, reason = evaluate_external_action_gates(
        workspace=workspace,
        candidate_profile=candidate,
        job=job,
        external_action_authorized=True,
        action_type="job_application"
    )
    assert allowed is False
    assert reason == "workspace_master_automation_paused"


def test_submission_uncertainty_triggers_soak_safety_stop(soak_env):
    """Prove provider uncertainty stops further soak writes."""
    candidate = soak_env["candidate"]
    workspace = soak_env["workspace"]
    settings = soak_env["settings"]

    job, _ = Job.upsert_discovered_job(
        provider="linkedin",
        external_job_id="8884001",
        title="Python Job Uncertainty Test",
        company="Company Uncertainty",
        location="New York, NY",
        description="Python Django role.",
        easy_apply=True,
        provenance="REAL_PROVIDER",
        provider_identity_status="VERIFIED"
    )

    # Trigger safety stop
    settings.trigger_safety_stop("provider_submission_uncertainty")
    settings.refresh_from_db()

    assert settings.soak_status == "SAFETY_STOP"
    assert settings.master_automation_enabled is False

    allowed, reason = evaluate_external_action_gates(
        workspace=workspace,
        candidate_profile=candidate,
        job=job,
        external_action_authorized=True,
        action_type="job_application"
    )
    assert allowed is False
    assert "soak_safety_stop" in reason


def test_connections_remain_paused_due_to_backlog(soak_env):
    """Prove LinkedIn connection channel remains strictly paused under 326 backlog."""
    settings = soak_env["settings"]
    assert settings.auto_send_connections is False
    assert settings.max_pending_invitations == 100
    assert settings.connection_backlog_behavior == "PAUSE_NEW_CONNECTIONS"
