from django.core.management.base import BaseCommand, CommandError
from openoutreach.linkedin.models import LinkedInProfile

class Command(BaseCommand):
    help = "Show session status info of LinkedInProfile."

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

        self.stdout.write(f"Session status: {profile.session_status}")
        self.stdout.write(f"Last validated at: {profile.last_validated_at}")
        self.stdout.write(f"Last authenticated at: {profile.last_authenticated_at}")
        self.stdout.write(f"Error details: {profile.session_error_details}")
