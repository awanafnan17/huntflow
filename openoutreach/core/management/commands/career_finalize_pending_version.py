from django.core.management.base import BaseCommand, CommandError
from openoutreach.career.models import (
    CandidateProfile, CandidateProfileVersion, CandidateFact,
    CandidateFactDraft
)
from openoutreach.career.canonical import compute_canonical_checksum
from django.contrib.auth.models import User
from django.db import transaction
import uuid

class Command(BaseCommand):
    help = "Finalize pending fact drafts into a CandidateProfileVersion before approval."

    def add_arguments(self, parser):
        parser.add_argument("--user-email", required=True)
        parser.add_argument("--expected-fact-set-checksum", required=True)

    def handle(self, *args, **options):
        try:
            user = User.objects.get(email=options["user_email"])
        except User.DoesNotExist:
            raise CommandError("User not found.")

        try:
            profile = CandidateProfile.objects.get(user=user)
        except CandidateProfile.DoesNotExist:
            raise CommandError("CandidateProfile not found.")

        drafts = CandidateFactDraft.objects.filter(
            extraction_record__profile=profile,
            status="pending"
        ).order_by("fact_type", "id")

        if not drafts.exists():
            raise CommandError("No pending fact drafts to finalize.")

        # Compute canonical checksum from drafts
        actual_checksum = compute_canonical_checksum(drafts)
        if actual_checksum != options["expected_fact_set_checksum"]:
            raise CommandError(f"Fact set checksum mismatch! Expected: {options['expected_fact_set_checksum']}, Actual: {actual_checksum}")

        # Build snapshot list of dicts
        fact_snapshot_list = []
        for d in drafts:
            fact_snapshot_list.append({
                "fact_type": d.fact_type,
                "category": d.category,
                "value": d.value
            })

        with transaction.atomic():
            latest = CandidateProfileVersion.objects.filter(profile=profile).order_by("-version_number").first()
            next_ver = (latest.version_number + 1) if latest else 1

            profile_version = CandidateProfileVersion.objects.create(
                profile=profile,
                version_number=next_ver,
                full_name=profile.full_name,
                email=profile.email,
                summary=profile.summary,
                checksum=actual_checksum,
                fact_snapshot=fact_snapshot_list,
                is_finalized=False,
                state="draft",
                revision_token=uuid.uuid4().hex,
                created_by=user
            )

            # Promote facts
            for d in drafts:
                CandidateFact.objects.create(
                    profile_version=profile_version,
                    fact_type=d.fact_type,
                    category=d.category,
                    value=d.value
                )
                d.status = "approved"
                d.save()

            # Mark finalized and transition state to pending_approval
            profile_version.is_finalized = True
            profile_version.state = "pending_approval"
            profile_version.save()

        self.stdout.write(self.style.SUCCESS("Candidate profile version finalized successfully!"))
        self.stdout.write(f"CandidateProfileVersion ID: {profile_version.id}")
        self.stdout.write(f"Version Number: {profile_version.version_number}")
        self.stdout.write(f"Stored Checksum: {profile_version.checksum}")
        self.stdout.write(f"Revision Token: {profile_version.revision_token}")
