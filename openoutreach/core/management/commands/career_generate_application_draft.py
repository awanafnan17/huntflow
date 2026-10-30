from django.core.management.base import BaseCommand, CommandError
from openoutreach.career.models import (
    CandidateProfileVersion, JobVersion, JobMatch, JobApplication,
    ApplicationDraft, ApprovalItem
)
from openoutreach.career.drafting import get_drafting_provider
import hashlib

class Command(BaseCommand):
    help = "Generate tailored application draft CV and cover letter."

    def add_arguments(self, parser):
        parser.add_argument("--user-email", required=True)
        parser.add_argument("--candidate-profile-version-id", type=int, required=True)
        parser.add_argument("--job-version-id", type=int, required=True)
        parser.add_argument("--job-match-id", type=int, required=True)
        parser.add_argument("--provider", default="deterministic")

    def handle(self, *args, **options):
        try:
            profile_ver = CandidateProfileVersion.objects.get(
                pk=options["candidate_profile_version_id"],
                profile__user__email=options["user_email"]
            )
        except CandidateProfileVersion.DoesNotExist:
            raise CommandError("CandidateProfileVersion not found.")

        if profile_ver.state != "approved":
            raise CommandError("CandidateProfileVersion is not approved.")

        try:
            job_ver = JobVersion.objects.get(pk=options["job_version_id"])
        except JobVersion.DoesNotExist:
            raise CommandError("JobVersion not found.")

        try:
            match = JobMatch.objects.get(pk=options["job_match_id"])
        except JobMatch.DoesNotExist:
            raise CommandError("JobMatch not found.")

        # Check safety holds & verification
        from openoutreach.career.service import check_safety_holds_and_verification
        check_safety_holds_and_verification(profile_version=profile_ver, job_version=job_ver, job=job_ver.job, job_match=match)

        # Resolve or create JobApplication
        app, created = JobApplication.objects.get_or_create(
            candidate_profile=profile_ver.profile,
            job=job_ver.job,
            defaults={
                "status": "draft",
                "audit_log": []
            }
        )

        provider = get_drafting_provider(offline=True)
        cv, letter, struct_cv, trace_map = provider.generate_draft(profile_ver, job_ver.job)

        latest_draft = app.drafts.order_by("-version_number").first()
        next_ver = (latest_draft.version_number + 1) if latest_draft else 1

        draft = ApplicationDraft.objects.create(
            application=app,
            profile_version=profile_ver,
            version_number=next_ver,
            tailored_cv=cv,
            cover_letter=letter,
            structured_cv=struct_cv,
            traceability_map=trace_map,
            created_by=profile_ver.profile.user
        )

        # Clear or create ApprovalItem
        ApprovalItem.objects.update_or_create(
            application=app,
            defaults={
                "draft": draft,
                "status": "pending"
            }
        )

        # Sync legacy cache fields
        app.tailored_cv = cv
        app.cover_letter = letter
        app.save()

        self.stdout.write(self.style.SUCCESS("Application draft generated!"))
        self.stdout.write(f"JobApplication ID: {app.id}")
        self.stdout.write(f"ApplicationDraft ID: {draft.id}")
        self.stdout.write(f"Draft Version: {draft.version_number}")
