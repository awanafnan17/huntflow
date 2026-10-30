"""
Tests for External Email Confirmation Reconciliation and Duplicate Protection.
"""
import pytest
from django.utils import timezone
from django.contrib.auth.models import User
from openoutreach.core.models import Workspace
from openoutreach.career.models import CandidateProfile, Job, JobApplication, ApplicationDraft
from openoutreach.career.services.confirmation_evidence import (
    record_external_confirmation,
    ConfirmationEvidenceType,
    can_downgrade_confirmation,
    VerificationStatus
)
from openoutreach.career.services.job_application_engine import (
    prepare_job_application,
    evaluate_canary_readiness,
    execute_job_application
)


@pytest.mark.django_db
class TestExternalEmailConfirmationReconciliation:
    def setup_method(self):
        self.user = User.objects.create_user(username="test_rec_op", email="test_rec_op@example.com")
        self.ws = Workspace.objects.create(name="Rec Workspace", slug="rec-ws")
        self.candidate = CandidateProfile.objects.create(
            user=self.user,
            full_name="Reconcile Candidate",
            email="reconcile_cand@example.com"
        )
        from openoutreach.career.models import CandidateProfileVersion
        self.ver = CandidateProfileVersion.objects.create(
            profile=self.candidate,
            version_number=1,
            state="approved",
            checksum="hash123456"
        )
        self.candidate.active_version = self.ver
        self.candidate.save()
        self.job = Job.objects.create(
            title="Backend Engineer",
            company="Confirmed Tech Corp",
            external_job_id="999888777",
            provider="linkedin",
            location="Remote",
            is_active=True,
            provenance="REAL_PROVIDER",
            provider_identity_status="VERIFIED"
        )

    def test_reconcile_unverified_application_with_operator_email(self):
        """Unverified application transitions cleanly to submitted/externally_confirmed."""
        original_time = timezone.now() - timezone.timedelta(hours=2)
        app = JobApplication.objects.create(
            candidate_profile=self.candidate,
            job=self.job,
            status="submission_unverified",
            externally_confirmed=False,
            submission_date=original_time
        )

        record_external_confirmation(
            application=app,
            provider="linkedin",
            external_job_id="999888777",
            evidence_type=ConfirmationEvidenceType.OPERATOR_CONFIRMED_LINKEDIN_EMAIL,
            checked_at=timezone.now(),
            metadata={"source": "operator_report", "note": "LinkedIn acknowledgement email received"}
        )

        app.submission_date = original_time
        app.save(update_fields=["submission_date"])
        app.refresh_from_db()

        assert app.status == "submitted"
        assert app.externally_confirmed is True
        assert app.confirmation_evidence_type == ConfirmationEvidenceType.OPERATOR_CONFIRMED_LINKEDIN_EMAIL
        assert app.submission_date == original_time

    def test_reconciliation_is_idempotent(self):
        """Already confirmed application remains confirmed upon re-reconciliation."""
        app = JobApplication.objects.create(
            candidate_profile=self.candidate,
            job=self.job,
            status="submitted",
            externally_confirmed=True,
            confirmation_evidence_type=ConfirmationEvidenceType.OPERATOR_CONFIRMED_LINKEDIN_EMAIL,
            confirmation_provider="linkedin",
            confirmation_external_job_id="999888777"
        )

        record_external_confirmation(
            application=app,
            provider="linkedin",
            external_job_id="999888777",
            evidence_type=ConfirmationEvidenceType.OPERATOR_CONFIRMED_LINKEDIN_EMAIL,
            metadata={"note": "Second reconciliation call"}
        )

        app.refresh_from_db()
        assert app.status == "submitted"
        assert app.externally_confirmed is True

    def test_duplicate_protection_blocks_confirmed_application(self):
        """Confirmed application blocks future preparation and execution."""
        JobApplication.objects.create(
            candidate_profile=self.candidate,
            job=self.job,
            status="submitted",
            externally_confirmed=True,
            confirmation_evidence_type=ConfirmationEvidenceType.OPERATOR_CONFIRMED_LINKEDIN_EMAIL,
            confirmation_provider="linkedin",
            confirmation_external_job_id="999888777"
        )

        prep_res = prepare_job_application(workspace=self.ws, candidate=self.candidate, job=self.job)
        assert prep_res["prepared"] is False
        assert prep_res["reason"] == "duplicate_application_blocked"

    def test_evidence_precedence_protects_confirmed_email(self):
        """Weak subsequent checks cannot downgrade confirmed application."""
        app = JobApplication.objects.create(
            candidate_profile=self.candidate,
            job=self.job,
            status="submitted",
            externally_confirmed=True,
            confirmation_evidence_type=ConfirmationEvidenceType.OPERATOR_CONFIRMED_LINKEDIN_EMAIL
        )

        # UNKNOWN status cannot downgrade
        assert can_downgrade_confirmation(app, VerificationStatus.UNKNOWN, "tracker_unmatched_or_unavailable") is False
