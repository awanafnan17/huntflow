from django.core.management.base import BaseCommand, CommandError
from openoutreach.career.models import ApplicationDraft
from openoutreach.career.approval_service import ApprovalIntegrityService
from django.core.exceptions import ValidationError

class Command(BaseCommand):
    help = "Validate ApplicationDraft integrity and trace mappings."

    def add_arguments(self, parser):
        parser.add_argument("--draft-id", type=int, required=True)

    def handle(self, *args, **options):
        try:
            draft = ApplicationDraft.objects.get(pk=options["draft_id"])
        except ApplicationDraft.DoesNotExist:
            raise CommandError("ApplicationDraft not found.")

        # Create dummy ApprovalItem to check integrity check workflow
        from openoutreach.career.models import ApprovalItem
        item = ApprovalItem.objects.filter(draft=draft).first()
        if not item:
            item = ApprovalItem.objects.create(
                application=draft.application,
                draft=draft,
                status="pending"
            )

        try:
            ApprovalIntegrityService.validate_item_integrity(item)
            self.stdout.write(self.style.SUCCESS(f"Draft {draft.id} validation check passed! Factual integrity is intact."))
        except ValidationError as e:
            self.stdout.write(self.style.ERROR(f"Factual integrity warning: {str(e)}"))
            raise CommandError("Draft validation failed!")
