import os
import io
import pytest
import hashlib
from django.urls import reverse
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.exceptions import PermissionDenied
from django.http import FileResponse
from django.contrib.auth.models import User, Permission
from openoutreach.career.models import (
    CandidateProfile,
    CandidateDocument,
    CandidateDocumentVersion,
    CandidateProfileVersion,
    CandidateFact,
    CandidateProfileApproval,
    CandidateAuditEvent
)
from tests.factories import UserFactory

@pytest.mark.django_db
class TestCareerPlatform:

    @pytest.fixture(autouse=True)
    def _setup(self, client, db):
        self.user = UserFactory(username="seeker1")
        self.profile = CandidateProfile.objects.create(
            user=self.user,
            full_name="John Doe",
            email="john@example.com",
            phone="123456",
            linkedin_url="https://linkedin.com/in/johndoe",
            summary="SWE at Google"
        )
        self.profile_version = CandidateProfileVersion.objects.create(
            profile=self.profile,
            version_number=1,
            full_name=self.profile.full_name,
            email=self.profile.email,
            phone=self.profile.phone,
            linkedin_url=self.profile.linkedin_url,
            summary=self.profile.summary,
            created_by=self.user
        )
        client.force_login(self.user)
        self.client = client

    def test_career_overview_auto_creates_profile(self):
        # Create a new user with no profile
        new_user = UserFactory(username="seeker2", email="seeker2@example.com")
        self.client.force_login(new_user)
        
        url = reverse("career:overview")
        resp = self.client.get(url)
        assert resp.status_code == 200
        
        # Check that CandidateProfile was auto created
        assert CandidateProfile.objects.filter(user=new_user).exists()
        profile = CandidateProfile.objects.get(user=new_user)
        assert profile.full_name == ""
        assert profile.email == "seeker2@example.com"
        assert profile.versions.count() == 1

    def test_document_upload_validation_valid_pdf(self):
        pdf_content = b"%PDF-1.4\n%...\n%%EOF"
        uploaded_file = SimpleUploadedFile("resume.pdf", pdf_content, content_type="application/pdf")
        
        url = reverse("career:document_upload")
        resp = self.client.post(url, {
            "name": "My Resume",
            "document_type": "cv",
            "file": uploaded_file
        })
        assert resp.status_code == 302  # redirects to overview
        
        # Assert Document and version created
        doc = CandidateDocument.objects.get(profile=self.profile, name="My Resume")
        assert doc.document_type == "cv"
        assert doc.versions.count() == 1
        
        ver = doc.versions.first()
        assert ver.version_number == 1
        assert ver.mime_type == "application/pdf"
        assert ver.file_size == len(pdf_content)
        assert ver.file_hash == hashlib.sha256(pdf_content).hexdigest()
        assert CandidateAuditEvent.objects.filter(event_type="document_uploaded").exists()

    def test_document_upload_validation_invalid_mimetype(self):
        uploaded_file = SimpleUploadedFile("exploit.exe", b"MZ...", content_type="application/octet-stream")
        
        url = reverse("career:document_upload")
        resp = self.client.post(url, {
            "name": "Malicious File",
            "document_type": "cv",
            "file": uploaded_file
        })
        assert resp.status_code == 200  # Renders form with errors
        assert "file" in resp.context["form"].errors
        assert "not allowed" in resp.context["form"].errors["file"][0]

    def test_document_upload_validation_invalid_pdf_signature(self):
        # PDF mimetype but invalid content header
        uploaded_file = SimpleUploadedFile("fake.pdf", b"NOTAPDF...", content_type="application/pdf")
        
        url = reverse("career:document_upload")
        resp = self.client.post(url, {
            "name": "Fake Resume",
            "document_type": "cv",
            "file": uploaded_file
        })
        assert resp.status_code == 200
        assert "file" in resp.context["form"].errors
        assert "header signature is incorrect" in resp.context["form"].errors["file"][0]

    def test_document_download_ownership_protection(self):
        # Create a document for john doe
        pdf_content = b"%PDF-1.4\n%...\n%%EOF"
        uploaded_file = SimpleUploadedFile("resume.pdf", pdf_content, content_type="application/pdf")
        doc = CandidateDocument.objects.create(profile=self.profile, name="Resume", document_type="cv")
        ver = CandidateDocumentVersion.objects.create(
            document=doc,
            version_number=1,
            file=uploaded_file,
            file_hash="dummyhash",
            file_size=len(pdf_content),
            mime_type="application/pdf",
            original_filename="resume.pdf"
        )
        
        # Log in as a different user without permission
        other_user = UserFactory(username="attacker")
        self.client.force_login(other_user)
        
        url = reverse("career:document_download", kwargs={"version_pk": ver.pk})
        resp = self.client.get(url)
        assert resp.status_code == 403  # Forbidden

        # Now login as superuser
        admin_user = UserFactory(username="admin", is_superuser=True)
        # Grant view_private_documents permission
        perm = Permission.objects.get(codename="view_private_documents")
        admin_user.user_permissions.add(perm)
        
        self.client.force_login(admin_user)
        resp = self.client.get(url)
        assert resp.status_code == 200
        assert isinstance(resp, FileResponse)

    def test_profile_edit_versioning(self):
        url = reverse("career:profile_edit")
        resp = self.client.post(url, {
            "full_name": "John Changed",
            "email": "changed@example.com",
            "phone": "999999",
            "linkedin_url": "https://linkedin.com/in/changed",
            "summary": "New bio details"
        })
        assert resp.status_code == 302
        
        self.profile.refresh_from_db()
        assert self.profile.full_name == "John Changed"
        assert self.profile.versions.count() == 2
        
        latest_ver = self.profile.versions.order_by("-version_number").first()
        assert latest_ver.version_number == 2
        assert latest_ver.full_name == "John Changed"
        assert latest_ver.summary == "New bio details"
        assert CandidateAuditEvent.objects.filter(event_type="profile_updated").exists()

    def test_profile_approve_workflow(self):
        # Seeker cannot approve their own version
        url = reverse("career:profile_approve", kwargs={"version_pk": self.profile_version.pk})
        resp = self.client.post(url, {"note": "Looks clean"})
        assert resp.status_code == 403  # Forbidden
        
        # Admin / Approver gets approval permission
        approver = UserFactory(username="approver")
        perm = Permission.objects.get(codename="approve_profile")
        approver.user_permissions.add(perm)
        self.client.force_login(approver)
        
        resp = self.client.post(url, {"note": "Approved by testing approver"})
        assert resp.status_code == 302
        
        # Verify approval is recorded
        self.profile_version.refresh_from_db()
        assert hasattr(self.profile_version, "approval")
        approval = self.profile_version.approval
        assert approval.approved_by == approver
        assert approval.approval_note == "Approved by testing approver"
        assert CandidateAuditEvent.objects.filter(event_type="profile_approved").exists()

        # Test duplicate approval rejection (idempotency)
        resp2 = self.client.post(url, {"note": "Approved again"})
        assert resp2.status_code == 302
        # Count should remain 1
        assert CandidateProfileApproval.objects.filter(profile_version=self.profile_version).count() == 1

    def test_physical_filename_obfuscation(self):
        pdf_content = b"%PDF-1.4\n%...\n%%EOF"
        uploaded_file = SimpleUploadedFile("my_secrets_resume.pdf", pdf_content, content_type="application/pdf")
        doc = CandidateDocument.objects.create(profile=self.profile, name="Resume", document_type="cv")
        ver = CandidateDocumentVersion.objects.create(
            document=doc,
            version_number=1,
            file=uploaded_file,
            file_hash="dummyhash",
            file_size=len(pdf_content),
            mime_type="application/pdf",
            original_filename="my_secrets_resume.pdf"
        )
        physical_path = ver.file.name
        # Assert that the physical storage filename does NOT contain original filename
        assert "my_secrets_resume.pdf" not in physical_path
        # Should be stored under a randomized folder or name like UUID
        assert len(os.path.basename(physical_path)) > 15

    def test_immutability_document_version(self):
        pdf_content = b"%PDF-1.4\n%...\n%%EOF"
        uploaded_file = SimpleUploadedFile("resume.pdf", pdf_content, content_type="application/pdf")
        doc = CandidateDocument.objects.create(profile=self.profile, name="Resume", document_type="cv")
        ver = CandidateDocumentVersion.objects.create(
            document=doc,
            version_number=1,
            file=uploaded_file,
            file_hash="dummyhash",
            file_size=len(pdf_content),
            mime_type="application/pdf",
            original_filename="resume.pdf"
        )
        # Attempt to save again should raise ValidationError
        from django.core.exceptions import ValidationError
        with pytest.raises(ValidationError):
            ver.file_size = 9999
            ver.save()

    def test_immutability_approved_profile_version(self):
        approver = UserFactory(username="approver")
        CandidateProfileApproval.objects.create(
            profile_version=self.profile_version,
            approved_by=approver,
            approval_note="Approved",
            idempotency_key="unique_key"
        )
        # Trying to edit the version now should fail
        from django.core.exceptions import ValidationError
        with pytest.raises(ValidationError):
            self.profile_version.full_name = "Changed Approved Profile Name"
            self.profile_version.save()

    def test_permissions_enforcement(self):
        # 1. Anonymous user blocked (redirected)
        self.client.logout()
        url = reverse("career:overview")
        resp = self.client.get(url)
        assert resp.status_code == 302
        
        # 2. Unrelated authenticated user gets blocked with 403 when trying to download other's document
        pdf_content = b"%PDF-1.4\n%...\n%%EOF"
        uploaded_file = SimpleUploadedFile("resume.pdf", pdf_content, content_type="application/pdf")
        doc = CandidateDocument.objects.create(profile=self.profile, name="Resume", document_type="cv")
        ver = CandidateDocumentVersion.objects.create(
            document=doc,
            version_number=1,
            file=uploaded_file,
            file_hash="dummyhash",
            file_size=len(pdf_content),
            mime_type="application/pdf",
            original_filename="resume.pdf"
        )
        
        unrelated_user = UserFactory(username="unrelated")
        self.client.force_login(unrelated_user)
        
        url_download = reverse("career:document_download", kwargs={"version_pk": ver.pk})
        resp_download = self.client.get(url_download)
        assert resp_download.status_code == 403

    def test_required_portal_pages(self):
        pages = [
            ("career:overview", {}),
            ("career:profile", {}),
            ("career:documents", {}),
            ("career:profile_versions", {}),
        ]
        for url_name, kwargs in pages:
            url = reverse(url_name, kwargs=kwargs)
            resp = self.client.get(url)
            assert resp.status_code == 200

        # Audit page requires permission
        url_audit = reverse("career:audit")
        resp_audit = self.client.get(url_audit)
        assert resp_audit.status_code == 403  # Forbidden without view_audit_history perm
        
        # Give permission
        perm = Permission.objects.get(codename="view_audit_history")
        self.user.user_permissions.add(perm)
        
        resp_audit_ok = self.client.get(url_audit)
        assert resp_audit_ok.status_code == 200

    def test_quarantined_rejected_status_download_block(self):
        pdf_content = b"%PDF-1.4\n%...\n%%EOF"
        uploaded_file = SimpleUploadedFile("resume.pdf", pdf_content, content_type="application/pdf")
        
        # Quarantined document
        doc_q = CandidateDocument.objects.create(profile=self.profile, name="Quarantined Resume", document_type="cv", status="quarantined")
        ver_q = CandidateDocumentVersion.objects.create(
            document=doc_q,
            version_number=1,
            file=uploaded_file,
            file_hash="dummyhash1",
            file_size=len(pdf_content),
            mime_type="application/pdf",
            original_filename="resume.pdf"
        )
        
        # Rejected document
        doc_r = CandidateDocument.objects.create(profile=self.profile, name="Rejected Resume", document_type="cv", status="rejected")
        ver_r = CandidateDocumentVersion.objects.create(
            document=doc_r,
            version_number=1,
            file=uploaded_file,
            file_hash="dummyhash2",
            file_size=len(pdf_content),
            mime_type="application/pdf",
            original_filename="resume.pdf"
        )
        
        # Try downloading quarantined
        url_q = reverse("career:document_download", kwargs={"version_pk": ver_q.pk})
        resp_q = self.client.get(url_q)
        assert resp_q.status_code == 403
        
        # Try downloading rejected
        url_r = reverse("career:document_download", kwargs={"version_pk": ver_r.pk})
        resp_r = self.client.get(url_r)
        assert resp_r.status_code == 403

    def test_archive_candidate_document(self):
        doc = CandidateDocument.objects.create(profile=self.profile, name="To Archive", document_type="cv")
        
        # 1. Unrelated user cannot archive
        unrelated_user = UserFactory(username="unrelated")
        self.client.force_login(unrelated_user)
        url = reverse("career:document_archive", kwargs={"document_pk": doc.pk})
        resp = self.client.post(url)
        assert resp.status_code == 403
        
        # 2. Owner can archive
        self.client.force_login(self.user)
        resp2 = self.client.post(url)
        assert resp2.status_code == 302
        
        doc.refresh_from_db()
        assert doc.status == "archived"
        assert CandidateAuditEvent.objects.filter(event_type="document_archived").exists()

    def test_career_overview_lists_applications(self):
        from openoutreach.career.models import Job, JobApplication
        job = Job.objects.create(
            title="Overview Dev",
            company="OverviewCorp",
            description="Testing application list in overview."
        )
        app = JobApplication.objects.create(
            candidate_profile=self.profile,
            job=job,
            status="draft"
        )
        url = reverse("career:overview")
        resp = self.client.get(url)
        assert resp.status_code == 200
        content = resp.content.decode("utf-8")
        assert "Overview Dev" in content
        assert "OverviewCorp" in content
        assert "Draft" in content

