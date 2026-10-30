# openoutreach/career/management/commands/career_identity_corrected_version_create.py
from django.core.management.base import BaseCommand, CommandError
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from openoutreach.career.models import CandidateProfile
from openoutreach.career.service import create_corrected_pending_version_with_provenance

class Command(BaseCommand):
    help = "Creates a single corrected pending candidate profile version with enforceable source provenance"

    def add_arguments(self, parser):
        parser.add_argument("--profile-id", type=int, required=True)
        parser.add_argument("--historical-version-id", type=int, required=True)
        parser.add_argument("--document-version-id", type=int, required=True)
        parser.add_argument("--extraction-record-id", type=int, required=True)
        parser.add_argument("--corrected-full-name", type=str, required=True)
        parser.add_argument("--idempotency-key", type=str, required=True)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        profile_id = options["profile_id"]
        historical_version_id = options["historical_version_id"]
        document_version_id = options["document_version_id"]
        extraction_record_id = options["extraction_record_id"]
        corrected_full_name = options["corrected_full_name"]
        idempotency_key = options["idempotency_key"]
        dry_run = options["dry_run"]

        try:
            profile = CandidateProfile.objects.get(pk=profile_id)
        except CandidateProfile.DoesNotExist:
            raise CommandError(f"CandidateProfile ID {profile_id} does not exist.")

        # Assign creation to admin or owner
        operator = User.objects.filter(username="huntflow_admin").first()
        if not operator:
            operator = profile.user

        self.stdout.write("Initializing creation service transaction safe preflight checks...")

        try:
            corrected_version = create_corrected_pending_version_with_provenance(
                profile=profile,
                historical_version_id=historical_version_id,
                document_version_id=document_version_id,
                extraction_record_id=extraction_record_id,
                corrected_full_name=corrected_full_name,
                idempotency_key=idempotency_key,
                created_by=operator,
                dry_run=dry_run
            )
            
            if dry_run:
                self.stdout.write(self.style.SUCCESS("[DRY-RUN] Preflight assertions and validation passed. No writes committed."))
            else:
                self.stdout.write(self.style.SUCCESS(f"Successfully created corrected pending CandidateProfileVersion ID: {corrected_version.id}"))
                self.stdout.write(self.style.SUCCESS(f"Version Number: {corrected_version.version_number}"))
                self.stdout.write(self.style.SUCCESS(f"State: {corrected_version.state}"))
                
        except ValidationError as e:
            raise CommandError(f"Validation failed: {str(e.message if hasattr(e, 'message') else e)}")
        except Exception as e:
            raise CommandError(f"Unexpected execution error: {str(e)}")
