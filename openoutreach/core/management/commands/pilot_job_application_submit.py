from django.core.management.base import BaseCommand, CommandError
from openoutreach.linkedin.models import LinkedInProfile
from openoutreach.career.models import Job, JobApplication, ApplicationDraft
import hashlib

class Command(BaseCommand):
    help = "Submit job application with final validation checks."

    def add_arguments(self, parser):
        parser.add_argument("--execute", action="store_true")
        parser.add_argument("--user-email", required=True)
        parser.add_argument("--profile-id", type=int, required=True)
        parser.add_argument("--job-id", type=int, required=True)
        parser.add_argument("--job-version-id", type=int, required=True)
        parser.add_argument("--application-id", type=int, required=True)
        parser.add_argument("--application-draft-id", type=int, required=True)
        parser.add_argument("--expected-resume-checksum", required=True)
        parser.add_argument("--expected-cover-letter-checksum", required=True)
        parser.add_argument("--idempotency-key", required=True)
        parser.add_argument("--confirm", required=True)

    def handle(self, *args, **options):
        if options["confirm"] != "CONFIRM FINAL REAL TEST SUBMISSION":
            raise CommandError("Confirmation must match exactly: 'CONFIRM FINAL REAL TEST SUBMISSION'")

        if not options["execute"]:
            raise CommandError("Must specify --execute to submit application.")

        try:
            profile = LinkedInProfile.objects.get(
                pk=options["profile_id"],
                user__email=options["user_email"]
            )
        except LinkedInProfile.DoesNotExist:
            raise CommandError("LinkedInProfile not found.")

        try:
            app = JobApplication.objects.get(pk=options["application_id"])
        except JobApplication.DoesNotExist:
            raise CommandError("JobApplication not found.")

        try:
            draft = ApplicationDraft.objects.get(pk=options["application_draft_id"])
        except ApplicationDraft.DoesNotExist:
            raise CommandError("ApplicationDraft not found.")

        # Check if required facts are supported in candidate profile facts
        profile_version = draft.profile_version
        if profile_version:
            fact_types = set(profile_version.facts.values_list("fact_type", flat=True))
            if "work_authorization" not in fact_types or "notice_period" not in fact_types:
                raise CommandError("Required unsupported fields: 'work_authorization' or 'notice_period' is missing from approved facts, blocking submission.")

        # Check safety holds & verification
        from openoutreach.career.service import check_safety_holds_and_verification
        check_safety_holds_and_verification(job_application=app, application_draft=draft, job=app.job)

        # Check checksums
        cv_checksum = hashlib.sha256(draft.tailored_cv.encode("utf-8")).hexdigest()
        cl_checksum = hashlib.sha256(draft.cover_letter.encode("utf-8")).hexdigest()

        if cv_checksum != options["expected_resume_checksum"]:
            raise CommandError("Resume checksum mismatch.")
        if cl_checksum != options["expected_cover_letter_checksum"]:
            raise CommandError("Cover letter checksum mismatch.")

        # A submission command without authoritative evidence becomes indeterminate/unverified
        app.status = "submission_unverified"
        app.externally_confirmed = False
        app.save()

        self.stdout.write(self.style.SUCCESS("Job application submission process run successfully!"))
        self.stdout.write(f"Application ID: {app.id}")
        self.stdout.write(f"Status: {app.status}")
        self.stdout.write(f"Externally Confirmed: {app.externally_confirmed}")
        self.stdout.write("Reconciliation Required: YES")
