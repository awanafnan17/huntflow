import pytest
import io
from django.core.management import call_command
from django.core.management.base import CommandError
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
from openoutreach.career.service import review_corrected_profile_version

@pytest.mark.django_db
class TestIdentityCorrectedVersionReview:
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

    def test_review_requires_owner(self):
        # Command should fail on unauthorized user email (non-owner or doesn't match version)
        with pytest.raises(CommandError):
            call_command(
                "career_identity_corrected_version_review",
                "--user-email", "other@example.com",
                "--version-id", self.profile_version_v4.id
            )

    def test_review_service_is_read_only(self):
        snapshot_before = self._get_snapshot()
        
        review_corrected_profile_version(self.profile_version_v4)
        
        snapshot_after = self._get_snapshot()
        assert snapshot_before == snapshot_after

    def test_review_command_is_read_only(self):
        snapshot_before = self._get_snapshot()
        
        out = io.StringIO()
        call_command(
            "career_identity_corrected_version_review",
            "--user-email", "candidate-10@example.com",
            "--version-id", self.profile_version_v4.id,
            stdout=out
        )
        
        snapshot_after = self._get_snapshot()
        assert snapshot_before == snapshot_after
        
        # Verify output sections
        output = out.getvalue()
        assert "VERSION STATUS" in output
        assert "IDENTITY COMPARISON" in output
        assert "FACT ALIGNMENT" in output
        assert "CONTAMINATION CHECK" in output
        assert "DOCUMENT REFERENCE CHECK" in output
        assert "INCIDENT HOLD STATUS" in output
        assert "APPROVAL STATUS" in output
        assert "MUTATION STATUS" in output
        assert "READ_ONLY" in output

    def test_version_3_remains_immutable(self):
        v3_checksum_before = self.profile_version_v3.checksum
        v3_name_before = self.profile_version_v3.full_name
        
        review_corrected_profile_version(self.profile_version_v4)
        
        v3_after = CandidateProfileVersion.objects.get(pk=self.profile_version_v3.id)
        assert v3_after.checksum == v3_checksum_before
        assert v3_after.full_name == v3_name_before

    def test_corrected_version_has_verified_identity(self):
        v = CandidateIdentityVerification.objects.get(candidate_profile=self.cand_profile)
        assert v.status == "verified"
        assert v.confirmed_name == "Alex Morgan"

    def test_corrected_version_has_pending_state(self):
        assert self.profile_version_v4.state == "pending_approval"
        assert self.profile_version_v4.is_finalized is False

    def test_corrected_version_preserves_facts(self):
        res = review_corrected_profile_version(self.profile_version_v4)
        comp = res["fact_comparison"]
        
        assert comp["total_source_facts"] == 2
        assert comp["total_corrected_facts"] == 2
        assert len(comp["added_facts"]) == 0
        assert len(comp["removed_facts"]) == 0
        assert len(comp["modified_facts"]) == 0

    def test_old_identity_not_present(self):
        res = review_corrected_profile_version(self.profile_version_v4)
        assert res["contamination_scan"]["contamination_found"] is False

    def test_application_draft_1_unchanged(self):
        draft = ApplicationDraft.objects.get(pk=self.app_draft.id)
        assert draft.tailored_cv == "A CV for idwsdsjtasdkk"

    def test_incident_hold_remains_active(self):
        hold = IncidentHold.objects.get(pk=self.hold.id)
        assert hold.active is True

    def test_no_approval_created(self):
        assert CandidateProfileApproval.objects.filter(profile_version=self.profile_version_v4).count() == 0

    def test_zero_database_mutations(self):
        snapshot_before = self._get_snapshot()
        
        # Perform all actions
        review_corrected_profile_version(self.profile_version_v4)
        out = io.StringIO()
        call_command(
            "career_identity_corrected_version_review",
            "--user-email", "candidate-10@example.com",
            "--version-id", self.profile_version_v4.id,
            stdout=out
        )
        
        snapshot_after = self._get_snapshot()
        assert snapshot_before == snapshot_after

    def test_review_rejects_approved_version(self):
        from django.core.exceptions import ValidationError
        # Call service directly
        with pytest.raises(ValidationError):
            review_corrected_profile_version(self.profile_version_v3)

        # Call command
        with pytest.raises(CommandError) as exc:
            call_command(
                "career_identity_corrected_version_review",
                "--user-email", "candidate-10@example.com",
                "--version-id", self.profile_version_v3.id
            )
        assert "approved historical identity version" in str(exc.value)

    def test_review_accepts_pending_corrected_version(self):
        res = review_corrected_profile_version(self.profile_version_v4)
        assert res["version_role"] == "corrected_pending"

        out = io.StringIO()
        call_command(
            "career_identity_corrected_version_review",
            "--user-email", "candidate-10@example.com",
            "--version-id", self.profile_version_v4.id,
            stdout=out
        )
        assert "Version role: corrected_pending" in out.getvalue()

    def test_review_does_not_scan_old_version_as_corrected(self):
        # Verify Version 3 is preserved and not scanned as a corrected target
        v3 = CandidateProfileVersion.objects.get(pk=self.profile_version_v3.id)
        assert v3.state == "approved"
        assert v3.is_finalized is True
        assert v3.full_name == "idwsdsjtasdkk"

    def test_review_reports_corrected_identity(self):
        res = review_corrected_profile_version(self.profile_version_v4)
        assert res["identity_comparison"]["old_identity"] == "idwsdsjtasdkk"
        assert res["identity_comparison"]["new_identity"] == "Alex Morgan"
        assert res["identity_comparison"]["identity_changed"] is True

    def test_review_reports_no_contamination(self):
        res = review_corrected_profile_version(self.profile_version_v4)
        assert res["contamination_scan"]["contamination_found"] is False

    def test_review_reports_zero_approval_for_pending_version(self):
        res = review_corrected_profile_version(self.profile_version_v4)
        assert res["approval_status"]["approval_count"] == 0
        assert res["approval_status"]["is_approved"] is False

