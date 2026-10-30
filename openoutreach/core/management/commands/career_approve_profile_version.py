from django.core.management.base import BaseCommand, CommandError
from openoutreach.career.models import (
    CandidateProfile, CandidateProfileVersion, CandidateProfileApproval
)
from openoutreach.career.canonical import compute_canonical_checksum
from django.contrib.auth.models import User
from django.db import transaction
import uuid
import json

class Command(BaseCommand):
    help = "Approve a pre-finalized CandidateProfileVersion atomically."

    def add_arguments(self, parser):
        parser.add_argument("--user-email", required=True)
        parser.add_argument("--profile-version-id", type=int, required=True)
        parser.add_argument("--expected-fact-set-checksum", required=True)
        parser.add_argument("--revision-token", required=True)
        parser.add_argument("--confirm", required=True)
        parser.add_argument("--idempotency-key", required=False)

    def handle(self, *args, **options):
        if options["confirm"] != "APPROVE CANDIDATE PROFILE":
            raise CommandError("Confirmation prompt must match: 'APPROVE CANDIDATE PROFILE'")

        try:
            user = User.objects.get(email=options["user_email"])
        except User.DoesNotExist:
            raise CommandError("User not found.")

        # Process within atomic transaction with select_for_update locking and optimistic state transition
        with transaction.atomic():
            try:
                version = CandidateProfileVersion.objects.select_for_update().get(
                    pk=options["profile_version_id"],
                    profile__user=user
                )
            except CandidateProfileVersion.DoesNotExist:
                raise CommandError("CandidateProfileVersion not found or does not belong to specified user.")

            if not version.is_finalized:
                raise CommandError("CandidateProfileVersion is not finalized.")

            # Idempotency support
            if hasattr(version, "approval") or version.state == "approved":
                if options.get("idempotency_key") and hasattr(version, "approval") and version.approval.idempotency_key == options["idempotency_key"]:
                    self.stdout.write(self.style.SUCCESS("Candidate profile version already approved with same idempotency key."))
                    self.stdout.write(f"CandidateProfileVersion ID: {version.id}")
                    self.stdout.write(f"CandidateProfileApproval ID: {version.approval.id}")
                    return
                raise CommandError("CandidateProfileVersion is already approved.")

            # Recompute canonical checksums using shared algorithm
            recomputed_snapshot_checksum = compute_canonical_checksum(version.fact_snapshot)
            recomputed_facts_checksum = compute_canonical_checksum(list(version.facts.all()))

            # Verify representation equality (JSON snapshot vs CandidateFact rows)
            snapshot_items = version.fact_snapshot
            fact_rows = list(version.facts.all())

            if len(snapshot_items) != len(fact_rows):
                raise CommandError("Number of facts in snapshot does not match CandidateFact rows.")

            snapshot_sigs = sorted([f"{item['fact_type']}:{item['category']}:{json.dumps(item['value'], sort_keys=True, ensure_ascii=False)}" for item in snapshot_items])
            fact_sigs = sorted([f"{f.fact_type}:{f.category}:{json.dumps(f.value, sort_keys=True, ensure_ascii=False)}" for f in fact_rows])

            if snapshot_sigs != fact_sigs:
                raise CommandError("Content mismatch between JSON snapshot and CandidateFact rows.")

            # Verify all four checksums match
            if options["expected_fact_set_checksum"] != version.checksum:
                raise CommandError(f"Supplied checksum mismatch! Supplied: {options['expected_fact_set_checksum']}, Stored: {version.checksum}")

            if recomputed_snapshot_checksum != version.checksum:
                raise CommandError(f"Snapshot checksum mismatch! Recomputed snapshot: {recomputed_snapshot_checksum}, Stored: {version.checksum}")

            if recomputed_facts_checksum != version.checksum:
                raise CommandError(f"CandidateFact rows checksum mismatch! Recomputed facts: {recomputed_facts_checksum}, Stored: {version.checksum}")

            # Perform optimistic concurrency and state transition update on the row
            updated = CandidateProfileVersion.objects.filter(
                pk=version.pk,
                profile__user=user,
                is_finalized=True,
                state="pending_approval",
                checksum=version.checksum,
                revision_token=options["revision_token"]
            ).update(
                state="approved",
                revision_token=uuid.uuid4().hex
            )

            if updated != 1:
                raise CommandError("Concurrency update failed! The version may have been modified or revision token is stale.")

            # Create approval
            ik = options.get("idempotency_key") or uuid.uuid4().hex
            approval = CandidateProfileApproval.objects.create(
                profile_version=version,
                approved_by=user,
                approval_note=f"Approved via terminal. Checksum: {version.checksum}",
                idempotency_key=ik
            )

            # Set active version on profile atomically
            profile = version.profile
            profile.active_version = version
            profile.save()

        self.stdout.write(self.style.SUCCESS("Candidate profile version successfully approved!"))
        self.stdout.write(f"CandidateProfileVersion ID: {version.id}")
        self.stdout.write(f"Version Number: {version.version_number}")
        self.stdout.write(f"CandidateProfileApproval ID: {approval.id}")
