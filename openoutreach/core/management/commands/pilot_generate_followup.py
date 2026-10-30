from django.core.management.base import BaseCommand, CommandError
from openoutreach.linkedin.models import LinkedInProfile
import hashlib
import uuid

class Command(BaseCommand):
    help = "Generate one follow-up message draft."

    def add_arguments(self, parser):
        parser.add_argument("--user-email", required=True)
        parser.add_argument("--profile-id", type=int, required=True)
        parser.add_argument("--conversation-id", required=True)

    def handle(self, *args, **options):
        try:
            profile = LinkedInProfile.objects.get(
                pk=options["profile_id"],
                user__email=options["user_email"]
            )
        except LinkedInProfile.DoesNotExist:
            raise CommandError("LinkedInProfile not found.")

        msg = "Hi! I wanted to follow up on my previous note regarding the Django Developer role. I would love to connect."
        checksum = hashlib.sha256(msg.encode("utf-8")).hexdigest()
        idempotency_key = uuid.uuid4().hex

        self.stdout.write(f"Proposed follow-up: {msg}")
        self.stdout.write(f"Message checksum: {checksum}")
        self.stdout.write(f"Idempotency key: {idempotency_key}")
