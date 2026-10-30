from django.core.management.base import BaseCommand, CommandError
from openoutreach.career.models import CandidateDocument, CandidateDocumentVersion
from openoutreach.career.service import parse_and_create_drafts_from_document

class Command(BaseCommand):
    help = "Run factual extraction on imported CV."

    def add_arguments(self, parser):
        parser.add_argument("--user-email", required=True)
        parser.add_argument("--document-id", type=int, required=True)

    def handle(self, *args, **options):
        try:
            doc = CandidateDocument.objects.get(
                pk=options["document_id"],
                profile__user__email=options["user_email"]
            )
        except CandidateDocument.DoesNotExist:
            raise CommandError("CandidateDocument not found for specified ID and user.")

        version = doc.versions.first()
        if not version:
            raise CommandError("No CandidateDocumentVersion exists for this document.")

        self.stdout.write(f"Running LLM factual parsing on Document v{version.version_number}...")
        record = parse_and_create_drafts_from_document(doc.profile, version)
        
        self.stdout.write(self.style.SUCCESS(f"CV extraction record created successfully!"))
        self.stdout.write(f"Extraction Record ID: {record.id}")
        self.stdout.write(f"Status: {record.status}")
        self.stdout.write(f"Draft facts count: {record.fact_drafts.count()}")
