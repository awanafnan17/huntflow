import pytest
import io
import uuid
from django.core.management import call_command
from django.core.management.base import CommandError
from django.core.exceptions import ValidationError
from tests.factories import UserFactory
from unittest.mock import patch

from openoutreach.career.models import (
    CandidateProfile,
    CandidateProfileVersion,
    CandidateDocument,
    CandidateDocumentVersion,
    CandidateExtractionRecord,
    CandidateIdentityVerification,
    CandidateAuditEvent,
    CandidateProfileApproval,
    CandidateFact,
    IncidentHold,
    IncidentHoldTarget,
    Job,
    JobVersion,
    JobMatch,
    JobApplication,
    ApplicationDraft
)
from openoutreach.career.service import approve_corrected_profile_version

@pytest.mark.django_db
class TestIdentityCorrectedVersionApproval:
    @pytest.fixture(autouse=True)
    def _setup(self, db):
        self.patcher = patch("openoutreach.career.service.extract_text_from_file_version", return_value="Alex Morgan\n+1-202-555-0100 | candidate-2@example.com")
        self.mock_extract = self.patcher.start()
        
        self.user = UserFactory(username="seeker_user", email="candidate-10@example.com")
        self.other_user = UserFactory(username="other_user", email="other@example.com")
        
        # Candidate Profile
        self.cand_profile = CandidateProfile.objects.create(
            user=self.user,
            full_name="idwsdsjtasdkk",
            email="candidate-10@example.com"
        )
        
        # Version 3 (Approved, NOT YET finalized to allow facts creation)
        self.profile_version_v3 = CandidateProfileVersion.objects.create(
            profile=self.cand_profile,
            version_number=3,
            full_name="idwsdsjtasdkk",
            email="candidate-10@example.com",
            created_by=self.user,
            is_finalized=False,
            state="draft",
            checksum="9a216e1c969a3d9b3859a30bec41cba249d7fc856af6087f6a77631526e4c8e3"
        )
        
        # Candidate Facts for v3
        self.fact1 = CandidateFact.objects.create(
            profile_version=self.profile_version_v3,
            fact_type="skill",
            category="programming",
            value="Python"
        )
        self.fact2 = CandidateFact.objects.create(
            profile_version=self.profile_version_v3,
            fact_type="experience",
            category="role",
            value="Software Engineer"
        )
        
        # Now finalize Version 3
        from openoutreach.career.canonical import compute_canonical_checksum
        self.profile_version_v3.checksum = compute_canonical_checksum(list(self.profile_version_v3.facts.all()))
        self.profile_version_v3.is_finalized = True
        self.profile_version_v3.state = "approved"
        self.profile_version_v3.save()
        
        self.cand_profile.active_version = self.profile_version_v3
        self.cand_profile.save()
        
        # Version 4 (Corrected, pending, new identity)
        self.profile_version_v4 = CandidateProfileVersion.objects.create(
            profile=self.cand_profile,
            version_number=4,
            full_name="Alex Morgan",
            email="candidate-10@example.com",
            created_by=self.user,
            is_finalized=False,
            state="pending_approval",
            checksum="correctedchecksum123"
        )
        
        # Candidate Facts copied to v4
        CandidateFact.objects.create(
            profile_version=self.profile_version_v4,
            fact_type="skill",
            category="programming",
            value="Python"
        )
        CandidateFact.objects.create(
            profile_version=self.profile_version_v4,
            fact_type="experience",
            category="role",
            value="Software Engineer"
        )

        # Document and extraction setup
        self.doc = CandidateDocument.objects.create(profile=self.cand_profile, name="cv.pdf")
        self.doc_ver = CandidateDocumentVersion.objects.create(
            document=self.doc,
            version_number=1,
            file_hash="c84dd31de3ba8dbb9b1ae3fcf1a40f0da0f6ce9664550855e42c4a78255547c3",
            file_size=1024,
            mime_type="application/pdf",
            original_filename="cv.pdf"
        )
        self.extraction = CandidateExtractionRecord.objects.create(
            profile=self.cand_profile,
            document_version=self.doc_ver,
            status="pending",
            raw_text="Alex Morgan\ncandidate-2@example.com",
            raw_text_hash="texthash123"
        )

        from openoutreach.career.canonical import compute_canonical_checksum
        from openoutreach.career.models import CandidateIdentityCorrectionLedger
        import hashlib
        import json

        expected_hist_checksum = self.profile_version_v3.checksum

        expected_corr_checksum = compute_canonical_checksum(list(self.profile_version_v4.facts.all()))
        self.profile_version_v4.checksum = expected_corr_checksum
        self.profile_version_v4.save()

        payload = {
            "full_name": "Alex Morgan",
            "preserved_email": "candidate-10@example.com",
            "source_email": "candidate-2@example.com",
            "discrepancy_status": "resolved_use_profile_email",
            "historical_version_id": self.profile_version_v3.id,
            "source_document_version_id": self.doc_ver.id,
            "source_file_hash": self.doc_ver.file_hash,
            "extraction_record_id": self.extraction.id,
            "fact_snapshot": self.profile_version_v3.fact_snapshot,
            "request_fingerprint": "fingerprint123"
        }
        payload_str = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        corr_payload_checksum = hashlib.sha256(payload_str.encode("utf-8")).hexdigest()

        self.ledger = CandidateIdentityCorrectionLedger.objects.create(
            profile=self.cand_profile,
            corrected_version=self.profile_version_v4,
            historical_version=self.profile_version_v3,
            source_document_version=self.doc_ver,
            extraction_record=self.extraction,
            idempotency_key="key123",
            created_by=self.user,
            status="pending_review",
            source_file_hash=self.doc_ver.file_hash,
            source_text_checksum=self.extraction.raw_text_hash,
            normalized_source_identity="Alex Morgan",
            source_email="candidate-2@example.com",
            preserved_profile_email="candidate-10@example.com",
            contact_email_discrepancy_status="resolved_use_profile_email",
            historical_fact_checksum=expected_hist_checksum,
            corrected_fact_checksum=expected_corr_checksum,
            corrected_payload_checksum=corr_payload_checksum,
            request_fingerprint="fingerprint123"
        )

        # Verification Verification Record
        self.verification = CandidateIdentityVerification.objects.create(
            candidate_profile=self.cand_profile,
            status="verified",
            extracted_name="Alex Morgan",
            confirmed_name="Alex Morgan",
            verification_source="manual",
            verified_by=self.user
        )

        # Job and application artifacts (Quarantined)
        self.job = Job.objects.create(pk=1, title="Software Engineer", company="GenOO")
        self.job_version = JobVersion.objects.create(pk=1, job=self.job, version_number=1, title="Software Engineer", company="GenOO")
        self.job_match = JobMatch.objects.create(pk=1, candidate_profile_version=self.profile_version_v3, job_version=self.job_version, overall_score=85)
        self.job_app = JobApplication.objects.create(pk=1, candidate_profile=self.cand_profile, job=self.job)
        self.app_draft = ApplicationDraft.objects.create(
            application=self.job_app,
            version_number=1,
            tailored_cv="A CV for idwsdsjtasdkk",
            cover_letter="Cover letter for idwsdsjtasdkk"
        )
        
        # Active incident hold and targets
        self.hold = IncidentHold.objects.create(
            reason="IDENTITY_AND_SOURCE_INTEGRITY_INCIDENT",
            active=True
        )
        IncidentHoldTarget.objects.create(incident_hold=self.hold, content_object=self.job)
        IncidentHoldTarget.objects.create(incident_hold=self.hold, content_object=self.app_draft)

    def teardown_method(self, method):
        if hasattr(self, "patcher"):
            self.patcher.stop()

    def _get_snapshot(self):
        return {
            "CandidateProfileVersion": CandidateProfileVersion.objects.count(),
            "CandidateProfileApproval": CandidateProfileApproval.objects.count(),
            "CandidateFact": CandidateFact.objects.count(),
            "ApplicationDraft": ApplicationDraft.objects.count(),
            "IncidentHold": IncidentHold.objects.count(),
            "JobApplication": JobApplication.objects.count(),
        }

    def test_version_3_cannot_be_approved(self):
        with pytest.raises(ValidationError):
            approve_corrected_profile_version(self.profile_version_v3, self.user)

    def test_pending_version_can_be_approved(self):
        approval = approve_corrected_profile_version(self.profile_version_v4, self.user)
        assert approval is not None
        assert approval.profile_version == self.profile_version_v4

    def test_approval_creates_single_record(self):
        start_count = CandidateProfileApproval.objects.count()
        approve_corrected_profile_version(self.profile_version_v4, self.user)
        assert CandidateProfileApproval.objects.count() == start_count + 1

    def test_same_idempotency_key_returns_existing_approval(self):
        ik = "my_idempotency_key"
        app1 = approve_corrected_profile_version(self.profile_version_v4, self.user, idempotency_key=ik)
        
        # Refresh object and re-request
        self.profile_version_v4.refresh_from_db()
        app2 = approve_corrected_profile_version(self.profile_version_v4, self.user, idempotency_key=ik)
        assert app1.id == app2.id

    def test_different_idempotency_key_after_approval_fails(self):
        approve_corrected_profile_version(self.profile_version_v4, self.user, idempotency_key="key1")
        
        self.profile_version_v4.refresh_from_db()
        with pytest.raises(ValidationError) as exc:
            approve_corrected_profile_version(self.profile_version_v4, self.user, idempotency_key="key2")
        assert "already approved" in str(exc.value)

    def test_version_changes_to_approved(self):
        approve_corrected_profile_version(self.profile_version_v4, self.user)
        self.profile_version_v4.refresh_from_db()
        assert self.profile_version_v4.state == "approved"

    def test_version_becomes_finalized(self):
        approve_corrected_profile_version(self.profile_version_v4, self.user)
        self.profile_version_v4.refresh_from_db()
        assert self.profile_version_v4.is_finalized is True

    def test_active_version_moves_forward(self):
        approve_corrected_profile_version(self.profile_version_v4, self.user)
        self.cand_profile.refresh_from_db()
        assert self.cand_profile.active_version == self.profile_version_v4

    def test_version_3_remains_byte_identical(self):
        v3_checksum_before = self.profile_version_v3.checksum
        v3_name_before = self.profile_version_v3.full_name
        v3_state_before = self.profile_version_v3.state
        
        approve_corrected_profile_version(self.profile_version_v4, self.user)
        
        v3_after = CandidateProfileVersion.objects.get(pk=self.profile_version_v3.id)
        assert v3_after.checksum == v3_checksum_before
        assert v3_after.full_name == v3_name_before
        assert v3_after.state == v3_state_before

    def test_incident_hold_remains_active(self):
        approve_corrected_profile_version(self.profile_version_v4, self.user)
        hold = IncidentHold.objects.get(pk=self.hold.id)
        assert hold.active is True

    def test_quarantined_application_draft_is_unchanged(self):
        approve_corrected_profile_version(self.profile_version_v4, self.user)
        draft = ApplicationDraft.objects.get(pk=self.app_draft.id)
        assert draft.tailored_cv == "A CV for idwsdsjtasdkk"

    def test_external_actions_remain_blocked(self):
        approve_corrected_profile_version(self.profile_version_v4, self.user)
        
        # Trying to prepare application on quarantined draft must raise CommandError due to IncidentHold
        with pytest.raises(CommandError):
            call_command(
                "pilot_job_application_prepare",
                "--user-email", "candidate-10@example.com",
                "--profile-id", self.cand_profile.id,
                "--job-id", self.job.id,
                "--job-version-id", self.job_version.id,
                "--job-match-id", self.job_match.id,
                "--application-draft-id", self.app_draft.id,
                "--resume-export-id", 1
            )

    def test_non_owner_cannot_approve(self):
        with pytest.raises(CommandError):
            call_command(
                "career_identity_corrected_version_approve",
                "--user-email", "other@example.com",
                "--version-id", self.profile_version_v4.id,
                "--confirm", "APPROVE CORRECTED IDENTITY VERSION"
            )

    def test_missing_confirmation_blocks(self):
        with pytest.raises(CommandError):
            call_command(
                "career_identity_corrected_version_approve",
                "--user-email", "candidate-10@example.com",
                "--version-id", self.profile_version_v4.id,
                "--confirm", "INVALID CONFIRMATION TEXT"
            )

    def test_failed_validation_creates_zero_writes(self):
        snapshot_before = self._get_snapshot()
        
        # Trigger validation failure (e.g. approve version 3)
        with pytest.raises(ValidationError):
            approve_corrected_profile_version(self.profile_version_v3, self.user)
            
        snapshot_after = self._get_snapshot()
        assert snapshot_before == snapshot_after
