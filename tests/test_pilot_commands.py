import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from openoutreach.linkedin.models import LinkedInProfile
from openoutreach.career.models import Job, JobApplication, ApplicationDraft, JobVersion, JobMatch
from tests.factories import UserFactory
import hashlib

@pytest.mark.django_db
class TestPilotCommands:
    @pytest.fixture(autouse=True)
    def _setup(self, db):
        self.user = UserFactory(username="seeker_user", email="candidate-10@example.com")
        self.profile = LinkedInProfile.objects.create(
            user=self.user,
            linkedin_username="candidate-10@example.com",
            active=True
        )

        from openoutreach.career.models import CandidateProfile, CandidateProfileVersion
        self.cand_profile = CandidateProfile.objects.create(
            user=self.user,
            full_name="Alice Candidate",
            email="candidate-10@example.com"
        )
        self.profile_version = CandidateProfileVersion.objects.create(
            profile=self.cand_profile,
            version_number=1,
            full_name="Alice Candidate",
            email="candidate-10@example.com",
            created_by=self.user
        )

        from openoutreach.career.models import CandidateIdentityVerification
        self.verification = CandidateIdentityVerification.objects.create(
            candidate_profile=self.cand_profile,
            extracted_name="Alice Candidate",
            confirmed_name="Alice Candidate",
            verification_source="manual",
            status="verified"
        )

        self.job = Job.objects.create(
            title="Senior Developer",
            company="Stark Industries",
            description="Django Developer"
        )
        self.job_version = JobVersion.objects.create(
            job=self.job,
            version_number=1,
            title="Senior Developer",
            company="Stark Industries",
            description="Django Developer"
        )
        self.match = JobMatch.objects.create(
            candidate_profile_version=self.profile_version,
            job_version=self.job_version,
            overall_score=90
        )
        self.app = JobApplication.objects.create(
            candidate_profile=self.cand_profile,
            job=self.job
        )
        self.draft = ApplicationDraft.objects.create(
            application=self.app,
            version_number=1,
            tailored_cv="cv text",
            cover_letter="letter text"
        )

    def test_pilot_linkedin_connection_dry_run(self):
        note = "Hi! I noticed you are hiring."
        checksum = hashlib.sha256(note.encode("utf-8")).hexdigest()
        
        # Test that dry-run passes even if identity is unverified
        self.verification.status = "pending"
        self.verification.save()

        call_command(
            "pilot_linkedin_connection",
            "--dry-run",
            "--profile-id", self.profile.id,
            "--expected-recipient-id", "recipient-id",
            "--expected-content-checksum", checksum,
            "--idempotency-key", "key123",
            "--note-text", note
        )

    def test_pilot_linkedin_connection_real_blocks_on_unverified(self):
        note = "Hi! I noticed you are hiring."
        checksum = hashlib.sha256(note.encode("utf-8")).hexdigest()
        
        self.verification.status = "pending"
        self.verification.save()

        with pytest.raises(CommandError) as exc:
            call_command(
                "pilot_linkedin_connection",
                "--execute",
                "--profile-id", self.profile.id,
                "--expected-recipient-id", "recipient-id",
                "--expected-content-checksum", checksum,
                "--idempotency-key", "key123",
                "--note-text", note
            )
        assert "has not been verified" in str(exc.value)

    def test_pilot_linkedin_followup_dry_run(self):
        msg = "Hi! I wanted to follow up."
        checksum = hashlib.sha256(msg.encode("utf-8")).hexdigest()
        
        # Test that dry-run passes even if identity is unverified
        self.verification.status = "pending"
        self.verification.save()

        call_command(
            "pilot_linkedin_followup",
            "--dry-run",
            "--profile-id", self.profile.id,
            "--expected-recipient-id", "recipient-id",
            "--expected-content-checksum", checksum,
            "--idempotency-key", "key123",
            "--message-text", msg
        )

    def test_pilot_linkedin_followup_real_blocks_on_unverified(self):
        msg = "Hi! I wanted to follow up."
        checksum = hashlib.sha256(msg.encode("utf-8")).hexdigest()
        
        self.verification.status = "pending"
        self.verification.save()

        with pytest.raises(CommandError) as exc:
            call_command(
                "pilot_linkedin_followup",
                "--execute",
                "--profile-id", self.profile.id,
                "--expected-recipient-id", "recipient-id",
                "--expected-content-checksum", checksum,
                "--idempotency-key", "key123",
                "--message-text", msg
            )
        assert "has not been verified" in str(exc.value)

    def test_pilot_job_application_dry_run(self):
        cv_hash = hashlib.sha256(self.draft.tailored_cv.encode("utf-8")).hexdigest()
        cl_hash = hashlib.sha256(self.draft.cover_letter.encode("utf-8")).hexdigest()

        call_command(
            "pilot_job_application",
            "--dry-run",
            "--profile-id", self.profile.id,
            "--expected-recipient-id", "recipient-id",
            "--expected-content-checksum", "dummy",
            "--idempotency-key", "key123",
            "--expected-job-id", self.job.id,
            "--expected-job-version-id", self.job_version.id,
            "--expected-application-id", self.app.id,
            "--expected-draft-id", self.draft.id,
            "--expected-resume-checksum", cv_hash,
            "--expected-cover-letter-checksum", cl_hash
        )

    def test_finalize_and_approve_workflow(self):
        from openoutreach.career.models import CandidateFactDraft, CandidateExtractionRecord, CandidateProfileVersion
        import json

        record = CandidateExtractionRecord.objects.create(
            profile=self.cand_profile,
            source_type="raw_text",
            raw_text="skills text",
            raw_text_hash="hash123",
            status="pending"
        )
        draft = CandidateFactDraft.objects.create(
            extraction_record=record,
            fact_type="skill",
            category="Skill",
            value={"name": "Django"},
            status="pending"
        )

        from openoutreach.career.canonical import compute_canonical_checksum
        checksum = compute_canonical_checksum([draft])

        # Finalize
        call_command(
            "career_finalize_pending_version",
            "--user-email", "candidate-10@example.com",
            "--expected-fact-set-checksum", checksum
        )

        ver = CandidateProfileVersion.objects.get(profile=self.cand_profile, checksum=checksum)
        assert ver.version_number > 1
        assert ver.facts.filter(fact_type="skill", category="Skill").exists()

        # Approve
        call_command(
            "career_approve_profile_version",
            "--user-email", "candidate-10@example.com",
            "--profile-version-id", ver.id,
            "--expected-fact-set-checksum", checksum,
            "--revision-token", ver.revision_token,
            "--confirm", "APPROVE CANDIDATE PROFILE"
        )
        ver.refresh_from_db()
        assert hasattr(ver, "approval")

        # Second approval should fail
        with pytest.raises(CommandError) as exc:
            call_command(
                "career_approve_profile_version",
                "--user-email", "candidate-10@example.com",
                "--profile-version-id", ver.id,
                "--expected-fact-set-checksum", checksum,
                "--revision-token", ver.revision_token,
                "--confirm", "APPROVE CANDIDATE PROFILE"
            )
        assert "already approved" in str(exc.value)

    def test_approve_wrong_checksum_fails(self):
        from openoutreach.career.models import CandidateProfileVersion
        from openoutreach.career.canonical import compute_canonical_checksum
        correct_checksum = compute_canonical_checksum([])
        ver = CandidateProfileVersion.objects.create(
            profile=self.cand_profile,
            version_number=10,
            full_name="Alice Candidate",
            email="candidate-10@example.com",
            checksum=correct_checksum,
            revision_token="token123",
            is_finalized=True,
            state="pending_approval"
        )
        with pytest.raises(CommandError) as exc:
            call_command(
                "career_approve_profile_version",
                "--user-email", "candidate-10@example.com",
                "--profile-version-id", ver.id,
                "--expected-fact-set-checksum", "wrongchecksum",
                "--revision-token", "token123",
                "--confirm", "APPROVE CANDIDATE PROFILE"
            )
        assert "checksum mismatch" in str(exc.value)
        # Verify that state remains unchanged and token remains unchanged
        ver.refresh_from_db()
        assert ver.state == "pending_approval"
        assert ver.revision_token == "token123"
        assert not hasattr(ver, "approval")

    def test_finalized_version_immutability(self):
        from openoutreach.career.models import CandidateProfileVersion, CandidateFact
        from django.core.exceptions import ValidationError
        ver = CandidateProfileVersion.objects.create(
            profile=self.cand_profile,
            version_number=20,
            full_name="Alice Candidate",
            email="candidate-10@example.com",
            is_finalized=False
        )
        fact = CandidateFact.objects.create(
            profile_version=ver,
            fact_type="skill",
            category="Skill",
            value={"name": "Python"}
        )
        ver.is_finalized = True
        ver.save()
        
        # Test editing version fields
        ver.full_name = "Changed Name"
        with pytest.raises(ValidationError):
            ver.save()
            
        # Test editing fact
        fact.category = "Different"
        with pytest.raises(ValidationError):
            fact.save()
            
        # Test deleting fact
        with pytest.raises(ValidationError):
            fact.delete()

    def test_unapproved_consumers_block(self):
        from openoutreach.career.models import CandidateProfileVersion
        unapproved_ver = CandidateProfileVersion.objects.create(
            profile=self.cand_profile,
            version_number=30,
            full_name="Alice Candidate",
            email="candidate-10@example.com",
            checksum="somechecksum",
            is_finalized=True
        )
        
        with pytest.raises(CommandError) as exc:
            call_command(
                "career_calculate_job_match",
                "--user-email", "candidate-10@example.com",
                "--candidate-profile-version-id", unapproved_ver.id,
                "--job-version-id", self.job_version.id
            )
        assert "not approved" in str(exc.value)

    def test_unapproved_consumers_docx_generation(self):
        from openoutreach.career.models import CandidateProfileVersion, ApplicationDraft
        unapproved_ver = CandidateProfileVersion.objects.create(
            profile=self.cand_profile,
            version_number=40,
            full_name="Alice Candidate",
            email="candidate-10@example.com",
            checksum="somechecksum",
            is_finalized=True
        )
        draft = ApplicationDraft.objects.create(
            application=self.app,
            profile_version=unapproved_ver,
            version_number=10,
            tailored_cv="tailored cv",
            cover_letter="cover letter"
        )
        with pytest.raises(CommandError) as exc:
            call_command(
                "career_export_resume_docx",
                "--user-email", "candidate-10@example.com",
                "--draft-id", draft.id
            )
        assert "unapproved" in str(exc.value)

    def test_concurrency_mismatch_revision_token(self):
        from openoutreach.career.models import CandidateProfileVersion
        # 4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945 is empty list checksum
        ver = CandidateProfileVersion.objects.create(
            profile=self.cand_profile,
            version_number=50,
            full_name="Alice Candidate",
            email="candidate-10@example.com",
            checksum="4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945",
            revision_token="token_original",
            is_finalized=True,
            state="pending_approval"
        )
        with pytest.raises(CommandError) as exc:
            call_command(
                "career_approve_profile_version",
                "--user-email", "candidate-10@example.com",
                "--profile-version-id", ver.id,
                "--expected-fact-set-checksum", "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945",
                "--revision-token", "token_stale_or_wrong",
                "--confirm", "APPROVE CANDIDATE PROFILE"
            )
        assert "Concurrency update failed" in str(exc.value)

    def test_cannot_add_fact_after_finalization(self):
        from openoutreach.career.models import CandidateProfileVersion, CandidateFact
        from django.core.exceptions import ValidationError
        ver = CandidateProfileVersion.objects.create(
            profile=self.cand_profile,
            version_number=60,
            full_name="Alice Candidate",
            email="candidate-10@example.com",
            is_finalized=True
        )
        with pytest.raises(ValidationError):
            CandidateFact.objects.create(
                profile_version=ver,
                fact_type="skill",
                category="Skill",
                value={"name": "Postgres"}
            )

    def test_approve_unfinalized_fails(self):
        from openoutreach.career.models import CandidateProfileVersion
        ver = CandidateProfileVersion.objects.create(
            profile=self.cand_profile,
            version_number=70,
            full_name="Alice Candidate",
            email="candidate-10@example.com",
            checksum="4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945",
            revision_token="token123",
            is_finalized=False
        )
        with pytest.raises(CommandError) as exc:
            call_command(
                "career_approve_profile_version",
                "--user-email", "candidate-10@example.com",
                "--profile-version-id", ver.id,
                "--expected-fact-set-checksum", "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945",
                "--revision-token", "token123",
                "--confirm", "APPROVE CANDIDATE PROFILE"
            )
        assert "not finalized" in str(exc.value)

    def test_approve_ownership_check(self):
        from openoutreach.career.models import CandidateProfileVersion
        ver = CandidateProfileVersion.objects.create(
            profile=self.cand_profile,
            version_number=80,
            full_name="Alice Candidate",
            email="candidate-10@example.com",
            checksum="4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945",
            revision_token="token123",
            is_finalized=True,
            state="pending_approval"
        )
        from django.contrib.auth.models import User
        if not User.objects.filter(username="other").exists():
            User.objects.create_user(username="other", email="other@example.com")
        
        with pytest.raises(CommandError) as exc:
            call_command(
                "career_approve_profile_version",
                "--user-email", "other@example.com",
                "--profile-version-id", ver.id,
                "--expected-fact-set-checksum", "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945",
                "--revision-token", "token123",
                "--confirm", "APPROVE CANDIDATE PROFILE"
            )
        assert "does not belong to specified user" in str(exc.value)

    def test_representation_mismatch_rejected(self):
        from openoutreach.career.models import CandidateProfileVersion, CandidateFact
        ver = CandidateProfileVersion.objects.create(
            profile=self.cand_profile,
            version_number=90,
            full_name="Alice Candidate",
            email="candidate-10@example.com",
            checksum="4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945",
            revision_token="token123",
            is_finalized=False,
            state="pending_approval"
        )
        CandidateFact.objects.create(
            profile_version=ver,
            fact_type="skill",
            category="Skill",
            value={"name": "C++"}
        )
        ver.is_finalized = True
        ver.save()

        with pytest.raises(CommandError) as exc:
            call_command(
                "career_approve_profile_version",
                "--user-email", "candidate-10@example.com",
                "--profile-version-id", ver.id,
                "--expected-fact-set-checksum", "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945",
                "--revision-token", "token123",
                "--confirm", "APPROVE CANDIDATE PROFILE"
            )
        assert "Number of facts in snapshot does not match CandidateFact rows." in str(exc.value)

    def test_idempotent_approval_key(self):
        from openoutreach.career.models import CandidateProfileVersion
        ver = CandidateProfileVersion.objects.create(
            profile=self.cand_profile,
            version_number=100,
            full_name="Alice Candidate",
            email="candidate-10@example.com",
            checksum="4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945",
            revision_token="token123",
            is_finalized=True,
            state="pending_approval"
        )
        call_command(
            "career_approve_profile_version",
            "--user-email", "candidate-10@example.com",
            "--profile-version-id", ver.id,
            "--expected-fact-set-checksum", "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945",
            "--revision-token", "token123",
            "--confirm", "APPROVE CANDIDATE PROFILE",
            "--idempotency-key", "key123"
        )
        ver.refresh_from_db()
        assert hasattr(ver, "approval")
        assert ver.profile.active_version == ver
        original_token = ver.revision_token

        call_command(
            "career_approve_profile_version",
            "--user-email", "candidate-10@example.com",
            "--profile-version-id", ver.id,
            "--expected-fact-set-checksum", "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945",
            "--revision-token", "token123",
            "--confirm", "APPROVE CANDIDATE PROFILE",
            "--idempotency-key", "key123"
        )
        ver.refresh_from_db()
        assert ver.revision_token == original_token


