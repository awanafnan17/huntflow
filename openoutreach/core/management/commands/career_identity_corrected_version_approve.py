from django.core.management.base import BaseCommand, CommandError
from openoutreach.career.models import CandidateProfileVersion, CandidateProfileApproval
from openoutreach.career.service import approve_corrected_profile_version
from django.contrib.auth.models import User

class Command(BaseCommand):
    help = "Approve a pending corrected CandidateProfileVersion atomically."

    def add_arguments(self, parser):
        parser.add_argument("--user-email", required=True)
        parser.add_argument("--version-id", type=int, required=True)
        parser.add_argument("--idempotency-key", required=False)
        parser.add_argument("--confirm", required=True)

    def handle(self, *args, **options):
        if options["confirm"] != "APPROVE CORRECTED IDENTITY VERSION":
            raise CommandError("Confirmation prompt must match: 'APPROVE CORRECTED IDENTITY VERSION'")

        try:
            user = User.objects.get(email=options["user_email"])
        except User.DoesNotExist:
            raise CommandError("User not found.")

        try:
            profile_ver = CandidateProfileVersion.objects.get(
                pk=options["version_id"],
                profile__user=user
            )
        except CandidateProfileVersion.DoesNotExist:
            raise CommandError("CandidateProfileVersion not found or does not belong to specified user.")

        profile = profile_ver.profile
        old_active_id = profile.active_version.id if profile.active_version else None
        old_approval_count = CandidateProfileApproval.objects.filter(profile_version=profile_ver).count()

        # Print BEFORE state
        self.stdout.write("BEFORE")
        self.stdout.write(f"Version ID: {profile_ver.id}")
        self.stdout.write(f"state: {profile_ver.state}")
        self.stdout.write(f"finalized: {profile_ver.is_finalized}")
        self.stdout.write(f"approval_count: {old_approval_count}")
        self.stdout.write(f"active_version: {old_active_id}\n")

        # Execute approval
        from django.core.exceptions import ValidationError
        try:
            approve_corrected_profile_version(
                profile_version=profile_ver,
                approved_by=user,
                idempotency_key=options.get("idempotency_key")
            )
        except ValidationError as e:
            raise CommandError(str(e))

        # Re-fetch values to print AFTER state
        profile_ver.refresh_from_db()
        profile.refresh_from_db()
        new_active_id = profile.active_version.id if profile.active_version else None
        new_approval_count = CandidateProfileApproval.objects.filter(profile_version=profile_ver).count()

        self.stdout.write("AFTER")
        self.stdout.write(f"Version ID: {profile_ver.id}")
        self.stdout.write(f"state: {profile_ver.state}")
        self.stdout.write(f"finalized: {profile_ver.is_finalized}")
        self.stdout.write(f"approval_count: {new_approval_count}")
        self.stdout.write(f"active_version: {new_active_id}")
