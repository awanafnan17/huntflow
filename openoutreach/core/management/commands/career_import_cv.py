import os
import hashlib
import mimetypes
from django.core.management.base import BaseCommand, CommandError
from django.contrib.auth.models import User
from django.core.files import File
from openoutreach.career.models import CandidateProfile, CandidateDocument, CandidateDocumentVersion

class Command(BaseCommand):
    help = "Import candidate CV document."

    def add_arguments(self, parser):
        parser.add_argument("--user-email", required=True)
        parser.add_argument("--file", required=True)

    def handle(self, *args, **options):
        file_path = options["file"]
        if not os.path.exists(file_path):
            raise CommandError(f"File {file_path} does not exist.")

        # Validate extension
        allowed_exts = {".pdf", ".docx", ".txt", ".md"}
        ext = os.path.splitext(file_path.lower())[1]
        if ext not in allowed_exts:
            raise CommandError(f"Extension {ext} is not allowed. Allowed: {', '.join(allowed_exts)}")

        # Get MIME type
        mime_type, _ = mimetypes.guess_type(file_path)
        if not mime_type:
            if ext == ".md":
                mime_type = "text/markdown"
            else:
                mime_type = "application/octet-stream"

        # Calculate file size and checksum
        file_size = os.path.getsize(file_path)
        sha256 = hashlib.sha256()
        with open(file_path, "rb") as f:
            while chunk := f.read(8192):
                sha256.update(chunk)
        file_hash = sha256.hexdigest()

        # Resolve user and profile
        try:
            user = User.objects.get(email=options["user_email"])
        except User.DoesNotExist:
            raise CommandError(f"Django user with email {options['user_email']} does not exist.")

        profile, _ = CandidateProfile.objects.get_or_create(
            user=user,
            defaults={
                "full_name": user.username,
                "email": user.email
            }
        )

        # Create CandidateDocument
        doc = CandidateDocument.objects.create(
            profile=profile,
            name=f"CV_{os.path.basename(file_path)}",
            document_type="cv",
            status="active"
        )

        # Create version
        with open(file_path, "rb") as f:
            version = CandidateDocumentVersion.objects.create(
                document=doc,
                version_number=1,
                file=File(f, name=os.path.basename(file_path)),
                file_hash=file_hash,
                file_size=file_size,
                mime_type=mime_type,
                original_filename=os.path.basename(file_path)
            )

        self.stdout.write(self.style.SUCCESS("CV successfully imported!"))
        self.stdout.write(f"CandidateProfile ID: {profile.id}")
        self.stdout.write(f"CandidateDocument ID: {doc.id}")
        self.stdout.write(f"CandidateDocumentVersion ID: {version.id}")
        self.stdout.write(f"File Checksum: {file_hash}")
