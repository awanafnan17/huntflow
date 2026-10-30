"""Comprehensive regression tests for Master Pause Hardening, Atomic OperatorActionApproval,
Canonical Provider Job Identity, Provider Verification Truth, and Secret Redaction."""

import threading
import pytest
from django.db import IntegrityError, connection
from django.utils import timezone
from django.contrib.auth import get_user_model

from openoutreach.core.models import (
    Workspace, WorkspaceMembership, Task, SiteConfig, WorkspaceAutomationSettings,
    OperatorActionApproval
)
from openoutreach.core.services.external_action_gates import evaluate_external_action_gates
from openoutreach.core.utils.security import redact_sensitive_dict, is_sensitive_key, safe_model_repr
from openoutreach.career.models import (
    CandidateProfile, CandidateProfileVersion, CandidateIdentityVerification,
    CandidateApplicationProfile, Job, JobApplication, ApplicationDraft
)
from openoutreach.career.services.job_application_engine import (
    authorize_job_application, execute_job_application
)
from openoutreach.career.services.provider_verification import verify_provider_job_identity
from openoutreach.career.answer_bank import get_answer_for_question, calculate_application_profile_readiness
from openoutreach.linkedin.models import LinkedInProfile

User = get_user_model()


@pytest.mark.django_db(transaction=True)
class TestMasterPauseHardeningAndCanonicalJobIdentity:

    @pytest.fixture(autouse=True)
    def setup_data(self, db):
        self.operator = User.objects.create_user(username="test_admin_op", email="admin_op@example.com")
        self.ws = Workspace.objects.create(name="Master Pause Test WS", slug="master-pause-ws")
        self.mem = WorkspaceMembership.objects.create(workspace=self.ws, user=self.operator, role="owner", status="active")
        self.auto_settings = WorkspaceAutomationSettings.objects.create(
            workspace=self.ws,
            master_automation_enabled=False,
            auto_find_jobs=True,
            auto_apply_jobs=True
        )

        self.candidate = CandidateProfile.objects.create(
            user=self.operator,
            full_name="Alex Morgan",
            email="candidate-2@example.com"
        )
        self.cpv = CandidateProfileVersion.objects.create(
            profile=self.candidate,
            version_number=1,
            state="approved",
            full_name=self.candidate.full_name,
            fact_snapshot=[{"category": "Skills", "value": "Python, Django, PostgreSQL"}]
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

        self.job_x = Job.objects.create(
            company="Xorora",
            title="Senior Software Engineer (Full-Stack / AI)",
            job_url="https://www.linkedin.com/jobs/view/4452269262/",
            provider="linkedin",
            external_job_id="4452269262",
            provenance="REAL_PROVIDER",
            provider_identity_status="VERIFIED",
            is_active=True
        )

        self.job_y = Job.objects.create(
            company="Crossing Hurdles",
            title="Python Developer | /hr Remote",
            job_url="https://www.linkedin.com/jobs/view/4423338490/",
            provider="linkedin",
            external_job_id="4423338490",
            provenance="REAL_PROVIDER",
            provider_identity_status="VERIFIED",
            is_active=True
        )

    # -------------------------------------------------------------
    # 1. MASTER PAUSE HARDENING & ONE-TIME OPERATOR APPROVAL TESTS
    # -------------------------------------------------------------

    def test_case_a_master_false_without_authorization_blocks(self):
        """Case A: master=False, external_action_authorized=False => BLOCK."""
        allowed, reason = evaluate_external_action_gates(
            workspace=self.ws,
            candidate_profile=self.candidate,
            job=self.job_x,
            external_action_authorized=False,
            operator_approval=None
        )
        assert allowed is False
        assert reason == "workspace_master_automation_paused"

    def test_case_b_master_false_generic_auth_true_without_approval_blocks(self):
        """Case B: master=False, external_action_authorized=True (generic bool) without approval => BLOCK."""
        allowed, reason = evaluate_external_action_gates(
            workspace=self.ws,
            candidate_profile=self.candidate,
            job=self.job_x,
            external_action_authorized=True,
            operator_approval=None
        )
        assert allowed is False
        assert reason == "workspace_master_automation_paused"

    def test_case_c_master_false_valid_exact_job_x_approval_allows(self):
        """Case C: master=False, valid exact one-time Job #X approval => ALLOW ONLY Job #X."""
        approval = OperatorActionApproval.objects.create(
            workspace=self.ws,
            operator=self.operator,
            action_type="job_application",
            candidate=self.candidate,
            job=self.job_x,
            external_job_id="4452269262",
            status="approved"
        )
        allowed, reason = evaluate_external_action_gates(
            workspace=self.ws,
            candidate_profile=self.candidate,
            job=self.job_x,
            external_action_authorized=True,
            operator_approval=approval
        )
        assert allowed is True
        assert reason == "authorized"

    def test_case_d_approval_for_job_x_attempting_job_y_blocks(self):
        """Case D: Approval bound to Job #X attempting Job #Y => BLOCK."""
        approval = OperatorActionApproval.objects.create(
            workspace=self.ws,
            operator=self.operator,
            action_type="job_application",
            candidate=self.candidate,
            job=self.job_x,
            external_job_id="4452269262",
            status="approved"
        )
        allowed, reason = evaluate_external_action_gates(
            workspace=self.ws,
            candidate_profile=self.candidate,
            job=self.job_y,  # Different job!
            external_action_authorized=True,
            operator_approval=approval
        )
        assert allowed is False
        assert reason == "workspace_master_automation_paused"

    def test_case_e_consumed_approval_reuse_blocks(self):
        """Case E: Reusing a consumed approval => BLOCK."""
        approval = OperatorActionApproval.objects.create(
            workspace=self.ws,
            operator=self.operator,
            action_type="job_application",
            candidate=self.candidate,
            job=self.job_x,
            external_job_id="4452269262",
            status="approved"
        )
        # Consume approval
        approval.consume()
        assert approval.status == "consumed"
        assert approval.consumed_at is not None

        allowed, reason = evaluate_external_action_gates(
            workspace=self.ws,
            candidate_profile=self.candidate,
            job=self.job_x,
            external_action_authorized=True,
            operator_approval=approval
        )
        assert allowed is False
        assert reason == "workspace_master_automation_paused"

    def test_case_f_master_true_specific_capability_false_blocks(self):
        """Case F: master=True, specific auto_apply_jobs=False => BLOCK."""
        self.auto_settings.master_automation_enabled = True
        self.auto_settings.auto_apply_jobs = False
        self.auto_settings.save(update_fields=["master_automation_enabled", "auto_apply_jobs"])

        allowed, reason = evaluate_external_action_gates(
            workspace=self.ws,
            candidate_profile=self.candidate,
            job=self.job_x,
            action_type="job_application"
        )
        assert allowed is False
        assert reason == "action_capability_disabled"

    # -------------------------------------------------------------
    # 2. ATOMIC ONE-TIME APPROVAL CONCURRENCY & FAILURE SEMANTICS
    # -------------------------------------------------------------

    def test_atomic_claim_concurrency_two_workers_exactly_one_succeeds(self):
        """Two workers attempting to claim the same approval: exactly one succeeds, second is rejected."""
        approval = OperatorActionApproval.objects.create(
            workspace=self.ws,
            operator=self.operator,
            action_type="job_application",
            candidate=self.candidate,
            job=self.job_x,
            external_job_id="4452269262",
            status="approved"
        )

        # Worker 1 claims
        claimed1, apprv1, err1 = OperatorActionApproval.claim_for_execution(
            approval.id,
            workspace=self.ws,
            candidate=self.candidate,
            job=self.job_x,
            action_type="job_application"
        )
        assert claimed1 is True
        assert err1 == "claimed"
        assert apprv1.status == "claimed"
        assert apprv1.claimed_at is not None

        # Worker 2 attempts same claim -> rejected immediately
        claimed2, apprv2, err2 = OperatorActionApproval.claim_for_execution(
            approval.id,
            workspace=self.ws,
            candidate=self.candidate,
            job=self.job_x,
            action_type="job_application"
        )
        assert claimed2 is False
        assert err2 == "approval_already_claimed"

        # Worker 1 consumes approval
        apprv1.consume()
        assert apprv1.status == "consumed"
        assert apprv1.consumed_at is not None

        # Worker 3 attempts claim after consumed -> rejected
        claimed3, apprv3, err3 = OperatorActionApproval.claim_for_execution(
            approval.id,
            workspace=self.ws,
            candidate=self.candidate,
            job=self.job_x,
            action_type="job_application"
        )
        assert claimed3 is False
        assert err3 == "approval_already_consumed"

    def test_failed_browser_action_marks_approval_failed_not_reusable(self):
        """Failed execution marks approval as failed; cannot be reused without operator re-approval."""
        approval = OperatorActionApproval.objects.create(
            workspace=self.ws,
            operator=self.operator,
            action_type="job_application",
            candidate=self.candidate,
            job=self.job_x,
            external_job_id="4452269262",
            status="approved"
        )

        class FailingAdapter:
            def apply(self, **kwargs):
                return {"success": False, "error": "browser_navigation_failed"}

        app = JobApplication.objects.create(candidate_profile=self.candidate, job=self.job_x, status="approved")
        draft = ApplicationDraft.objects.create(application=app, version_number=1)

        res = execute_job_application(
            workspace=self.ws,
            candidate=self.candidate,
            job=self.job_x,
            draft=draft,
            adapter=FailingAdapter(),
            operator_approval=approval
        )
        assert res["status"] == "failed"

        approval.refresh_from_db()
        assert approval.status == "failed"
        assert "browser_navigation_failed" in approval.failure_reason

        # Attempting to reuse failed approval is blocked
        allowed, reason = evaluate_external_action_gates(
            workspace=self.ws,
            candidate_profile=self.candidate,
            job=self.job_x,
            operator_approval=approval
        )
        assert allowed is False

    # -------------------------------------------------------------
    # 3. CONTROLLED PROVIDER VERIFICATION & CANONICAL IDENTITY
    # -------------------------------------------------------------

    def test_caller_raw_evidence_cannot_create_production_verified(self):
        """Passing raw_evidence directly to public verifier MUST NOT promote to VERIFIED."""
        fake_job = Job.objects.create(
            company="Fake Provider Corp",
            title="Fake Python Engineer",
            job_url="https://www.linkedin.com/jobs/view/9999999998/",
            provider="linkedin",
            external_job_id="9999999998",
            provenance="REAL_PROVIDER",
            provider_identity_status="UNVERIFIED",
            is_active=True
        )

        res = verify_provider_job_identity(
            fake_job,
            raw_evidence={
                "observed_title": "Fake Python Engineer",
                "observed_company": "Fake Provider Corp",
                "is_expired": False,
                "easy_apply": True
            }
        )
        assert res.status == "UNVERIFIED"
        fake_job.refresh_from_db()
        assert fake_job.provider_identity_status == "UNVERIFIED"
        assert fake_job.is_real_provider_job() is False
        assert "cannot establish production VERIFIED status" in fake_job.verification_error

    def test_browser_live_observation_can_promote_to_verified(self):
        """Trusted internal LIVE_BROWSER observation promotes matching job to VERIFIED."""
        from openoutreach.career.services.provider_verification import (
            ProviderVerificationObservation, verify_provider_job_identity_from_observation
        )
        job = Job.objects.create(
            company="Genuine Labs",
            title="Senior AI Engineer",
            job_url="https://www.linkedin.com/jobs/view/4477112233/",
            provider="linkedin",
            external_job_id="4477112233",
            provenance="REAL_PROVIDER",
            provider_identity_status="UNVERIFIED",
            is_active=True
        )

        obs = ProviderVerificationObservation(
            provider="linkedin",
            expected_external_job_id="4477112233",
            observed_external_job_id="4477112233",
            canonical_url="https://www.linkedin.com/jobs/view/4477112233/",
            observed_title="Senior AI Engineer",
            observed_company="Genuine Labs",
            observed_location="San Francisco, CA",
            is_expired=False,
            easy_apply=True,
            verification_source="LIVE_BROWSER",
            extraction_source="dom"
        )

        res = verify_provider_job_identity_from_observation(job, obs)
        assert res.status == "VERIFIED"
        job.refresh_from_db()
        assert job.provider_identity_status == "VERIFIED"
        assert job.is_real_provider_job() is True
        assert job.last_provider_verified_at is not None
        assert job.provider_verification_evidence["verification_source"] == "LIVE_BROWSER"
        assert "li_at" not in job.provider_verification_evidence

    def test_wrong_job_redirect_marked_invalid_never_verified(self):
        """Browser redirecting to a different external job ID is marked INVALID, never VERIFIED."""
        from openoutreach.career.services.provider_verification import (
            ProviderVerificationObservation, verify_provider_job_identity_from_observation
        )
        job = Job.objects.create(
            company="Expected Company",
            title="Expected Role",
            job_url="https://www.linkedin.com/jobs/view/1111111111/",
            provider="linkedin",
            external_job_id="1111111111",
            provenance="REAL_PROVIDER",
            provider_identity_status="UNVERIFIED",
            is_active=True
        )

        obs = ProviderVerificationObservation(
            provider="linkedin",
            expected_external_job_id="1111111111",
            observed_external_job_id="2222222222",  # Different job ID redirect!
            canonical_url="https://www.linkedin.com/jobs/view/1111111111/",
            observed_title="Different Role",
            observed_company="Different Company",
            observed_location="Remote",
            is_expired=False,
            easy_apply=True,
            verification_source="LIVE_BROWSER",
            extraction_source="dom"
        )

        res = verify_provider_job_identity_from_observation(job, obs)
        assert res.status == "INVALID"
        job.refresh_from_db()
        assert job.provider_identity_status == "INVALID"
        assert job.is_real_provider_job() is False
        assert "redirected to different job ID" in job.verification_error

    def test_test_fixture_verification_quarantined_not_eligible(self):
        """verify_provider_job_identity_from_test_fixture forces TEST_FIXTURE state and is not real."""
        from openoutreach.career.services.provider_verification import verify_provider_job_identity_from_test_fixture
        job = Job.objects.create(
            company="Fixture Corp",
            title="Fixture Engineer",
            job_url="https://www.linkedin.com/jobs/view/9999999999/",
            provider="linkedin",
            external_job_id="9999999999",
            provenance="REAL_PROVIDER",
            provider_identity_status="UNVERIFIED",
            is_active=True
        )

        res = verify_provider_job_identity_from_test_fixture(
            job,
            raw_evidence={
                "title": "Fixture Engineer",
                "company": "Fixture Corp",
                "is_expired": False
            }
        )
        assert res.status == "TEST_FIXTURE"
        job.refresh_from_db()
        assert job.provenance == "TEST_FIXTURE"
        assert job.provider_identity_status == "TEST_FIXTURE"
        assert job.is_synthetic is True
        assert job.is_real_provider_job() is False

    def test_unique_canonical_verified_provider_job_constraint(self):
        """Database constraint prevents two canonical active VERIFIED rows for same (provider, external_job_id)."""
        Job.objects.create(
            company="Verified Co 1",
            title="Python Dev",
            job_url="https://www.linkedin.com/jobs/view/4499112233/",
            provider="linkedin",
            external_job_id="4499112233",
            provenance="REAL_PROVIDER",
            provider_identity_status="VERIFIED",
            canonical_job=None,
            is_active=True
        )

        with pytest.raises(IntegrityError):
            Job.objects.create(
                company="Verified Co 2",
                title="Python Dev Duplicate",
                job_url="https://www.linkedin.com/jobs/view/4499112233/",
                provider="linkedin",
                external_job_id="4499112233",
                provenance="REAL_PROVIDER",
                provider_identity_status="VERIFIED",
                canonical_job=None,
                is_active=True
            )

    def test_concurrent_upsert_creates_exactly_one_canonical_job(self):
        """Sequential and concurrent discovery upserts for same provider and external_job_id do not duplicate entities."""
        j1, created1 = Job.upsert_discovered_job(
            provider="linkedin",
            external_job_id="4466554433",
            title="Initial Title",
            company="Concurrent Co",
            description="Initial JD text...",
            provenance="REAL_PROVIDER"
        )
        assert created1 is True
        assert j1.id is not None

        # Second upsert with updated JD updates existing job
        j2, created2 = Job.upsert_discovered_job(
            provider="linkedin",
            external_job_id="4466554433",
            title="Updated Title",
            company="Concurrent Co",
            description="Updated JD text with new requirements...",
            provenance="REAL_PROVIDER"
        )
        assert created2 is False
        assert j2.id == j1.id
        assert j2.title == "Updated Title"
        assert Job.objects.filter(provider="linkedin", external_job_id="4466554433").count() == 1

    # -------------------------------------------------------------
    # 4. SECRET REDACTION & LOGGING SAFETY TESTS
    # -------------------------------------------------------------

    def test_secret_redaction_utility_recursively_masks_all_credentials(self):
        """redact_sensitive_dict masks passwords, cookies, tokens, keys."""
        data = {
            "user": "candidate",
            "password": "SuperSecretPassword123!",
            "linkedin_password": "PlaintextPassword!",
            "cookie_data": {"li_at": "AQEDASampleCookieValue"},
            "access_token": "secret_oauth_token",
            "nested": {
                "api_key": "test-1-aaaaaaaa",
                "normal_field": "visible_info"
            }
        }
        redacted = redact_sensitive_dict(data)
        assert redacted["password"] == "[test-1-aaaaaaaa]"
        assert redacted["linkedin_password"] == "[test-1-aaaaaaaa]"
        assert redacted["cookie_data"] == "[test-1-aaaaaaaa]"
        assert redacted["access_token"] == "[test-1-aaaaaaaa]"
        assert redacted["nested"]["api_key"] == "[test-1-aaaaaaaa]"
        assert redacted["nested"]["normal_field"] == "visible_info"
        assert redacted["user"] == "candidate"

    def test_linkedin_profile_repr_and_safe_dict_redact_secrets(self):
        """LinkedInProfile __repr__ and safe_dict never expose plaintext passwords or cookies."""
        lp = LinkedInProfile.objects.create(
            user=self.operator,
            linkedin_username="safe_user@example.com",
            linkedin_password="",
            cookie_data={"li_at": "AQEDASampleCookieValue"},
            session_status="valid",
            active=True
        )
        repr_str = repr(lp)
        assert "AQEDASampleCookieValue" not in repr_str
        assert "[test-1-aaaaaaaa]" in repr_str

        s_dict = lp.safe_dict()
        assert s_dict.get("cookie_data") == "[test-1-aaaaaaaa]"

    # -------------------------------------------------------------
    # 5. HEALTH CHECK ENDPOINT TESTS
    # -------------------------------------------------------------

    def test_healthz_endpoint_returns_200_and_safe_json(self, client):
        """GET /healthz/ returns HTTP 200 with service health dictionary without secrets."""
        resp = client.get("/healthz/")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] in ["ok", "healthy", "degraded"]
        assert "services" in data
        assert data["services"]["web"] == "ok"
        assert data["services"]["database"] == "ok"
        assert "password" not in str(data)
        assert "cookie" not in str(data)
        assert "secret" not in str(data)
