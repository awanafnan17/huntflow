import pytest
import hashlib
from django.core.exceptions import ValidationError
from django.test import Client
from django.urls import reverse
from django.contrib.auth.models import User
from tests.factories import UserFactory
from unittest.mock import patch

from openoutreach.career.models import (
    CandidateProfile,
    CandidateProfileVersion,
    CandidateDocument,
    CandidateDocumentVersion,
    CandidateProfileApproval,
    CandidateIdentityVerification,
    CandidateAuditEvent,
    Job,
    JobVersion,
    JobMatch,
    JobApplication,
    ApplicationDraft,
    CandidateFact
)
from openoutreach.career.service import (
    get_identity_comparison_data,
    verify_candidate_identity,
    create_identity_corrected_profile_version
)

@pytest.mark.django_db
class TestIdentityResolution:
    @pytest.fixture(autouse=True)
    def _setup(self, db):
        self.patcher = patch("openoutreach.career.service.extract_text_from_file_version", return_value="Alex Morgan\n+1-202-555-0100 | candidate-2@example.com")
        self.mock_extract = self.patcher.start()
        
        self.client = Client()
        self.user = UserFactory(username="seeker_user", email="candidate-10@example.com")
        self.other_user = UserFactory(username="other_user", email="other@example.com")
        
        # Candidate Profile
        self.cand_profile = CandidateProfile.objects.create(
            user=self.user,
            full_name="idwsdsjtasdkk",
            email="candidate-10@example.com"
        )
        
        # Version 3
        self.profile_version = CandidateProfileVersion.objects.create(
            profile=self.cand_profile,
            version_number=3,
            full_name="idwsdsjtasdkk",
            email="candidate-10@example.com",
            created_by=self.user,
            is_finalized=True,
            state="approved",
            checksum="9a216e1c969a3d9b3859a30bec41cba249d7fc856af6087f6a77631526e4c8e3"
        )
        # Create legacy approval so that version has exactly one approval
        CandidateProfileApproval.objects.create(
            profile_version=self.profile_version,
            approved_by=self.user,
            approval_reason="legacy_import",
            approval_note="Initial legacy approval",
            idempotency_key="legacy_ik"
        )
        # Update profile to point to active version 3
        self.cand_profile.active_version = self.profile_version
        self.cand_profile.save()
        
        # Source document & version
        self.doc = CandidateDocument.objects.create(
            profile=self.cand_profile,
            name="Candidate_Awan.pdf"
        )
        self.doc_version = CandidateDocumentVersion.objects.create(
            document=self.doc,
            version_number=1,
            file="dummy.txt",
            file_hash="abc",
            file_size=10,
            mime_type="text/plain",
            original_filename="dummy.txt"
        )
        from openoutreach.career.models import CandidateExtractionRecord
        self.extraction = CandidateExtractionRecord.objects.create(
            profile=self.cand_profile,
            document_version=self.doc_version,
            status="pending",
            raw_text="Alex Morgan\n+1-202-555-0100 | candidate-2@example.com",
            raw_text_hash="abc"
        )
        
        # Job and application artifacts (Quarantined)
        self.job = Job.objects.create(pk=1, title="Software Engineer", company="GenOO")
        self.job_version = JobVersion.objects.create(pk=1, job=self.job, version_number=1, title="Software Engineer", company="GenOO")
        self.job_match = JobMatch.objects.create(pk=1, candidate_profile_version=self.profile_version, job_version=self.job_version, overall_score=85)
        self.job_app = JobApplication.objects.create(pk=1, candidate_profile=self.cand_profile, job=self.job)
        self.app_draft = ApplicationDraft.objects.create(
            application=self.job_app,
            version_number=1,
            tailored_cv="A CV for idwsdsjtasdkk",
            cover_letter="Cover letter for idwsdsjtasdkk"
        )

    def teardown_method(self, method):
        if hasattr(self, "patcher"):
            self.patcher.stop()

    def test_identity_review_requires_owner(self):
        url = reverse("career:identity_review")
        
        # Other user logged in
        self.client.force_login(self.other_user)
        resp = self.client.get(url)
        assert resp.status_code == 403
        
        # Owner logged in
        self.client.force_login(self.user)
        resp = self.client.get(url)
        assert resp.status_code == 200
        assert "idwsdsjtasdkk" in resp.content.decode("utf-8")
        assert "Alex Morgan" in resp.content.decode("utf-8")

    def test_identity_confirmation_creates_verification_only(self):
        url = reverse("career:identity_review")
        self.client.force_login(self.user)
        
        resp = self.client.post(url, {
            "action": "verify",
            "extracted_name": "Alex Morgan",
            "confirmed_name": "Alex Morgan"
        })
        assert resp.status_code == 302
        
        verification = CandidateIdentityVerification.objects.get(candidate_profile=self.cand_profile)
        assert verification.status == "verified"
        assert verification.confirmed_name == "Alex Morgan"
        
        # Check that no new profile version is created
        assert self.cand_profile.versions.count() == 1
        assert self.profile_version.state == "approved"

    def test_identity_confirmation_does_not_modify_version_3(self):
        url = reverse("career:identity_review")
        self.client.force_login(self.user)
        
        # Step 1: verify identity
        self.client.post(url, {
            "action": "verify",
            "extracted_name": "Alex Morgan",
            "confirmed_name": "Alex Morgan"
        })
        
        # Step 2: create corrected version
        self.client.post(url, {
            "action": "create_corrected_version"
        })
        
        # Query Version 3 again
        v3 = CandidateProfileVersion.objects.get(pk=self.profile_version.id)
        assert v3.full_name == "idwsdsjtasdkk"
        assert v3.state == "approved"
        assert v3.checksum == "9a216e1c969a3d9b3859a30bec41cba249d7fc856af6087f6a77631526e4c8e3"

    def test_legacy_endpoint_blocked(self):
        url = reverse("career:identity_review")
        self.client.force_login(self.user)
        self.client.post(url, {
            "action": "verify",
            "extracted_name": "Alex Morgan",
            "confirmed_name": "Alex Morgan"
        })
        before_count = CandidateProfileVersion.objects.count()
        resp = self.client.post(url, {
            "action": "create_corrected_version"
        })
        assert resp.status_code == 400 or resp.status_code == 200
        assert CandidateProfileVersion.objects.count() == before_count

    def test_identity_confirmation_does_not_auto_approve_version(self):
        from openoutreach.career.service import create_corrected_pending_version_with_provenance
        v4 = create_corrected_pending_version_with_provenance(
            profile=self.cand_profile,
            historical_version_id=self.profile_version.id,
            document_version_id=self.doc_version.id,
            extraction_record_id=self.extraction.id,
            corrected_full_name="Alex Morgan",
            idempotency_key="test_res_ik_1",
            created_by=self.user
        )
        assert v4.state == "pending_approval"
        assert v4.is_finalized is False

    def test_identity_mismatch_requires_manual_review(self):
        url = reverse("career:identity_review")
        self.client.force_login(self.user)
        
        resp = self.client.post(url, {
            "action": "verify",
            "extracted_name": "Alex Morgan",
            "confirmed_name": "Different Name"
        })
        
        # Page should show error
        assert resp.status_code == 200
        assert "IDENTITY MISMATCH REQUIRES REVIEW" in resp.content.decode("utf-8")
        
        # No verification record should exist
        assert not CandidateIdentityVerification.objects.filter(candidate_profile=self.cand_profile).exists()

    def test_corrected_version_excludes_old_identity(self):
        from openoutreach.career.service import create_corrected_pending_version_with_provenance
        v4 = create_corrected_pending_version_with_provenance(
            profile=self.cand_profile,
            historical_version_id=self.profile_version.id,
            document_version_id=self.doc_version.id,
            extraction_record_id=self.extraction.id,
            corrected_full_name="Alex Morgan",
            idempotency_key="test_res_ik_2",
            created_by=self.user
        )
        assert v4.full_name == "Alex Morgan"
        assert "idwsdsjtasdkk" not in v4.full_name

    def test_quarantined_application_draft_is_not_mutated(self):
        from openoutreach.career.service import create_corrected_pending_version_with_provenance
        v4 = create_corrected_pending_version_with_provenance(
            profile=self.cand_profile,
            historical_version_id=self.profile_version.id,
            document_version_id=self.doc_version.id,
            extraction_record_id=self.extraction.id,
            corrected_full_name="Alex Morgan",
            idempotency_key="test_res_ik_3",
            created_by=self.user
        )
        # Verify the quarantined draft is not modified
        draft = ApplicationDraft.objects.get(pk=self.app_draft.id)
        assert draft.tailored_cv == "A CV for idwsdsjtasdkk"
        assert draft.cover_letter == "Cover letter for idwsdsjtasdkk"

    def test_identity_verification_creates_audit_event(self):
        verify_candidate_identity(
            profile=self.cand_profile,
            extracted_name="Alex Morgan",
            confirmed_name="Alex Morgan",
            source_document_id=self.doc.id,
            source_document_version_id=self.doc_version.id,
            evidence_excerpt="Alex Morgan",
            verified_by=self.user
        )
        
        event = CandidateAuditEvent.objects.filter(event_type="IDENTITY_VERIFICATION_COMPLETED").first()
        assert event is not None
        assert "Alex Morgan" in event.description

    def test_identity_verification_cannot_create_multiple_verified_records(self):
        verify_candidate_identity(
            profile=self.cand_profile,
            extracted_name="Alex Morgan",
            confirmed_name="Alex Morgan",
            source_document_id=self.doc.id,
            source_document_version_id=self.doc_version.id,
            evidence_excerpt="Alex Morgan",
            verified_by=self.user
        )
        
        # Second verification should fail
        with pytest.raises(ValidationError):
            verify_candidate_identity(
                profile=self.cand_profile,
                extracted_name="Alex Morgan",
                confirmed_name="Alex Morgan",
                source_document_id=self.doc.id,
                source_document_version_id=self.doc_version.id,
                evidence_excerpt="Alex Morgan",
                verified_by=self.user
            )
