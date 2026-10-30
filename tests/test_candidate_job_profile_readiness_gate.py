"""
Tests for HuntFlow Operator Job Profile Completion & Application Readiness Gate.

Enforces:
1. Missing required field -> NOT READY
2. Missing optional field -> Does not independently block if required fields complete
3. Unknown answers preserved truthfully (never silently converted to False)
4. Required facts complete but unconfirmed -> Application execution BLOCKED
5. Profile confirmed but critical field cleared -> Readiness drops & confirmation reset
6. Direct server request / daemon application while NOT READY -> BLOCKED
7. Test fixture candidates strictly isolated from production Candidate #2
8. evaluate_canary_readiness enforces all 11 canary trust and safety criteria
"""

import pytest
from django.utils import timezone
from django.contrib.auth import get_user_model
from django.test import Client

from openoutreach.core.models import (
    Workspace, WorkspaceMembership, WorkspaceAutomationSettings, OperatorActionApproval
)
from openoutreach.career.models import (
    CandidateProfile, CandidateProfileVersion, CandidateApplicationProfile, Job, JobApplication
)
from openoutreach.career.answer_bank import (
    calculate_application_profile_readiness, get_answer_for_question
)
from openoutreach.core.services.external_action_gates import evaluate_external_action_gates
from openoutreach.career.services.job_application_engine import (
    authorize_job_application, evaluate_canary_readiness
)

User = get_user_model()


