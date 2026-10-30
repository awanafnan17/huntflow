import pytest
from django.core.exceptions import ValidationError
from django.test import Client
from django.urls import reverse
from django.core.management import call_command
from django.core.management.base import CommandError
from tests.factories import UserFactory
from unittest.mock import patch
from openoutreach.career.service import create_corrected_pending_version_with_provenance

from openoutreach.career.models import (
    CandidateProfile,
    CandidateProfileVersion,
    CandidateDocument,
    CandidateDocumentVersion,
    CandidateExtractionRecord,
    CandidateProfileApproval,
    CandidateIdentityVerification,
    CandidateAuditEvent,
    IncidentHold,
    IncidentHoldTarget,
    Job,
    JobVersion,
    JobMatch,
    JobApplication,
    ApplicationDraft
)

@pytest.mark.django_db
class TestIdentityConfirmationExecution:
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
        
        # Set up active incident hold and targets
        self.hold = IncidentHold.objects.create(
            reason="IDENTITY_AND_SOURCE_INTEGRITY_INCIDENT",
            active=True
        )
        IncidentHoldTarget.objects.create(incident_hold=self.hold, content_object=self.job)
        IncidentHoldTarget.objects.create(incident_hold=self.hold, content_object=self.app_draft)

    def teardown_method(self, method):
        if hasattr(self, "patcher"):
            self.patcher.stop()

    def test_non_owner_cannot_confirm_identity(self):
        url = reverse("career:identity_review_confirm")
        self.client.force_login(self.other_user)
        
        resp = self.client.post(url, {
            "profile_id": self.cand_profile.id,
            "action": "confirm_identity",
            "confirmed_name": "Alex Morgan"
        })
        assert resp.status_code == 403

    def test_owner_can_confirm_identity(self):
        url = reverse("career:identity_review_confirm")
        self.client.force_login(self.user)
        
        resp = self.client.post(url, {
            "profile_id": self.cand_profile.id,
            "action": "confirm_identity",
            "confirmed_name": "Alex Morgan"
        })
        assert resp.status_code == 302
        
        verification = CandidateIdentityVerification.objects.get(candidate_profile=self.cand_profile)
        assert verification.status == "verified"
        assert verification.confirmed_name == "Alex Morgan"

    def test_identity_mismatch_creates_no_records(self):
        url = reverse("career:identity_review_confirm")
        self.client.force_login(self.user)
        
        # Confirmation mismatch
        self.client.post(url, {
            "profile_id": self.cand_profile.id,
            "action": "confirm_identity",
            "confirmed_name": "Candidate"
        })
        
        assert not CandidateIdentityVerification.objects.filter(candidate_profile=self.cand_profile).exists()
        assert not CandidateAuditEvent.objects.filter(event_type="IDENTITY_VERIFICATION_COMPLETED").exists()

    def test_confirmation_requires_exact_name_match(self):
        url = reverse("career:identity_review_confirm")
        self.client.force_login(self.user)
        
        # Test lowercase mismatch
        self.client.post(url, {
            "profile_id": self.cand_profile.id,
            "action": "confirm_identity",
            "confirmed_name": "alex morgan"
        })
        assert not CandidateIdentityVerification.objects.filter(candidate_profile=self.cand_profile).exists()
        
        # Test spaces mismatch
        self.client.post(url, {
            "profile_id": self.cand_profile.id,
            "action": "confirm_identity",
            "confirmed_name": " Alex Morgan "
        })
        assert not CandidateIdentityVerification.objects.filter(candidate_profile=self.cand_profile).exists()

    def test_verification_record_created(self):
        url = reverse("career:identity_review_confirm")
        self.client.force_login(self.user)
        
        self.client.post(url, {
            "profile_id": self.cand_profile.id,
            "action": "confirm_identity",
            "confirmed_name": "Alex Morgan"
        })
        
        v = CandidateIdentityVerification.objects.get(candidate_profile=self.cand_profile)
        assert v.status == "verified"
        assert v.extracted_name == "Alex Morgan"
        assert v.confirmed_name == "Alex Morgan"
        assert v.verified_by == self.user
        assert v.source_document_id == self.doc.id
        assert v.source_document_version_id == self.doc_version.id

    def test_audit_event_created(self):
        url = reverse("career:identity_review_confirm")
        self.client.force_login(self.user)
        
        self.client.post(url, {
            "profile_id": self.cand_profile.id,
            "action": "confirm_identity",
            "confirmed_name": "Alex Morgan"
        })
        
        event = CandidateAuditEvent.objects.filter(event_type="IDENTITY_VERIFICATION_COMPLETED").first()
        assert event is not None
        assert "idwsdsjtasdkk" in event.description
        assert "Alex Morgan" in event.description

    def test_version_3_unchanged_after_confirmation(self):
        url = reverse("career:identity_review_confirm")
        self.client.force_login(self.user)
        
        self.client.post(url, {
            "profile_id": self.cand_profile.id,
            "action": "confirm_identity",
            "confirmed_name": "Alex Morgan"
        })
        
        v3 = CandidateProfileVersion.objects.get(pk=self.profile_version.id)
        assert v3.full_name == "idwsdsjtasdkk"
        assert v3.state == "approved"
        assert v3.checksum == "9a216e1c969a3d9b3859a30bec41cba249d7fc856af6087f6a77631526e4c8e3"

    def test_old_quarantined_application_unchanged(self):
        url = reverse("career:identity_review_confirm")
        self.client.force_login(self.user)
        
        self.client.post(url, {
            "profile_id": self.cand_profile.id,
            "action": "confirm_identity",
            "confirmed_name": "Alex Morgan"
        })
        
        draft = ApplicationDraft.objects.get(pk=self.app_draft.id)
        assert draft.tailored_cv == "A CV for idwsdsjtasdkk"

    def test_incident_hold_remains_active(self):
        url = reverse("career:identity_review_confirm")
        self.client.force_login(self.user)
        
        self.client.post(url, {
            "profile_id": self.cand_profile.id,
            "action": "confirm_identity",
            "confirmed_name": "Alex Morgan"
        })
        
        # Verify hold is still active
        hold = IncidentHold.objects.get(pk=self.hold.id)
        assert hold.active is True
        assert IncidentHoldTarget.objects.filter(incident_hold=hold).count() == 2

    def test_legacy_endpoint_blocked(self):
        url = reverse("career:identity_review_confirm")
        self.client.force_login(self.user)
        # Step 1: verify
        self.client.post(url, {
            "profile_id": self.cand_profile.id,
            "action": "confirm_identity",
            "confirmed_name": "Alex Morgan"
        })
        before_count = CandidateProfileVersion.objects.count()
        # Step 2: attempt creation from legacy view
        resp = self.client.post(url, {
            "profile_id": self.cand_profile.id,
            "action": "create_corrected_version",
            "confirmed_name": "Alex Morgan"
        })
        assert resp.status_code in [200, 302, 400]
        assert CandidateProfileVersion.objects.count() == before_count

    def test_new_version_is_pending_not_approved(self):
        # Directly call the new service
        v4 = create_corrected_pending_version_with_provenance(
            profile=self.cand_profile,
            historical_version_id=self.profile_version.id,
            document_version_id=self.doc_version.id,
            extraction_record_id=self.extraction.id,
            corrected_full_name="Alex Morgan",
            idempotency_key="test_exec_ik_1",
            created_by=self.user
        )
        assert v4.state == "pending_approval"
        assert v4.is_finalized is False

    def test_new_version_contains_verified_name(self):
        v4 = create_corrected_pending_version_with_provenance(
            profile=self.cand_profile,
            historical_version_id=self.profile_version.id,
            document_version_id=self.doc_version.id,
            extraction_record_id=self.extraction.id,
            corrected_full_name="Alex Morgan",
            idempotency_key="test_exec_ik_2",
            created_by=self.user
        )
        assert v4.full_name == "Alex Morgan"

    def test_old_identity_not_present(self):
        v4 = create_corrected_pending_version_with_provenance(
            profile=self.cand_profile,
            historical_version_id=self.profile_version.id,
            document_version_id=self.doc_version.id,
            extraction_record_id=self.extraction.id,
            corrected_full_name="Alex Morgan",
            idempotency_key="test_exec_ik_3",
            created_by=self.user
        )
        assert "idwsdsjtasdkk" not in v4.full_name

    def test_external_actions_remain_blocked_until_new_version_approved(self):
        v4 = create_corrected_pending_version_with_provenance(
            profile=self.cand_profile,
            historical_version_id=self.profile_version.id,
            document_version_id=self.doc_version.id,
            extraction_record_id=self.extraction.id,
            corrected_full_name="Alex Morgan",
            idempotency_key="test_exec_ik_4",
            created_by=self.user
        )
        
        # Call calculate_job_match using version 4 (which is pending approval)
        # It must block with CommandError because version 4 is not approved
        with pytest.raises(CommandError) as exc:
            call_command(
                "career_calculate_job_match",
                "--user-email", "candidate-10@example.com",
                "--candidate-profile-version-id", v4.id,
                "--job-version-id", self.job_version.id
            )
        assert "is not approved" in str(exc.value)
