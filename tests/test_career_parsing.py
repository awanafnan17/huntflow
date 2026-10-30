import os
import pytest
from unittest.mock import patch, MagicMock
from django.urls import reverse
from django.core.files.uploadedfile import SimpleUploadedFile
from django.contrib.auth.models import User, Permission
from django.core.exceptions import ValidationError
from openoutreach.career.models import (
    CandidateProfile,
    CandidateDocument,
    CandidateDocumentVersion,
    CandidateProfileVersion,
    CandidateFact,
    CandidateExtractionRecord,
    CandidateFactDraft
)
from openoutreach.career.service import (
    ExtractedProfile,
    ExperienceItem,
    EducationItem,
    CertificationItem
)
from tests.factories import UserFactory

@pytest.mark.django_db
class TestCareerParsing:

    @pytest.fixture(autouse=True)
    def _setup(self, client, db):
        self.user = UserFactory(username="seeker1", email="seeker1@example.com")
        self.profile = CandidateProfile.objects.create(
            user=self.user,
            full_name="Original Name",
            email="seeker1@example.com",
            phone="1234",
            linkedin_url="https://linkedin.com/in/seeker1",
            summary="Original Summary"
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

    @patch("openoutreach.career.service.run_factual_extraction")
    def test_raw_cv_paste_workflow(self, mock_extract):
        # Setup mock return data
        mock_extract.return_value = ExtractedProfile(
            full_name="Jane Paste",
            email="jane@paste.com",
            phone="9876",
            linkedin_url="https://linkedin.com/in/janepaste",
            summary="Pasted Text Expert",
            experience=[
                ExperienceItem(company="OpenAI", title="AI Researcher", start_date="2022", end_date="Present", bullet_points=["Trained model B"])
            ],
            education=[
                EducationItem(school="MIT", degree="PhD AI", year="2021")
            ],
            skills=["Transformers", "PyTorch"],
            certifications=[
                CertificationItem(name="AWS Solutions Architect", issuer="Amazon", year="2022")
            ]
        )

        url = reverse("career:raw_cv_input")
        
        # 1. Test HTML payload rejection
        resp_bad = self.client.post(url, {"raw_text": "<script>alert(1)</script>"})
        assert resp_bad.status_code == 200
        assert "file" not in resp_bad.context or "raw_text" in resp_bad.context["form"].errors

        # 2. Test successful raw text parse
        pasted_text = "Jane Paste\nAI Researcher at OpenAI\nPhD from MIT"
        resp = self.client.post(url, {"raw_text": pasted_text})
        assert resp.status_code == 302
        
        # Check extraction record and draft facts created
        record = CandidateExtractionRecord.objects.get(profile=self.profile, source_type="raw_text")
        assert record.status == "pending"
        assert record.raw_text == pasted_text
        assert record.raw_text_hash != ""

        drafts = record.fact_drafts.all()
        assert drafts.filter(fact_type="experience", category="OpenAI").exists()
        assert drafts.filter(fact_type="education", category="MIT").exists()
        assert drafts.filter(fact_type="skill", value={"name": "Transformers"}).exists()
        assert drafts.filter(fact_type="certification", category="AWS Solutions Architect").exists()

    @patch("openoutreach.career.service.run_factual_extraction")
    @patch("openoutreach.career.service.extract_text_from_file_version")
    def test_document_parse_trigger_view(self, mock_extract_text, mock_extract):
        mock_extract_text.return_value = "Mocked resume file content text"
        mock_extract.return_value = ExtractedProfile(
            full_name="Bob Doc",
            email="bob@doc.com",
            experience=[],
            education=[],
            skills=["Management"],
            certifications=[]
        )

        pdf_content = b"%PDF-1.4\n%...\n%%EOF"
        uploaded_file = SimpleUploadedFile("resume.pdf", pdf_content, content_type="application/pdf")
        doc = CandidateDocument.objects.create(profile=self.profile, name="My Resume", document_type="cv")
        ver = CandidateDocumentVersion.objects.create(
            document=doc,
            version_number=1,
            file=uploaded_file,
            file_hash="dummyhash",
            file_size=len(pdf_content),
            mime_type="application/pdf",
            original_filename="resume.pdf"
        )

        url = reverse("career:document_parse", kwargs={"version_pk": ver.pk})
        resp = self.client.post(url)
        assert resp.status_code == 302

        record = CandidateExtractionRecord.objects.get(profile=self.profile, source_type="document", document_version=ver)
        assert record.status == "pending"
        assert record.fact_drafts.count() == 1

    def test_extraction_review_save_and_commit(self):
        # Set up a record with drafts
        record = CandidateExtractionRecord.objects.create(
            profile=self.profile,
            source_type="raw_text",
            status="pending"
        )
        draft1 = CandidateFactDraft.objects.create(
            extraction_record=record,
            fact_type="experience",
            category="Startup X",
            value={"company": "Startup X", "title": "Lead Dev", "start": "2020", "end": "2022", "bullet_points": ["Coded app"]},
            status="pending"
        )
        draft2 = CandidateFactDraft.objects.create(
            extraction_record=record,
            fact_type="skill",
            category="Skill",
            value={"name": "Go"},
            status="pending"
        )

        url = reverse("career:extraction_review", kwargs={"record_pk": record.pk})
        
        # 1. Save edits (draft1 -> approved, draft2 -> rejected, update draft1 title)
        resp_save = self.client.post(url, {
            "action": "save",
            f"draft_{draft1.id}_status": "approved",
            f"draft_{draft1.id}_company": "Startup X Edited",
            f"draft_{draft1.id}_title": "Principal Architect",
            f"draft_{draft1.id}_start": "2020",
            f"draft_{draft1.id}_end": "2022",
            f"draft_{draft1.id}_bullet_points": "Coded app\nScaled DB",
            f"draft_{draft2.id}_status": "rejected",
            f"draft_{draft2.id}_name": "Go Language"
        })
        assert resp_save.status_code == 302
        
        draft1.refresh_from_db()
        draft2.refresh_from_db()
        assert draft1.status == "approved"
        assert draft1.value["title"] == "Principal Architect"
        assert draft1.value["bullet_points"] == ["Coded app", "Scaled DB"]
        assert draft2.status == "rejected"

        # 2. Commit approved facts (action="commit")
        resp_commit = self.client.post(url, {
            "action": "commit",
            # Include bio updates
            "full_name": "Bob Extracted Name",
            "email": "bob@extracted.com",
            "phone": "555-5555",
            "linkedin_url": "https://linkedin.com/in/bobextracted",
            "summary": "New bio summary",
            # Must supply draft status and fields again because it acts as a save before commit
            f"draft_{draft1.id}_status": "approved",
            f"draft_{draft1.id}_company": "Startup X Edited",
            f"draft_{draft1.id}_title": "Principal Architect",
            f"draft_{draft1.id}_start": "2020",
            f"draft_{draft1.id}_end": "2022",
            f"draft_{draft1.id}_bullet_points": "Coded app\nScaled DB",
            f"draft_{draft2.id}_status": "rejected",
            f"draft_{draft2.id}_name": "Go Language"
        })
        assert resp_commit.status_code == 302
        
        # Verify a new profile version was created (v2) and previous remains unchanged (v1)
        assert self.profile.versions.count() == 2
        
        v1 = self.profile.versions.get(version_number=1)
        assert v1.full_name == "Original Name"
        
        v2 = self.profile.versions.get(version_number=2)
        assert v2.full_name == "Bob Extracted Name"
        assert v2.summary == "New bio summary"
        assert v2.created_by == self.user

        # Verify fact was created and linked to v2
        facts = v2.facts.all()
        assert facts.count() == 1
        fact = facts.first()
        assert fact.fact_type == "experience"
        assert fact.category == "Startup X Edited"
        assert fact.value["title"] == "Principal Architect"
        assert fact.value["bullet_points"] == ["Coded app", "Scaled DB"]

        # Extraction record status updated to applied
        record.refresh_from_db()
        assert record.status == "applied"

    @patch("openoutreach.career.service.pypdf.PdfReader")
    def test_extract_text_from_pdf_service(self, mock_pdf):
        mock_reader = MagicMock()
        mock_page = MagicMock()
        mock_page.extract_text.return_value = "Extracted PDF resume content text."
        mock_reader.pages = [mock_page]
        mock_pdf.return_value = mock_reader
        
        from openoutreach.career.service import extract_text_from_pdf
        text = extract_text_from_pdf("dummy.pdf")
        assert text == "Extracted PDF resume content text."

    @patch("openoutreach.career.service.docx.Document")
    def test_extract_text_from_docx_service(self, mock_docx):
        mock_doc = MagicMock()
        mock_para = MagicMock()
        mock_para.text = "Extracted DOCX resume paragraph."
        mock_doc.paragraphs = [mock_para]
        
        mock_cell = MagicMock()
        mock_cell.text = "Cell Text"
        mock_row = MagicMock()
        mock_row.cells = [mock_cell]
        mock_table = MagicMock()
        mock_table.rows = [mock_row]
        mock_doc.tables = [mock_table]
        
        mock_docx.return_value = mock_doc
        
        from openoutreach.career.service import extract_text_from_docx
        text = extract_text_from_docx("dummy.docx")
        assert "Extracted DOCX resume paragraph." in text
        assert "Cell Text" in text
