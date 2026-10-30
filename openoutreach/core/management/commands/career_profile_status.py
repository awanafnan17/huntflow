from django.core.management.base import BaseCommand, CommandError
from openoutreach.career.models import CandidateProfile, CandidateProfileVersion
from django.contrib.auth.models import User

class Command(BaseCommand):
    help = "Show profile status and versions of the candidate."

    def add_arguments(self, parser):
        parser.add_argument("--user-email", required=True)

    def handle(self, *args, **options):
        try:
            profile = CandidateProfile.objects.get(user__email=options["user_email"])
        except CandidateProfile.DoesNotExist:
            raise CommandError("CandidateProfile not found.")

        self.stdout.write(f"CandidateProfile ID: {profile.id}")
        self.stdout.write(f"Name: {profile.full_name}")
        self.stdout.write(f"Email: {profile.email}")

        versions = CandidateProfileVersion.objects.filter(profile=profile).order_by("-version_number")
        self.stdout.write(f"Total Versions: {versions.count()}")
        for v in versions:
            has_approval = hasattr(v, "approval")
            self.stdout.write(f"  v{v.version_number} - ID: {v.id} - Approved: {has_approval} - Facts: {v.facts.count()}")
