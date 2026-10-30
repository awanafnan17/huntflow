from django.core.management.base import BaseCommand, CommandError
from openoutreach.linkedin.models import LinkedInProfile
from openoutreach.linkedin.session_manager import manual_login_linkedin

class Command(BaseCommand):
    help = "Launch manual login window to authenticate LinkedIn profile."

    def add_arguments(self, parser):
        parser.add_argument("--user-email", required=True)
        parser.add_argument("--profile-id", type=int, required=True)

    def handle(self, *args, **options):
        try:
            profile = LinkedInProfile.objects.get(
                pk=options["profile_id"],
                user__email=options["user_email"]
            )
        except LinkedInProfile.DoesNotExist:
            raise CommandError("LinkedInProfile not found.")

        self.stdout.write("Launching browser manual login flow... Please complete login on screen.")
        manual_login_linkedin(profile)
        profile.refresh_from_db()
        self.stdout.write(f"Login complete. Session status is now: {profile.session_status}")
