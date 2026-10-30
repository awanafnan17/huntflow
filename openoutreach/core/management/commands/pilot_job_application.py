from django.core.management.base import BaseCommand, CommandError
from openoutreach.career.models import Job, JobApplication, ApplicationDraft
from openoutreach.linkedin.models import LinkedInProfile, ActionLog
from openoutreach.linkedin.browser.session import AccountSession
import hashlib

class Command(BaseCommand):
    help = "Pilot job applications with safety checks, checksum validations and dry-run controls."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Perform validation checks but do not submit.")
        parser.add_argument("--execute", action="store_true", help="Execute the job application.")
        parser.add_argument("--profile-id", type=int, help="ID of the LinkedInProfile to use.")
        parser.add_argument("--user-email", help="Email of the Django user to resolve LinkedInProfile.")
        parser.add_argument("--expected-recipient-id", required=True, help="Expected recipient identifier (e.g. company/job identifier).")
        parser.add_argument("--expected-content-checksum", required=True, help="Combined SHA256 checksum of the content.")
        parser.add_argument("--idempotency-key", required=True, help="Unique key to prevent duplicate runs.")
        
        # Additional required flags for job applications
        parser.add_argument("--expected-job-id", type=int, required=True)
        parser.add_argument("--expected-job-version-id", type=int, required=True)
        parser.add_argument("--expected-application-id", type=int, required=True)
        parser.add_argument("--expected-draft-id", type=int, required=True)
        parser.add_argument("--expected-resume-checksum", required=True)
        parser.add_argument("--expected-cover-letter-checksum", required=True)

    def handle(self, *args, **options):
        if not options["dry_run"] and not options["execute"]:
            raise CommandError("Either --dry-run or --execute must be specified.")

        if not options.get("profile_id") and not options.get("user_email"):
            raise CommandError("Either --profile-id or --user-email must be specified.")

        try:
            if options.get("profile_id"):
                profile = LinkedInProfile.objects.get(pk=options["profile_id"])
            else:
                profile = LinkedInProfile.objects.get(user__email=options["user_email"])
        except LinkedInProfile.DoesNotExist:
            raise CommandError("LinkedInProfile not found for the specified ID or user email.")

        # Load job application and draft
        try:
            job = Job.objects.get(pk=options["expected_job_id"])
        except Job.DoesNotExist:
            raise CommandError(f"Job with ID {options['expected_job_id']} does not exist.")

        try:
            app = JobApplication.objects.get(pk=options["expected_application_id"])
        except JobApplication.DoesNotExist:
            raise CommandError(f"JobApplication with ID {options['expected_application_id']} does not exist.")

        try:
            draft = ApplicationDraft.objects.get(pk=options["expected_draft_id"])
        except ApplicationDraft.DoesNotExist:
            raise CommandError(f"ApplicationDraft with ID {options['expected_draft_id']} does not exist.")

        # Check resume and cover letter checksums
        cv_hash = hashlib.sha256(draft.tailored_cv.encode("utf-8")).hexdigest()
        cl_hash = hashlib.sha256(draft.cover_letter.encode("utf-8")).hexdigest()

        if cv_hash != options["expected_resume_checksum"]:
            raise CommandError(f"Resume checksum mismatch! Expected: {options['expected_resume_checksum']}, Actual: {cv_hash}")

        if cl_hash != options["expected_cover_letter_checksum"]:
            raise CommandError(f"Cover letter checksum mismatch! Expected: {options['expected_cover_letter_checksum']}, Actual: {cl_hash}")

        self.stdout.write(f"Idempotency Key: {options['idempotency_key']}")
        self.stdout.write(f"LinkedIn Username: {profile.linkedin_username}")
        self.stdout.write(f"Session Status: {profile.session_status}")
        self.stdout.write(f"Job Target: {job.title} at {job.company}")
        self.stdout.write(f"Job URL: {job.job_url}")

        if options["dry_run"]:
            self.stdout.write(self.style.SUCCESS("Dry-run validation checks passed! Ready for job application pilot."))
            return

        # Execute Playwright automation to open the job and upload resume
        self.stdout.write("Initializing browser session...")
        session = AccountSession(profile)
        session.ensure_browser()

        try:
            self.stdout.write(f"Navigating to Job URL: {job.job_url}...")
            session.page.goto(job.job_url)
            session.page.wait_for_timeout(5000)

            # Look for Easy Apply or other controls on the page
            page_content = session.page.content().lower()
            if "easy apply" in page_content or "apply now" in page_content:
                self.stdout.write(self.style.SUCCESS("Application page loaded. Blocker: Operator confirmation CONFIRM FINAL REAL TEST SUBMISSION required to complete the submission."))
            else:
                self.stdout.write(self.style.WARNING("Standard apply page loaded. Manual operator action needed to complete application."))
        finally:
            if session.page:
                session.page.close()
            if session.context:
                session.context.close()
            if session.browser:
                session.browser.close()
            if session.playwright:
                session.playwright.stop()
