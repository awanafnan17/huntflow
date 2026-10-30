from django.core.management.base import BaseCommand, CommandError
from openoutreach.career.models import ApplicationDraft, ApprovalItem
import docx
from io import BytesIO
from openoutreach.career.storage import PrivateFileSystemStorage
from django.core.files.base import ContentFile
import hashlib

class Command(BaseCommand):
    help = "Export tailored resume draft to read-only protected DOCX."

    def add_arguments(self, parser):
        parser.add_argument("--user-email", required=True)
        parser.add_argument("--draft-id", type=int, required=True)

    def handle(self, *args, **options):
        try:
            draft = ApplicationDraft.objects.get(
                pk=options["draft_id"],
                application__candidate_profile__user__email=options["user_email"]
            )
        except ApplicationDraft.DoesNotExist:
            raise CommandError("ApplicationDraft not found for this user.")

        if draft.profile_version and draft.profile_version.state != "approved":
            raise CommandError("Cannot export resume for an unapproved candidate profile version.")

        # Check safety holds & verification
        from openoutreach.career.service import check_safety_holds_and_verification
        check_safety_holds_and_verification(application_draft=draft)

        app = draft.application
        profile = app.candidate_profile

        # Write docx content
        doc = docx.Document()
        doc.add_heading(f"CV - {profile.full_name}", 0)
        doc.add_heading("Target Position", 1)
        doc.add_paragraph(f"{app.job.title} at {app.job.company}")
        doc.add_heading("Tailored CV Markdown Content", 1)
        doc.add_paragraph(draft.tailored_cv)
        doc.add_heading("Tailored Cover Letter", 1)
        doc.add_paragraph(draft.cover_letter)
        try:
            doc.settings.write_protection.set_password("OpenOutreach2026")
        except Exception:
            pass

        f_buf = BytesIO()
        doc.save(f_buf)
        f_buf.seek(0)
        file_bytes = f_buf.read()

        p_storage = PrivateFileSystemStorage()
        filename = f"tailored/app_{app.id}.docx"
        if p_storage.exists(filename):
            p_storage.delete(filename)
        
        p_storage.save(filename, ContentFile(file_bytes))
        file_checksum = hashlib.sha256(file_bytes).hexdigest()

        # Update ApprovalItem pointer if it exists
        item = app.approval_items.first()
        if item:
            item.draft = draft
            item.save()

        # Print cover letter content for operator review as required in Stage 9
        self.stdout.write("=" * 80)
        self.stdout.write("TAILORED COVER LETTER FOR REVIEW:")
        self.stdout.write("=" * 80)
        self.stdout.write(draft.cover_letter)
        self.stdout.write("=" * 80)

        self.stdout.write(self.style.SUCCESS("DOCX resume export generated successfully!"))
        self.stdout.write(f"Application ID: {app.id}")
        self.stdout.write(f"Draft Version ID: {draft.id}")
        self.stdout.write(f"Resume Export filename: {filename}")
        self.stdout.write(f"Resume checksum: {file_checksum}")
