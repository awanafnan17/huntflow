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
    CandidateIdentityVerification,
    CandidateAuditEvent,
    CandidateProfileApproval,
    IncidentHold,
    IncidentHoldTarget,
    Job,
    JobVersion,
    JobMatch,
    JobApplication,
    ApplicationDraft
)
from openoutreach.career.service import preview_identity_resolution

@pytest.mark.django_db
class TestIdentityPreflight:
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

    def _get_snapshot(self):
        return {
            "CandidateProfileVersion": CandidateProfileVersion.objects.count(),
            "CandidateProfileApproval": CandidateProfileApproval.objects.count(),
            "CandidateIdentityVerification": CandidateIdentityVerification.objects.count(),
            "CandidateAuditEvent": CandidateAuditEvent.objects.count(),
            "IncidentHold": IncidentHold.objects.count(),
            "IncidentHoldTarget": IncidentHoldTarget.objects.count(),
            "ApplicationDraft": ApplicationDraft.objects.count(),
            "JobApplication": JobApplication.objects.count(),
        }

    def test_preflight_requires_owner(self):
        # Using a non-existent email must raise CommandError
        with pytest.raises(CommandError):
            call_command("career_identity_review_preflight", "--user-email", "nonexistent@example.com")
        
        # Valid email works
        out = io.StringIO()
        call_command("career_identity_review_preflight", "--user-email", "candidate-10@example.com", stdout=out)
        output = out.getvalue()
        assert "CURRENT STORED IDENTITY:" in output

    def test_preflight_has_no_database_writes(self):
        snapshot_before = self._get_snapshot()
        
        out = io.StringIO()
        call_command("career_identity_review_preflight", "--user-email", "candidate-10@example.com", stdout=out)
        
        snapshot_after = self._get_snapshot()
        assert snapshot_before == snapshot_after

    def test_preflight_database_snapshot_unchanged(self):
        # Strictly checking that nothing mutates when running preview resolution or preflight command
        snapshot_before = self._get_snapshot()
        
        preview_identity_resolution(self.cand_profile)
        
        snapshot_after = self._get_snapshot()
        assert snapshot_before == snapshot_after

    def test_preflight_reports_version_3(self):
        out = io.StringIO()
        call_command("career_identity_review_preflight", "--user-email", "candidate-10@example.com", stdout=out)
        output = out.getvalue()
        
        assert f"Active version ID: {self.profile_version.id}" in output
        assert "Active version checksum: 9a216e1c969a3d9b3859a30bec41cba249d7fc856af6087f6a77631526e4c8e3" in output
        assert "Current full_name: idwsdsjtasdkk" in output

    def test_preflight_reports_extracted_identity(self):
        out = io.StringIO()
        call_command("career_identity_review_preflight", "--user-email", "candidate-10@example.com", stdout=out)
        output = out.getvalue()
        
        assert "Extracted name: Alex Morgan" in output
        assert "Alex Morgan" in output

    def test_preflight_reports_quarantined_artifacts(self):
        res = preview_identity_resolution(self.cand_profile)
        artifacts = res["affected_artifacts"]
        
        # Verify preview lists the quarantined artifacts correctly
        assert len(artifacts["jobs"]) == 1
        assert artifacts["jobs"][0]["id"] == 1
        assert len(artifacts["applicationdrafts"]) == 1
        assert artifacts["applicationdrafts"][0]["id"] == self.app_draft.id

    def test_preview_resolution_does_not_create_records(self):
        snapshot_before = self._get_snapshot()
        
        res = preview_identity_resolution(self.cand_profile)
        assert res["stored_identity"]["full_name"] == "idwsdsjtasdkk"
        assert res["extracted_identity"]["extracted_name"] == "Alex Morgan"
        
        snapshot_after = self._get_snapshot()
        assert snapshot_before == snapshot_after
