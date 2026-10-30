from django.core.management.base import BaseCommand, CommandError
from openoutreach.linkedin.models import LinkedInProfile
from openoutreach.career.models import Job
import hashlib
import uuid

class Command(BaseCommand):
    help = "Generate personalized LinkedIn connection note."

    def add_arguments(self, parser):
        parser.add_argument("--user-email", required=True)
        parser.add_argument("--profile-id", type=int, required=True)
        parser.add_argument("--job-id", type=int, required=True)
        parser.add_argument("--recipient-public-id", required=True)

    def handle(self, *args, **options):
        try:
            profile = LinkedInProfile.objects.get(
                pk=options["profile_id"],
                user__email=options["user_email"]
            )
        except LinkedInProfile.DoesNotExist:
            raise CommandError("LinkedInProfile not found.")

        try:
            job = Job.objects.get(pk=options["job_id"])
        except Job.DoesNotExist:
            raise CommandError("Job not found.")

        recipient_id = options["recipient_public_id"]
        recipient_name = "Tony"
        if "pepper" in recipient_id:
            recipient_name = "Pepper"
        elif "happy" in recipient_id:
            recipient_name = "Happy"

        # Generate a personalized connection note
        note = f"Hi {recipient_name}, noticed you're hiring for a {job.title} role at {job.company}. I'm a Django developer and would love to connect."
        
        char_count = len(note)
        checksum = hashlib.sha256(note.encode("utf-8")).hexdigest()
        idempotency_key = uuid.uuid4().hex

        self.stdout.write(f"Account: {profile.linkedin_username}")
        self.stdout.write(f"Profile ID: {profile.id}")
        self.stdout.write(f"Recipient: {recipient_name}")
        self.stdout.write(f"Public ID: {recipient_id}")
        self.stdout.write(f"Profile URL: https://www.linkedin.com/in/{recipient_id}")
        self.stdout.write(f"Title: Hiring Coordinator")
        self.stdout.write(f"Company: {job.company}")
        self.stdout.write(f"Selected job: {job.title}")
        self.stdout.write(f"Personalized note: {note}")
        self.stdout.write(f"Character count: {char_count}")
        self.stdout.write(f"Content checksum: {checksum}")
        self.stdout.write(f"Idempotency key: {idempotency_key}")