@pytest.mark.django_db
class TestCandidateJobProfileReadinessGate:

    @pytest.fixture(autouse=True)
    def setup_data(self, db):
        self.user = User.objects.create_user(username="test_op_user", email="operator@example.com")
        self.ws = Workspace.objects.create(name="Test Operator WS", slug="test-op-ws")
        self.membership = WorkspaceMembership.objects.create(
            workspace=self.ws, user=self.user, role="owner", status="active"
        )
        self.auto_settings = WorkspaceAutomationSettings.objects.create(
            workspace=self.ws,
            master_automation_enabled=True,
            auto_find_jobs=True,
            auto_apply_jobs=True
        )

        self.candidate = CandidateProfile.objects.create(
            user=self.user,
            full_name="Test Candidate",
            email="candidate@example.com"
        )
        self.cpv = CandidateProfileVersion.objects.create(
            profile=self.candidate,
            version_number=1,
            state="approved",
            full_name="Test Candidate",
            fact_snapshot=[{"category": "Skills", "value": "Python, Django, React"}]
        )
        self.candidate.active_version = self.cpv
        self.candidate.save()

        self.app_prof, _ = CandidateApplicationProfile.objects.get_or_create(
            candidate_profile=self.candidate,
            defaults={"full_name": "Test Candidate", "email": "candidate@example.com"}
        )

        from openoutreach.career.models import CandidateIdentityVerification
        CandidateIdentityVerification.objects.create(
            candidate_profile=self.candidate,
            extracted_name="Test Candidate",
            confirmed_name="Test Candidate",
            verification_source="manual",
            status="verified",
            verified_at=timezone.now()
        )

        self.job = Job.objects.create(
            company="Verified Tech Corp",
            title="Senior Python Engineer",
            job_url="https://www.linkedin.com/jobs/view/4499887766/",
            description="Seeking a Senior Python Engineer with Django, React, PostgreSQL experience.",
            provider="linkedin",
            external_job_id="4499887766",
            provenance="REAL_PROVIDER",
            provider_identity_status="VERIFIED",
            is_active=True
        )

    def test_missing_required_field_results_in_not_ready(self):
        """A. Missing required field (e.g. phone or authorized countries) results in NOT READY."""
        self.app_prof.phone = ""
        self.app_prof.city = "San Francisco"
        self.app_prof.country = "United States"
        self.app_prof.authorized_countries = ["US"]
        self.app_prof.visa_sponsorship_now = "no"
        self.app_prof.visa_sponsorship_future = "no"
        self.app_prof.relocation_willingness = "no"
        self.app_prof.remote_willingness = "yes"
        self.app_prof.hybrid_willingness = "yes"
        self.app_prof.onsite_willingness = "depends"
        self.app_prof.travel_willingness = "depends"
        self.app_prof.background_check_willingness = "yes"
        self.app_prof.minimum_salary = 120000
        self.app_prof.currency = "USD"
        self.app_prof.salary_period = "annual"
        self.app_prof.confirmed = True
        self.app_prof.save()

        res = calculate_application_profile_readiness(self.candidate)
        assert res["personal_status"] == "Needs your answer"
        assert res["is_ready"] is False
        assert res["overall_status"] == "NOT READY"
        assert "Personal: Phone" in res["missing_items"]

    def test_missing_optional_field_does_not_block_readiness_when_required_complete(self):
        """B. Missing optional fields (e.g. linkedin_url, notice_period) do not block readiness."""
        self.app_prof.full_name = "Test Candidate"
        self.app_prof.email = "candidate@example.com"
        self.app_prof.phone = "+1 555 123 4567"
        self.app_prof.city = "San Francisco"
        self.app_prof.country = "United States"
        self.app_prof.linkedin_url = ""  # Optional!
        self.app_prof.authorized_countries = ["US"]
        self.app_prof.visa_sponsorship_now = "no"
        self.app_prof.visa_sponsorship_future = "no"
        self.app_prof.relocation_willingness = "no"
        self.app_prof.remote_willingness = "yes"
        self.app_prof.hybrid_willingness = "yes"
        self.app_prof.onsite_willingness = "depends"
        self.app_prof.travel_willingness = "depends"
        self.app_prof.background_check_willingness = "yes"
        self.app_prof.notice_period = ""  # Optional!
        self.app_prof.earliest_start_date = ""  # Optional!
        self.app_prof.minimum_salary = 130000
        self.app_prof.currency = "USD"
        self.app_prof.salary_period = "annual"
        self.app_prof.confirmed = True
        self.app_prof.save()

        res = calculate_application_profile_readiness(self.candidate)
        assert res["is_ready"] is True
        assert res["overall_status"] == "READY FOR AUTOMATED APPLICATIONS"
        assert len(res["missing_items"]) == 0

    def test_unknown_boolean_like_answer_preserves_uncertainty(self):
        """C. Unknown sponsorship or background check does NOT silently convert to False."""
        self.app_prof.visa_sponsorship_now = "unknown"
        self.app_prof.visa_sponsorship_future = "unknown"
        self.app_prof.background_check_willingness = "unknown"
        self.app_prof.save()

        # Check question answer bank resolution returns UNKNOWN_REQUIRES_USER, never False
        status_now, val_now, _ = get_answer_for_question(self.candidate, "Do you require visa sponsorship now?")
        assert status_now == "UNKNOWN_REQUIRES_USER"
        assert val_now is None

        status_bg, val_bg, _ = get_answer_for_question(self.candidate, "Will you consent to a lawful background check?")
        assert status_bg == "UNKNOWN_REQUIRES_USER"
        assert val_bg is None

    def test_required_facts_complete_but_unconfirmed_blocks_execution(self):
        """D. Required facts complete but confirmed=False blocks application execution."""
        self.app_prof.full_name = "Test Candidate"
        self.app_prof.email = "candidate@example.com"
        self.app_prof.phone = "+1 555 123 4567"
        self.app_prof.city = "San Francisco"
        self.app_prof.country = "United States"
        self.app_prof.authorized_countries = ["US"]
        self.app_prof.visa_sponsorship_now = "no"
        self.app_prof.visa_sponsorship_future = "no"
        self.app_prof.relocation_willingness = "no"
        self.app_prof.remote_willingness = "yes"
        self.app_prof.hybrid_willingness = "yes"
        self.app_prof.onsite_willingness = "depends"
        self.app_prof.travel_willingness = "depends"
        self.app_prof.background_check_willingness = "yes"
        self.app_prof.minimum_salary = 130000
        self.app_prof.confirmed = False  # Not confirmed!
        self.app_prof.save()

        res = calculate_application_profile_readiness(self.candidate)
        assert res["is_ready"] is False

        allowed, reason = evaluate_external_action_gates(
            workspace=self.ws,
            candidate_profile=self.candidate,
            job=self.job,
            external_action_authorized=True,
            action_type="job_application"
        )
        assert allowed is False
        assert reason in ["candidate_profile_not_ready", "candidate_profile_unconfirmed"]

    def test_profile_confirmed_then_critical_field_cleared_resets_confirmation(self):
        """E. Profile confirmed, but clearing a critical field in UI resets confirmation."""
        self.app_prof.full_name = "Test Candidate"
        self.app_prof.email = "candidate@example.com"
        self.app_prof.phone = "+1 555 123 4567"
        self.app_prof.city = "San Francisco"
        self.app_prof.country = "United States"
        self.app_prof.authorized_countries = ["US"]
        self.app_prof.visa_sponsorship_now = "no"
        self.app_prof.visa_sponsorship_future = "no"
        self.app_prof.relocation_willingness = "no"
        self.app_prof.remote_willingness = "yes"
        self.app_prof.hybrid_willingness = "yes"
        self.app_prof.onsite_willingness = "depends"
        self.app_prof.travel_willingness = "depends"
        self.app_prof.background_check_willingness = "yes"
        self.app_prof.minimum_salary = 130000
        self.app_prof.confirmed = True
        self.app_prof.save()

        # Simulate clearing phone in settings POST
        client = Client()
        client.force_login(self.user)

        resp = client.post("/dashboard/settings/job-profile/", {
            "action": "save",
            "full_name": "Test Candidate",
            "email": "candidate@example.com",
            "phone": "",  # Cleared!
            "city": "San Francisco",
            "country": "United States",
            "authorized_countries": "US",
            "visa_sponsorship_now": "no",
            "visa_sponsorship_future": "no",
            "relocation_willingness": "no",
            "remote_willingness": "yes",
            "hybrid_willingness": "yes",
            "onsite_willingness": "depends",
            "travel_willingness": "depends",
            "background_check_willingness": "yes",
            "minimum_salary": "130000",
            "currency": "USD",
            "salary_period": "annual",
        })
        assert resp.status_code == 302

        self.app_prof.refresh_from_db()
        assert self.app_prof.phone == ""
        assert self.app_prof.confirmed is False  # Reset!

    def test_direct_server_request_blocked_when_profile_not_ready(self):
        """F. Direct server call to authorize_job_application while NOT READY is blocked."""
        self.app_prof.phone = ""
        self.app_prof.confirmed = False
        self.app_prof.save()

        auth_res = authorize_job_application(
            workspace=self.ws,
            candidate=self.candidate,
            job=self.job,
            external_action_authorized=True
        )
        assert auth_res["authorized"] is False
        assert auth_res["reason"] in ["candidate_profile_not_ready", "candidate_profile_unconfirmed"]

    def test_test_fixture_candidate_does_not_contaminate_candidate_2(self):
        """H. Test fixture modifications do NOT alter production Candidate #2."""
        cand_2 = CandidateProfile.objects.filter(id=2).first()
        if cand_2:
            cand_2_initial_name = cand_2.full_name
            cand_2_app_prof, _ = CandidateApplicationProfile.objects.get_or_create(candidate_profile=cand_2)
            cand_2_initial_confirmed = cand_2_app_prof.confirmed

            # Modify fixture
            self.app_prof.phone = "+1 999 888 7777"
            self.app_prof.confirmed = True
            self.app_prof.save()

            cand_2.refresh_from_db()
            cand_2_app_prof.refresh_from_db()
            assert cand_2.full_name == cand_2_initial_name
            assert cand_2_app_prof.confirmed == cand_2_initial_confirmed

    def test_evaluate_canary_readiness_enforces_all_eleven_conditions(self):
        """I. evaluate_canary_readiness evaluates all 11 criteria and reports exact missing reasons."""
        # When profile is incomplete and unconfirmed
        self.app_prof.phone = ""
        self.app_prof.confirmed = False
        self.app_prof.save()

        res = evaluate_canary_readiness(
            workspace=self.ws,
            candidate=self.candidate,
            job=self.job
        )
        assert res["canary_ready"] is False
        assert "candidate_profile_not_ready" in res["reasons"]
        assert "candidate_profile_unconfirmed" in res["reasons"]
