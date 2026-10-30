from django.core.management.base import BaseCommand, CommandError
from openoutreach.linkedin.models import LinkedInProfile, ActionLog
from openoutreach.career.models import (
    CandidateProfile, CandidateProfileVersion, CandidateFact,
    CandidateDocument, CandidateDocumentVersion, Job, JobVersion, JobMatch,
    JobApplication, ApplicationDraft, ApprovalItem
)
from openoutreach.core.models import Task

class Command(BaseCommand):
    help = "Print database counts snapshot."

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

        self.stdout.write(f"CandidateProfile count: {CandidateProfile.objects.count()}")
        self.stdout.write(f"CandidateProfileVersion count: {CandidateProfileVersion.objects.count()}")
        self.stdout.write(f"CandidateFact count: {CandidateFact.objects.count()}")
        self.stdout.write(f"CandidateDocument count: {CandidateDocument.objects.count()}")
        self.stdout.write(f"CandidateDocumentVersion count: {CandidateDocumentVersion.objects.count()}")
        self.stdout.write(f"Job count: {Job.objects.count()}")
        self.stdout.write(f"JobVersion count: {JobVersion.objects.count()}")
        self.stdout.write(f"JobMatch count: {JobMatch.objects.count()}")
        self.stdout.write(f"JobApplication count: {JobApplication.objects.count()}")
        self.stdout.write(f"ApplicationDraft count: {ApplicationDraft.objects.count()}")
        self.stdout.write(f"ApprovalItem count: {ApprovalItem.objects.count()}")
        self.stdout.write(f"LinkedIn ActionLog count: {ActionLog.objects.count()}")
        self.stdout.write(f"Pending tasks count: {Task.objects.filter(status='pending').count()}")
        self.stdout.write(f"Failed tasks count: {Task.objects.filter(status='failed').count()}")
