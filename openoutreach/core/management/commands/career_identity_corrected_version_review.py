from django.core.management.base import BaseCommand, CommandError
from openoutreach.career.models import CandidateProfileVersion
from openoutreach.career.service import review_corrected_profile_version

class Command(BaseCommand):
    help = "READ-ONLY CandidateProfileVersion safety audit."

    def add_arguments(self, parser):
        parser.add_argument("--user-email", required=True)
        parser.add_argument("--version-id", type=int, required=True)

    def handle(self, *args, **options):
        try:
            profile_ver = CandidateProfileVersion.objects.get(
                pk=options["version_id"],
                profile__user__email=options["user_email"]
            )
        except CandidateProfileVersion.DoesNotExist:
            raise CommandError("CandidateProfileVersion not found or permission denied.")

        if profile_ver.state == "approved" or profile_ver.is_finalized:
            raise CommandError(f"Version ID {profile_ver.id} is approved historical identity version. Use a pending corrected version only.")

        from django.core.exceptions import ValidationError
        try:
            res = review_corrected_profile_version(profile_ver)
        except ValidationError as e:
            raise CommandError(str(e))

        self.stdout.write("VERSION STATUS")
        self.stdout.write(f"CandidateProfileVersion ID: {res['version_status']['profile_version_id']}")
        self.stdout.write(f"Profile ID: {res['version_status']['profile_id']}")
        self.stdout.write(f"Version number: {res['version_status']['version_number']}")
        self.stdout.write(f"State: {res['version_status']['state']}")
        self.stdout.write(f"Finalized: {res['version_status']['finalized']}")
        self.stdout.write(f"Checksum: {res['version_status']['checksum']}")
        self.stdout.write(f"Version role: {res['version_role']}")
        self.stdout.write(f"Created timestamp: {res['version_status']['created_at']}\n")

        self.stdout.write("IDENTITY COMPARISON")
        self.stdout.write(f"Old identity: {res['identity_comparison']['old_identity']}")
        self.stdout.write(f"New identity: {res['identity_comparison']['new_identity']}")
        self.stdout.write(f"Identity changed: {res['identity_comparison']['identity_changed']}\n")

        self.stdout.write("FACT ALIGNMENT")
        self.stdout.write(f"Total source facts: {res['fact_comparison']['total_source_facts']}")
        self.stdout.write(f"Total corrected facts: {res['fact_comparison']['total_corrected_facts']}")
        self.stdout.write(f"Added facts: {len(res['fact_comparison']['added_facts'])}")
        self.stdout.write(f"Removed facts: {len(res['fact_comparison']['removed_facts'])}")
        self.stdout.write(f"Modified facts: {len(res['fact_comparison']['modified_facts'])}")
        if res['fact_comparison']['added_facts']:
            self.stdout.write(f"  Added list: {res['fact_comparison']['added_facts']}")
        if res['fact_comparison']['removed_facts']:
            self.stdout.write(f"  Removed list: {res['fact_comparison']['removed_facts']}")
        if res['fact_comparison']['modified_facts']:
            self.stdout.write(f"  Modified list: {res['fact_comparison']['modified_facts']}\n")
        else:
            self.stdout.write("")

        self.stdout.write("CONTAMINATION CHECK")
        self.stdout.write(f"Forbidden identity: {res['contamination_scan']['forbidden_identity']}")
        self.stdout.write(f"Contamination found: {res['contamination_scan']['contamination_found']}\n")

        self.stdout.write("DOCUMENT REFERENCE CHECK")
        self.stdout.write(f"References ApplicationDraft ID 1: {res['document_reference_scan']['references_application_draft_1']}")
        self.stdout.write(f"References old resume: {res['document_reference_scan']['references_old_resume']}")
        self.stdout.write(f"References old cover letter: {res['document_reference_scan']['references_old_cover_letter']}")
        self.stdout.write(f"Has references: {res['document_reference_scan']['has_references']}\n")

        self.stdout.write("INCIDENT HOLD STATUS")
        self.stdout.write(f"Active holds count: {res['incident_hold_status']['active_holds_count']}")
        for h in res['incident_hold_status']['active_holds']:
            self.stdout.write(f"  Hold ID {h['id']}: {h['reason']}")
        self.stdout.write(f"Targeted objects count: {len(res['incident_hold_status']['targeted_objects'])}")
        for t in res['incident_hold_status']['targeted_objects']:
            self.stdout.write(f"  Target: {t['model']} ID {t['object_id']}")
        self.stdout.write("")

        self.stdout.write("APPROVAL STATUS")
        self.stdout.write(f"Approval count: {res['approval_status']['approval_count']}")
        self.stdout.write(f"Is approved: {res['approval_status']['is_approved']}\n")

        self.stdout.write("MUTATION STATUS")
        self.stdout.write(res['mutation_status'])
