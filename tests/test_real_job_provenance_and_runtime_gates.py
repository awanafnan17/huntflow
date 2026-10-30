"""Tests for Job Provenance, Legacy False vs Unknown handling, and Master Automation Precedence."""

import pytest
from django.utils import timezone
from django.contrib.auth import get_user_model

from openoutreach.core.models import Workspace, WorkspaceMembership, Task, SiteConfig, WorkspaceAutomationSettings
from openoutreach.core.services.external_action_gates import evaluate_external_action_gates
from openoutreach.career.models import (
    CandidateProfile, CandidateProfileVersion, CandidateIdentityVerification,
    CandidateApplicationProfile, Job, JobApplication
)
from openoutreach.career.services.job_application_engine import prepare_job_application, authorize_job_application
from openoutreach.career.answer_bank import get_answer_for_question, calculate_application_profile_readiness

User = get_user_model()


@pytest.mark.django_db
class TestJobProvenanceAndRuntimeGates:

    @pytest.fixture(autouse=True)
    def setup_data(self, db):
        self.user = User.objects.create_user(username="test_prov_user", email="prov@example.com")
        self.ws = Workspace.objects.create(name="Provenance WS", slug="prov-ws")
        self.mem = WorkspaceMembership.objects.create(workspace=self.ws, user=self.user, role="owner", status="active")
        self.auto_settings = WorkspaceAutomationSettings.objects.create(
            workspace=self.ws,
            master_automation_enabled=True,
            auto_find_jobs=True,
            auto_apply_jobs=True
        )

        self.candidate = CandidateProfile.objects.create(
            user=self.user,
            full_name="Alex Morgan",
            email="candidate-2@example.com"
        )
        self.cpv = CandidateProfileVersion.objects.create(
            profile=self.candidate,
            version_number=1,
            state="approved",
            full_name=self.candidate.full_name,
            fact_snapshot=[{"category": "Skills", "value": "Python, Django, React"}]
        )
        self.candidate.active_version = self.cpv
        self.candidate.save()

        CandidateIdentityVerification.objects.create(
            candidate_profile=self.candidate,
            extracted_name=self.candidate.full_name,
            confirmed_name=self.candidate.full_name,
            verification_source="manual",
            status="verified",
            verified_at=timezone.now()
        )

    def test_example_com_job_blocked_from_real_provider_classification(self):
        """1. Jobs with example.com URLs are classified as TEST_FIXTURE and blocked from production."""
        job = Job.objects.create(
            company="Netflix",
            title="Stream",
            job_url="https://example.com/jobs/sq",
            description="A substantive description of distributed streaming software engineering systems.",
            provenance="TEST_FIXTURE",
            is_active=True
        )
        assert job.is_real_provider_job() is False

        allowed, reason = evaluate_external_action_gates(
            workspace=self.ws,
            candidate_profile=self.candidate,
            job=job,
            external_action_authorized=True
        )
        assert allowed is False
        assert reason in ["invalid_job_url_domain", "unverified_job_provenance"]

    def test_blank_url_job_blocked(self):
        """2. Jobs with blank URLs are blocked from production auto-apply."""
        job = Job.objects.create(
            company="any",
            title="python",
            job_url="",
            description="Short placeholder",
            provenance="UNKNOWN",
            is_active=True
        )
        assert job.is_real_provider_job() is False

        allowed, reason = evaluate_external_action_gates(
            workspace=self.ws,
            candidate_profile=self.candidate,
            job=job,
            external_action_authorized=True
        )
        assert allowed is False
        assert reason in ["missing_job_url", "invalid_job_url_domain", "unverified_job_provenance"]

    def test_real_linkedin_provenance_accepted(self):
        """3. Real LinkedIn jobs with valid URLs and numeric IDs are accepted."""
        job = Job.objects.create(
            company="Xorora",
            title="Senior Software Engineer",
            job_url="https://www.linkedin.com/jobs/view/4452269262/",
            description="We are looking for a Senior Software Engineer with Python, Django, PostgreSQL experience.",
            provider="linkedin",
            external_job_id="4452269262",
            provenance="REAL_PROVIDER",
            provider_identity_status="VERIFIED",
            is_active=True
        )
        assert job.is_real_provider_job() is True

        allowed, reason = evaluate_external_action_gates(
            workspace=self.ws,
            candidate_profile=self.candidate,
            job=job,
            external_action_authorized=True
        )
        assert allowed is True
        assert reason == "authorized"

    def test_workspace_master_pause_dominates_siteconfig_true(self):
        """4. Workspace master pause dominates SiteConfig=True and blocks automated action."""
        SiteConfig.objects.update_or_create(
            id=1,
            defaults={"auto_find_jobs": True, "auto_apply_jobs": True}
        )
        self.auto_settings.master_automation_enabled = False
        self.auto_settings.save(update_fields=["master_automation_enabled"])

        job = Job.objects.create(
            company="RealCo",
            title="Python Dev",
            job_url="https://www.linkedin.com/jobs/view/4452269999/",
            description="Substantive description with Python and Django requirements.",
            provider="linkedin",
            external_job_id="4452269999",
            provenance="REAL_PROVIDER",
            is_active=True
        )

        allowed, reason = evaluate_external_action_gates(
            workspace=self.ws,
            candidate_profile=self.candidate,
            job=job,
            external_action_authorized=False  # automated daemon run
        )
        assert allowed is False
        assert reason == "workspace_master_automation_paused"

    def test_legacy_false_does_not_become_approved_no(self):
        """5. Unanswered tri-state questions return UNKNOWN_REQUIRES_USER instead of guessing 'No'."""
        app_prof = CandidateApplicationProfile.objects.create(
            candidate_profile=self.candidate,
            visa_sponsorship_now="unknown",
            visa_sponsorship_future="unknown",
            relocation_willingness="unknown",
            confirmed=False
        )

        # Question about sponsorship
        status, val, key = get_answer_for_question(self.candidate, "Do you need visa sponsorship now?")
        assert status == "UNKNOWN_REQUIRES_USER"
        assert val is None

        # Question about relocation
        status, val, key = get_answer_for_question(self.candidate, "Are you willing to relocate for this role?")
        assert status == "UNKNOWN_REQUIRES_USER"
        assert val is None

    def test_explicit_operator_no_is_respected_when_confirmed(self):
        """6. Explicitly selected 'no' on confirmed profile returns False / No."""
        app_prof, _ = CandidateApplicationProfile.objects.get_or_create(candidate_profile=self.candidate)
        app_prof.visa_sponsorship_now = "no"
        app_prof.relocation_willingness = "no"
        app_prof.confirmed = True
        app_prof.save()

        status, val, key = get_answer_for_question(self.candidate, "Do you need visa sponsorship now?")
        assert status == "ANSWER_FOUND"
        assert val is False

        status, val, key = get_answer_for_question(self.candidate, "Are you willing to relocate?")
        assert status == "ANSWER_FOUND"
        assert val is False

    def test_profile_readiness_marks_unanswered_items_as_not_ready(self):
        """7. Candidate with missing/unknown fields computes accurate score and overall NOT READY."""
        app_prof, _ = CandidateApplicationProfile.objects.get_or_create(candidate_profile=self.candidate)
        app_prof.visa_sponsorship_now = "unknown"
        app_prof.relocation_willingness = "unknown"
        app_prof.phone = ""
        app_prof.confirmed = False
        app_prof.save()

        readiness = calculate_application_profile_readiness(self.candidate)
        assert readiness["is_ready"] is False
        assert readiness["overall_status"] == "NOT READY"
        assert any("Phone" in item for item in readiness["missing_items"])
        assert any("Sponsorship" in item or "Eligibility" in item for item in readiness["missing_items"])
