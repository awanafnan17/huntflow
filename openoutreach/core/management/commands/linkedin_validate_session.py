from django.core.management.base import BaseCommand, CommandError
from openoutreach.linkedin.models import LinkedInProfile
from openoutreach.linkedin.session_manager import run_session_health_check

class Command(BaseCommand):
    help = "Validate LinkedIn session state."

    def add_arguments(self, parser):
        parser.add_argument("--user-email", required=True)
        parser.add_argument("--profile-id", type=int, required=True)
        parser.add_argument("--real", action="store_true")

    def handle(self, *args, **options):
        try:
            profile = LinkedInProfile.objects.get(
                pk=options["profile_id"],
                user__email=options["user_email"]
            )
        except LinkedInProfile.DoesNotExist:
            raise CommandError("LinkedInProfile not found for specified ID and user email.")

        if options["real"]:
            run_session_health_check(profile)
            profile.refresh_from_db()

        status = profile.session_status
        login_page_detected = "login" in str(profile.session_error_details).lower()
        checkpoint_detected = "checkpoint" in str(profile.session_error_details).lower()
        verification_detected = "verification" in str(profile.session_error_details).lower()
        restriction_detected = "restricted" in str(profile.session_error_details).lower()

        self.stdout.write(f"owner_email: {profile.user.email}")
        self.stdout.write(f"profile_id: {profile.pk}")
        self.stdout.write(f"session_status: {status}")
        self.stdout.write(f"login_page_detected: {'true' if login_page_detected else 'false'}")
        self.stdout.write(f"checkpoint_detected: {'true' if checkpoint_detected else 'false'}")
        self.stdout.write(f"verification_detected: {'true' if verification_detected else 'false'}")
        self.stdout.write(f"restriction_detected: {'true' if restriction_detected else 'false'}")
