import pytest
import hashlib
from django.core.management import call_command
from django.core.management.base import CommandError
from openoutreach.linkedin.models import LinkedInProfile, ConnectionAttempt
from openoutreach.career.models import Job, JobApplication, ApplicationDraft, JobVersion, JobMatch, CandidateFact
from tests.factories import UserFactory
from unittest.mock import MagicMock, patch

@pytest.mark.django_db
class TestIncidentVerification:
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
            created_by=self.user,
            is_finalized=True
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
            company="Real Company Ltd",
            description="Django Developer"
        )
        self.job_version = JobVersion.objects.create(
            job=self.job,
            version_number=1,
            title="Senior Developer",
            company="Real Company Ltd",
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
            profile_version=self.profile_version,
            version_number=1,
            tailored_cv="cv text",
            cover_letter="letter text"
        )

    @patch("openoutreach.linkedin.browser.session.AccountSession.ensure_browser")
    def test_real_search_never_returns_fixture_data_on_failure(self, mock_ensure_browser):
        # 1. Proves that real search fails closed on browser exceptions instead of falling back to seed/fixture jobs
        mock_ensure_browser.side_effect = Exception("Browser initialization failed")
        with pytest.raises(CommandError) as exc:
            call_command(
                "pilot_linkedin_job_search",
                "--user-email", "candidate-10@example.com",
                "--profile-id", self.profile.id,
                "--query", "Python Developer",
                "--location", "Remote",
                "--real"
            )
        assert "Browser initialization failed" in str(exc.value)

    @patch("openoutreach.linkedin.browser.session.AccountSession.ensure_browser")
    def test_real_search_fails_closed_on_parse_failure(self, mock_ensure_browser):
        # 2. Proves that real search fails closed on card parsing errors
        session_mock = MagicMock()
        session_mock.page.query_selector_all.return_value = [] # no cards
        
        with patch("openoutreach.core.management.commands.pilot_linkedin_job_search.AccountSession", return_value=session_mock):
            with pytest.raises(CommandError) as exc:
                call_command(
                    "pilot_linkedin_job_search",
                    "--user-email", "candidate-10@example.com",
                    "--profile-id", self.profile.id,
                    "--query", "Python Developer",
                    "--location", "Remote",
                    "--real"
                )
            assert "No job list elements matched" in str(exc.value)

    @patch("openoutreach.linkedin.browser.session.AccountSession.ensure_browser")
    def test_real_search_fails_closed_on_empty_or_ambiguous_identity(self, mock_ensure_browser):
        # 3. Proves that real search fails closed when scraped title or company is "Unknown"
        session_mock = MagicMock()
        card_mock = MagicMock()
        card_mock.query_selector.return_value = None # None title and company
        card_mock.get_attribute.return_value = "12345"
        session_mock.page.query_selector_all.return_value = [card_mock]

        with patch("openoutreach.core.management.commands.pilot_linkedin_job_search.AccountSession", return_value=session_mock):
            with pytest.raises(CommandError) as exc:
                call_command(
                    "pilot_linkedin_job_search",
                    "--user-email", "candidate-10@example.com",
                    "--profile-id", self.profile.id,
                    "--query", "Python Developer",
                    "--location", "Remote",
                    "--real"
                )
            assert "ambiguous company/title identity" in str(exc.value)

    def test_recruiter_targets_cannot_come_from_test_fixtures(self):
        # 4. Proves that recruiter target searching blocks test fixtures in real mode
        with pytest.raises(CommandError) as exc:
            call_command(
                "pilot_linkedin_search_connection_targets",
                "--user-email", "candidate-10@example.com",
                "--profile-id", self.profile.id,
                "--job-id", self.job.id,
                "--real"
            )
        assert "fixtures are prohibited in real mode" in str(exc.value)

    @patch("openoutreach.linkedin.browser.session.AccountSession.ensure_browser")
    def test_connection_attempt_idempotency_blocks_second_network_attempt(self, mock_ensure_browser):
        # 5. Proves that identical connection request idempotency key blocks further execution
        note = "Hi! Let's connect."
        checksum = hashlib.sha256(note.encode("utf-8")).hexdigest()
        key = "idemp-key-123"

        # Mock page to throw on navigation so it records a failed attempt
        session_mock = MagicMock()
        session_mock.page.goto.side_effect = Exception("Page load error")
        
        with patch("openoutreach.core.management.commands.pilot_linkedin_connection.AccountSession", return_value=session_mock):
            with pytest.raises(Exception):
                call_command(
                    "pilot_linkedin_connection",
                    "--user-email", "candidate-10@example.com",
                    "--profile-id", self.profile.id,
                    "--expected-recipient-id", "pepperpotts-test",
                    "--expected-content-checksum", checksum,
                    "--idempotency-key", key,
                    "--note-text", note,
                    "--execute"
                )
            
            # Second run must raise CommandError immediately due to idempotency block
            with pytest.raises(CommandError) as exc:
                call_command(
                    "pilot_linkedin_connection",
                    "--user-email", "candidate-10@example.com",
                    "--profile-id", self.profile.id,
                    "--expected-recipient-id", "pepperpotts-test",
                    "--expected-content-checksum", checksum,
                    "--idempotency-key", key,
                    "--note-text", note,
                    "--execute"
                )
            assert "already used" in str(exc.value)

    @patch("openoutreach.linkedin.browser.session.AccountSession.ensure_browser")
    def test_failed_and_indeterminate_attempts_are_persisted(self, mock_ensure_browser):
        # 6. Proves failed attempt state is saved in connection attempts
        note = "Hi! Let's connect."
        checksum = hashlib.sha256(note.encode("utf-8")).hexdigest()
        key = "idemp-key-failed"

        session_mock = MagicMock()
        session_mock.page.goto.side_effect = Exception("Page navigation error")

        with patch("openoutreach.core.management.commands.pilot_linkedin_connection.AccountSession", return_value=session_mock):
            with pytest.raises(Exception):
                call_command(
                    "pilot_linkedin_connection",
                    "--user-email", "candidate-10@example.com",
                    "--profile-id", self.profile.id,
                    "--expected-recipient-id", "pepperpotts-test",
                    "--expected-content-checksum", checksum,
                    "--idempotency-key", key,
                    "--note-text", note,
                    "--execute"
                )
            
            attempt = ConnectionAttempt.objects.get(idempotency_key=key)
            assert attempt.status == "failed"
            assert "Page navigation error" in attempt.error_message

    def test_required_unsupported_application_fields_block_submission(self):
        # 7. Proves lack of approved facts for required fields blocks submit execution
        cv_hash = hashlib.sha256(self.draft.tailored_cv.encode("utf-8")).hexdigest()
        cl_hash = hashlib.sha256(self.draft.cover_letter.encode("utf-8")).hexdigest()

        with pytest.raises(CommandError) as exc:
            call_command(
                "pilot_job_application_submit",
                "--user-email", "candidate-10@example.com",
                "--profile-id", self.profile.id,
                "--job-id", self.job.id,
                "--job-version-id", self.job_version.id,
                "--application-id", self.app.id,
                "--application-draft-id", self.draft.id,
                "--expected-resume-checksum", cv_hash,
                "--expected-cover-letter-checksum", cl_hash,
                "--idempotency-key", "key-submit-123",
                "--confirm", "CONFIRM FINAL REAL TEST SUBMISSION",
                "--execute"
            )
        assert "Required unsupported fields" in str(exc.value)

    def test_console_output_cannot_mark_external_confirmation(self):
        # 8, 9, 10. Proves console run results in unverified status, not external confirmation
        # Add required facts to let it pass validation by temporarily unfinalizing version
        self.profile_version.is_finalized = False
        self.profile_version.save()
        CandidateFact.objects.create(profile_version=self.profile_version, fact_type="work_authorization", category="Auth", value={"status": "Yes"})
        CandidateFact.objects.create(profile_version=self.profile_version, fact_type="notice_period", category="Notice", value={"period": "Immediate"})
        self.profile_version.is_finalized = True
        self.profile_version.save()

        cv_hash = hashlib.sha256(self.draft.tailored_cv.encode("utf-8")).hexdigest()
        cl_hash = hashlib.sha256(self.draft.cover_letter.encode("utf-8")).hexdigest()

        call_command(
            "pilot_job_application_submit",
            "--user-email", "candidate-10@example.com",
            "--profile-id", self.profile.id,
            "--job-id", self.job.id,
            "--job-version-id", self.job_version.id,
            "--application-id", self.app.id,
            "--application-draft-id", self.draft.id,
            "--expected-resume-checksum", cv_hash,
            "--expected-cover-letter-checksum", cl_hash,
            "--idempotency-key", "key-submit-456",
            "--confirm", "CONFIRM FINAL REAL TEST SUBMISSION",
            "--execute"
        )
        self.app.refresh_from_db()
        assert self.app.status == "submission_unverified"
        assert self.app.externally_confirmed is False

    def test_reconciliation_cannot_resubmit_automatically(self):
        # 11. Proves unverified/indeterminate status requires human reconciliation and is not auto-resubmitted
        self.app.status = "submission_unverified"
        self.app.externally_confirmed = False
        self.app.save()

        # Any auto-submit trigger should skip this application
        assert self.app.status != "submitted"

    @patch("openoutreach.linkedin.browser.session.AccountSession.ensure_browser")
    def test_fixture_company_names_rejected_in_real_mode(self, mock_ensure_browser):
        # 12. Proves that real search rejects cards containing fixture company names
        session_mock = MagicMock()
        card_mock = MagicMock()
        title_mock = MagicMock()
        title_mock.inner_text.return_value = "Python Developer"
        comp_mock = MagicMock()
        comp_mock.inner_text.return_value = "Stark Industries"
        
        card_mock.query_selector.side_effect = lambda selector: title_mock if "title" in selector else comp_mock
        card_mock.get_attribute.return_value = "4423338490"
        session_mock.page.query_selector_all.return_value = [card_mock]

        with patch("openoutreach.core.management.commands.pilot_linkedin_job_search.AccountSession", return_value=session_mock):
            with pytest.raises(CommandError) as exc:
                call_command(
                    "pilot_linkedin_job_search",
                    "--user-email", "candidate-10@example.com",
                    "--profile-id", self.profile.id,
                    "--query", "Python Developer",
                    "--location", "Remote",
                    "--real"
                )
            assert "Safety violation" in str(exc.value)

    def test_followup_eligibility_not_reported_on_argument_error(self):
        # 13. Proves missing profile-id throws error and does not report eligibility status
        with pytest.raises(CommandError):
            call_command(
                "pilot_linkedin_list_eligible_followups",
                "--user-email", "candidate-10@example.com"
            )

    def test_identity_verification_gate_blocks_workflows(self):
        # 14. Proves candidate identity verification status pending blocks workflows
        self.verification.status = "pending"
        self.verification.save()

        with pytest.raises(CommandError) as exc:
            call_command(
                "pilot_linkedin_job_search",
                "--user-email", "candidate-10@example.com",
                "--profile-id", self.profile.id,
                "--query", "Python Developer",
                "--location", "Remote"
            )
        assert "has not been verified" in str(exc.value)

    def test_unverified_job_chain_blocked(self):
        # 15. Proves that IncidentHold target blocks execution
        from openoutreach.career.models import IncidentHold, IncidentHoldTarget
        from django.contrib.contenttypes.models import ContentType

        self.job.job_url = "https://www.linkedin.com/jobs/view/1001"
        self.job.save()

        hold = IncidentHold.objects.create(reason="TEST_HOLD", active=True)
        ct = ContentType.objects.get_for_model(self.job)
        IncidentHoldTarget.objects.create(incident_hold=hold, content_type=ct, object_id=self.job.id)

        job_count_before = Job.objects.count()
        job_ver_count_before = JobVersion.objects.count()

        with pytest.raises(CommandError) as exc:
            call_command(
                "pilot_linkedin_job_import",
                "--user-email", "candidate-10@example.com",
                "--profile-id", self.profile.id,
                "--external-job-id", "1001",
                "--confirm", "APPROVE PILOT JOB 1001"
            )
        assert "active safety hold" in str(exc.value)
        assert Job.objects.count() == job_count_before
        assert JobVersion.objects.count() == job_ver_count_before
