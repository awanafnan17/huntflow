from django.core.management.base import BaseCommand, CommandError
from openoutreach.linkedin.models import LinkedInProfile
from openoutreach.career.models import Job

class Command(BaseCommand):
    help = "Search connection targets (recruiters, managers) for a job."

    def add_arguments(self, parser):
        parser.add_argument("--user-email", required=True)
        parser.add_argument("--profile-id", type=int, required=True)
        parser.add_argument("--job-id", type=int, required=True)
        parser.add_argument("--limit", type=int, default=5)
        parser.add_argument("--real", action="store_true")

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

        # Check safety holds & verification
        from openoutreach.career.models import CandidateProfile
        from openoutreach.career.service import check_safety_holds_and_verification
        cand_profile = CandidateProfile.objects.filter(user=profile.user).first()
        check_safety_holds_and_verification(profile=cand_profile, job=job)

        limit = options["limit"]
        company = job.company

        if options["real"]:
            raise CommandError("Real recruiter target searching is not supported. Safety block: test fixtures are prohibited in real mode.")

        # Deterministic test recruiter targets
        targets = [
            {
                "full_name": "Tony Stark",
                "first_name": "Tony",
                "public_id": "tonystark-test",
                "url": "https://www.linkedin.com/in/tonystark-test",
                "title": "Head of Recruiting",
                "company": company,
                "relationship": "Hiring Manager",
                "state": "NOT_CONNECTED",
                "pending": False
            },
            {
                "full_name": "Pepper Potts",
                "first_name": "Pepper",
                "public_id": "pepperpotts-test",
                "url": "https://www.linkedin.com/in/pepperpotts-test",
                "title": "Talent Acquisition Partner",
                "company": company,
                "relationship": "Recruiter",
                "state": "NOT_CONNECTED",
                "pending": False
            },
            {
                "full_name": "Happy Hogan",
                "first_name": "Happy",
                "public_id": "happyhogan-test",
                "url": "https://www.linkedin.com/in/happyhogan-test",
                "title": "HR Specialist",
                "company": company,
                "relationship": "HR Coordinator",
                "state": "NOT_CONNECTED",
                "pending": False
            }
        ][:limit]

        for t in targets:
            self.stdout.write(f"Full name: {t['full_name']}")
            self.stdout.write(f"First name: {t['first_name']}")
            self.stdout.write(f"Public ID: {t['public_id']}")
            self.stdout.write(f"Profile URL: {t['url']}")
            self.stdout.write(f"Title: {t['title']}")
            self.stdout.write(f"Company: {t['company']}")
            self.stdout.write(f"Relationship to selected job: {t['relationship']}")
            self.stdout.write(f"Connection state: {t['state']}")
            self.stdout.write(f"Pending invitation state: {'YES' if t['pending'] else 'NO'}")
            self.stdout.write(f"Duplicate action state: NO")
            self.stdout.write("")
