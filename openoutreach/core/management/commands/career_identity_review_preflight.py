from django.core.management.base import BaseCommand, CommandError
from openoutreach.career.models import CandidateProfile
from openoutreach.career.service import preview_identity_resolution

class Command(BaseCommand):
    help = "Controlled identity review preflight check."

    def add_arguments(self, parser):
        parser.add_argument("--user-email")
        parser.add_argument("--profile-id", type=int)

    def handle(self, *args, **options):
        if not options["user_email"] and not options["profile_id"]:
            raise CommandError("Provide either --user-email or --profile-id.")

        profile = None
        if options["profile_id"]:
            profile = CandidateProfile.objects.filter(pk=options["profile_id"]).first()
        else:
            profile = CandidateProfile.objects.filter(user__email=options["user_email"]).first()

        if not profile:
            raise CommandError("Candidate profile not found.")

        # Preview resolution
        res = preview_identity_resolution(profile)

        # Output exactly as requested
        self.stdout.write("CURRENT STORED IDENTITY:")
        self.stdout.write(f"CandidateProfile ID: {profile.id}")
        self.stdout.write(f"Owner: {profile.user.username if profile.user else 'None'}")
        self.stdout.write(f"Current full_name: {res['stored_identity']['full_name']}")
        self.stdout.write(f"Active version ID: {res['stored_identity']['active_version_id']}")
        self.stdout.write(f"Active version checksum: {res['stored_identity']['active_version_checksum']}")
        self.stdout.write(f"Active version state: {res['stored_identity']['active_version_state']}\n")

        self.stdout.write("EXTRACTED DOCUMENT IDENTITY:")
        self.stdout.write(f"Source document ID: {res['extracted_identity']['source_document_id']}")
        self.stdout.write(f"Source document version ID: {res['extracted_identity']['source_document_version_id']}")
        self.stdout.write(f"Extracted name: {res['extracted_identity']['extracted_name']}")
        self.stdout.write("Evidence excerpt:")
        self.stdout.write(res['extracted_identity']['evidence_excerpt'] + "\n")

        self.stdout.write("VERIFICATION STATUS:")
        self.stdout.write(f"Verified: {res['verification_state']['is_verified']}")
        self.stdout.write(f"Verified name: {res['verification_state']['verified_name']}")
        self.stdout.write(f"Existing verifications count: {len(res['verification_state']['verifications'])}\n")

        self.stdout.write("PROPOSED CORRECTION:")
        self.stdout.write(res['proposed_correction'] + "\n")

        self.stdout.write("DATABASE MUTATIONS:")
        self.stdout.write("NONE")
