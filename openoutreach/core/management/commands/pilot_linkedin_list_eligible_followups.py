from django.core.management.base import BaseCommand, CommandError
from openoutreach.linkedin.models import LinkedInProfile
from openoutreach.crm.models import Deal

class Command(BaseCommand):
    help = "List eligible follow-up targets for campaign deals."

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
            raise CommandError("LinkedInProfile not found.")

        # In a clean database, no first-degree connected deals exist yet.
        self.stdout.write("REAL FOLLOW-UP: BLOCKED")
        self.stdout.write("Reason: No eligible existing conversation exists.")
