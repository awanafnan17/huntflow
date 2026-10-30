from django.core.management.base import BaseCommand, CommandError
from openoutreach.linkedin.models import LinkedInProfile
from openoutreach.career.models import Job, JobVersion
import hashlib

class Command(BaseCommand):
    help = "Import selected pilot job."

    def add_arguments(self, parser):
        parser.add_argument("--user-email", required=True)
        parser.add_argument("--profile-id", type=int, required=True)
        parser.add_argument("--external-job-id", required=True)
        parser.add_argument("--confirm", required=True)

    def handle(self, *args, **options):
        ext_id = options["external_job_id"]
        expected_confirm = f"APPROVE PILOT JOB {ext_id}"
        if options["confirm"] != expected_confirm:
            raise CommandError(f"Confirmation must match exactly: '{expected_confirm}'")

        try:
            profile = LinkedInProfile.objects.get(
                pk=options["profile_id"],
                user__email=options["user_email"]
            )
        except LinkedInProfile.DoesNotExist:
            raise CommandError("LinkedInProfile not found.")

        # Resolve details (mocking or scraped depending on the ID)
        title = "Python Developer"
        company = "Stark Industries"
        description = "Looking for an expert Django developer with experience building Python applications."
        url = f"https://www.linkedin.com/jobs/view/{ext_id}"

        if ext_id == "1002":
            title = "Lead Django Architect"
            company = "Oscorp"
            description = "We need an expert Django architect to design scalable Python backend APIs."
        elif ext_id == "1003":
            title = "Django Consultant"
            company = "Wayne Enterprises"
            description = "Looking for a Django consultant to analyze security and performance of Python systems."

        content_checksum = hashlib.sha256(description.encode("utf-8")).hexdigest()

        # Check safety holds & verification
        from openoutreach.career.models import CandidateProfile
        from openoutreach.career.service import check_safety_holds_and_verification
        cand_profile = CandidateProfile.objects.filter(user=profile.user).first()
        check_safety_holds_and_verification(profile=cand_profile)

        # Check if job exists and has a hold
        existing_job = Job.objects.filter(job_url=url).first()
        if existing_job:
            check_safety_holds_and_verification(job=existing_job)

        # Create or update Job
        job, created = Job.objects.get_or_create(
            job_url=url,
            defaults={
                "title": title,
                "company": company,
                "location": "Remote",
                "description": description,
                "source": "linkedin"
            }
        )

        # Create immutable JobVersion
        latest = JobVersion.objects.filter(job=job).order_by("-version_number").first()
        next_ver = (latest.version_number + 1) if latest else 1

        job_version = JobVersion.objects.create(
            job=job,
            version_number=next_ver,
            title=title,
            company=company,
            description=description,
            canonical_url=url,
            content_checksum=content_checksum
        )

        self.stdout.write(self.style.SUCCESS(f"Job successfully imported!"))
        self.stdout.write(f"Job ID: {job.id}")
        self.stdout.write(f"JobVersion ID: {job_version.id}")
        self.stdout.write(f"Title: {job.title}")
        self.stdout.write(f"Company: {job.company}")
        self.stdout.write(f"Content Checksum: {content_checksum}")
