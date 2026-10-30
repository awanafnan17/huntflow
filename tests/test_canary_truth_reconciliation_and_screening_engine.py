"""Deterministic tests for Canary Truth Reconciliation, Screening Answer Safety Engine, and Controlled External Confirmation."""
import pytest
from django.utils import timezone
from django.contrib.auth import get_user_model

from openoutreach.core.models import Workspace, WorkspaceMembership, Task
from openoutreach.core.services.entitlements import get_or_create_workspace_entitlement
from openoutreach.career.models import (
    CandidateProfile, CandidateProfileVersion, CandidateIdentityVerification,
    CandidateApplicationProfile, Job, JobApplication, ApplicationDraft, JobApplicationAnswer
)
from openoutreach.career.services.job_application_engine import (
    prepare_job_application, execute_job_application, record_external_confirmation,
    check_candidate_profile_readiness, answer_screening_question_and_resume_application,
    enqueue_job_application_task
)
from openoutreach.career.answer_bank import get_answer_for_question, save_answer

User = get_user_model()


@pytest.mark.django_db
class TestCanaryTruthAndScreeningEngine:

    @pytest.fixture(autouse=True)
    def setup_data(self, db):
        self.user = User.objects.create_user(username="truth_user", email="truth@example.com")
        self.ws = Workspace.objects.create(name="Truth WS", slug="truth-ws")
        WorkspaceMembership.objects.create(workspace=self.ws, user=self.user, role="owner", status="active")
        get_or_create_workspace_entitlement(self.ws)

        self.candidate = CandidateProfile.objects.create(
            user=self.user,
            full_name="Alex Morgan",
            email="awancandidate17@example.com"
        )
        self.cpv = CandidateProfileVersion.objects.create(
            profile=self.candidate,
            version_number=1,
            state="approved",
            full_name=self.candidate.full_name,
            fact_snapshot=[
                {"category": "Skills", "value": "Python"},
                {"category": "Skills", "value": "Django"},
                {"category": "Skills", "value": "PostgreSQL"},
                {"category": "Skills", "value": "React"}
            ]
        )
        self.candidate.active_version = self.cpv
        self.candidate.save()

        self.app_profile = CandidateApplicationProfile.objects.create(
            candidate_profile=self.candidate,
            full_name="Alex Morgan",
            email="awancandidate17@example.com",
            phone="+1-202-555-0101",
            city="Lahore",
            country="Pakistan",
            authorized_countries=["PK", "US"],
            visa_sponsorship_now="no",
            visa_sponsorship_future="no",
            relocation_willingness="yes",
            remote_willingness="yes",
            hybrid_willingness="yes",
            onsite_willingness="yes",
            travel_willingness="yes",
            background_check_willingness="yes",
            minimum_salary=100000,
            target_salary=120000,
            currency="USD",
            salary_period="annual",
            willing_to_relocate=True,
            requires_sponsorship_now=False,
            work_modes=["Remote", "Hybrid", "Onsite"],
            confirmed=True,
            confirmed_at=timezone.now()
        )

        CandidateIdentityVerification.objects.create(
            candidate_profile=self.candidate,
            extracted_name=self.candidate.full_name,
            confirmed_name=self.candidate.full_name,
            verification_source="manual",
            status="verified",
            verified_at=timezone.now()
        )

        self.job = Job.objects.create(
            company="Truth Corporation",
            title="Senior Python / Django Engineer",
            job_url="https://www.linkedin.com/jobs/view/9999999999/",
            description="We require strong Python, Django, and PostgreSQL skills for web applications.",
            provenance="REAL_PROVIDER",
            provider="linkedin",
            external_job_id="9999999999",
            provider_identity_status="VERIFIED",
            is_active=True
        )

    def test_record_external_confirmation_provenance(self):
        """Controlled method sets provenance fields and updates status to submitted."""
        app = JobApplication.objects.create(
            candidate_profile=self.candidate,
            job=self.job,
            status="submission_unverified",
            externally_confirmed=False
        )

        now = timezone.now()
        record_external_confirmation(
            application=app,
            provider="linkedin",
            external_job_id="9999999999",
            evidence_type="applied_badge",
            checked_at=now,
            metadata={"source": "verify_linkedin_job_applied", "badge": "Applied 1d ago"}
        )

        app.refresh_from_db()
        assert app.status == "submitted"
        assert app.externally_confirmed is True
        assert app.confirmation_provider == "linkedin"
        assert app.confirmation_external_job_id == "9999999999"
        assert app.confirmation_evidence_type == "applied_badge"
        assert app.confirmation_checked_at == now
        assert app.confirmation_metadata["badge"] == "Applied 1d ago"

    def test_candidate_profile_readiness_gate(self):
        """Check completeness of candidate profile before real job applications."""
        ready, missing = check_candidate_profile_readiness(self.candidate)
        assert ready is True
        assert len(missing) == 0

        # Incomplete profile
        cand_inc = CandidateProfile.objects.create(
            user=User.objects.create_user(username="inc_user", email="inc@example.com"),
            full_name="Incomplete Cand",
            email="inc@example.com"
        )
        ready_inc, missing_inc = check_candidate_profile_readiness(cand_inc)
        assert ready_inc is False
        assert "phone" in missing_inc
        assert "location" in missing_inc
        assert "active_approved_version" in missing_inc

    def test_screening_answer_bank_resolution_and_safety_stop(self):
        """Screening answer bank resolves known preferences and returns safety stop for unknown questions."""
        # 1. Known question from application profile
        status, val, key = get_answer_for_question(self.candidate, "Are you willing to relocate?")
        assert status == "ANSWER_FOUND"
        assert val is True
        assert key == "willing_to_relocate"

        # 2. Sensitive question auto-declined
        status, val, key = get_answer_for_question(self.candidate, "What is your race and ethnicity?")
        assert status == "OPTIONAL_SKIP"
        assert val == "Decline to self-identify"

        # 3. Unknown question stops modal progression
        status, val, key = get_answer_for_question(self.candidate, "What is your clearance level for top secret projects?")
        assert status == "UNKNOWN_REQUIRES_USER"
        assert val is None

    def test_unknown_question_transitions_application_to_needs_your_answer(self):
        """When unknown screening questions exist, execute_job_application sets status='needs_your_answer'."""
        class MockStopAdapter:
            def apply(self, **kwargs):
                return {
                    "success": False,
                    "status": "needs_your_answer",
                    "error": "unknown_required_answers",
                    "unknown_questions": [
                        {"question": "Do you hold an active TS/SCI clearance?", "canonical_key": "ts_clearance"}
                    ]
                }

        prep = prepare_job_application(workspace=self.ws, candidate=self.candidate, job=self.job)
        draft = ApplicationDraft.objects.get(id=prep["draft_id"])
        draft.application.status = "approved"
        draft.application.save()

        res = execute_job_application(
            workspace=self.ws,
            candidate=self.candidate,
            job=self.job,
            draft=draft,
            adapter=MockStopAdapter(),
            external_action_authorized=True
        )

        assert res["status"] == "needs_your_answer"
        app = JobApplication.objects.get(id=draft.application.id)
        assert app.status == "needs_your_answer"
        assert len(app.unknown_questions) == 1
        assert app.unknown_questions[0]["canonical_key"] == "ts_clearance"

    def test_answer_screening_question_and_resume_application(self):
        """Operator can supply answers to unknown questions, updating the draft without creating duplicates."""
        app = JobApplication.objects.create(
            candidate_profile=self.candidate,
            job=self.job,
            status="needs_your_answer",
            unknown_questions=[{"question": "Do you hold an active TS/SCI clearance?", "canonical_key": "ts_clearance"}]
        )
        draft = ApplicationDraft.objects.create(
            application=app,
            profile_version=self.cpv,
            version_number=1,
            tailored_cv="CV text",
            cover_letter="Cover letter text"
        )

        res = answer_screening_question_and_resume_application(
            workspace=self.ws,
            candidate=self.candidate,
            job=self.job,
            answers_update={"ts_clearance": "No"}
        )

        assert res["status"] in ["ready_for_review", "resumed"]
        app.refresh_from_db()
        assert app.status == "approved"
        assert app.unknown_questions == []

        # Confirm answer is saved to answer bank
        status, val, key = get_answer_for_question(self.candidate, "Do you hold an active TS/SCI clearance?", job=self.job)
        assert status == "ANSWER_FOUND"
        assert val == "No"

    def test_task_idempotency_prevents_duplicate_active_tasks(self):
        """At most one PENDING or RUNNING JOB_APPLICATION task can exist for (candidate, job)."""
        prep = prepare_job_application(workspace=self.ws, candidate=self.candidate, job=self.job)
        draft = ApplicationDraft.objects.get(id=prep["draft_id"])

        task1 = enqueue_job_application_task(
            workspace=self.ws,
            candidate=self.candidate,
            job=self.job,
            draft=draft
        )
        assert task1 is not None
        assert task1.status == Task.Status.PENDING

        task2 = enqueue_job_application_task(
            workspace=self.ws,
            candidate=self.candidate,
            job=self.job,
            draft=draft
        )
        assert task2.id == task1.id
        assert Task.objects.filter(task_type=Task.TaskType.JOB_APPLICATION, payload__job_id=self.job.id).count() == 1
