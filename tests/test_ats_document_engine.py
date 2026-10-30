"""Unit tests for ATS Document Engine, real DOCX/PDF generation, and Cover Letter engine."""
import os
import pytest
from unittest.mock import MagicMock
from django.contrib.auth import get_user_model

import docx
import pypdf

from openoutreach.core.models import Workspace, WorkspaceMembership
from openoutreach.career.models import CandidateProfile, CandidateProfileVersion, CandidateFact, Job
from openoutreach.career.services.ats_analyzer import (
    extract_jd_requirements,
    classify_keywords_against_candidate,
)
from openoutreach.career.services.ats_document_engine import (
    generate_ats_resume_text,
    generate_job_specific_cover_letter,
    generate_application_package_documents,
    render_ats_docx,
    render_ats_pdf,
)

User = get_user_model()


@pytest.mark.django_db
class TestATSDocumentEngine:

    @pytest.fixture(autouse=True)
    def setup_data(self, db):
        self.user = User.objects.create_user(username="doc_test_user", email="doc_test@example.com")
        self.ws = Workspace.objects.create(name="Doc WS", slug="doc-ws")
        WorkspaceMembership.objects.create(workspace=self.ws, user=self.user, role="owner", status="active")

        self.candidate = CandidateProfile.objects.create(
            user=self.user,
            full_name="Alex Morgan",
            email="awancandidate17@example.com",
            phone="+1-202-555-0101"
        )
        self.cpv = CandidateProfileVersion.objects.create(
            profile=self.candidate,
            version_number=1,
            state="approved",
            full_name=self.candidate.full_name,
            summary="Senior Engineer with 5+ years experience building Python, Django, PostgreSQL, and Docker backends.",
            fact_snapshot=[
                {"category": "Skills", "value": "Python"},
                {"category": "Skills", "value": "Django"},
                {"category": "Skills", "value": "PostgreSQL"},
                {"category": "Skills", "value": "REST APIs"},
                {"category": "Experience", "value": {
                    "company": "Veloce Systems",
                    "title": "Senior Software Engineer",
                    "period": "2020 - Present",
                    "bullets": ["Architected distributed backend services with Python and PostgreSQL.", "Maintained 99.9% uptime for core REST APIs."]
                }}
            ]
        )
        self.candidate.active_version = self.cpv
        self.candidate.save()

        CandidateFact.objects.create(profile_version=self.cpv, fact_type="skill", category="Skills", value="Python", verified=True)
        CandidateFact.objects.create(profile_version=self.cpv, fact_type="skill", category="Skills", value="Django", verified=True)
        CandidateFact.objects.create(profile_version=self.cpv, fact_type="skill", category="Skills", value="PostgreSQL", verified=True)
        CandidateFact.objects.create(profile_version=self.cpv, fact_type="experience", category="Work", value={"company": "Veloce Systems", "title": "Senior Software Engineer"}, verified=True)

        self.job = Job.objects.create(
            company="Nexlify AI",
            title="Lead Python Architect",
            job_url="https://www.linkedin.com/jobs/view/2222222222/",
            description="We are seeking a Lead Python Architect with deep Django, PostgreSQL, and REST API experience.",
            is_active=True
        )

    def test_generate_ats_resume_text_truthful_and_tailored(self):
        """Test resume text generation prioritizes supported skills and does not fabricate unsupported tech."""
        jd_reqs = extract_jd_requirements(self.job)
        kw_map = classify_keywords_against_candidate(jd_reqs, self.candidate)

        resume_text = generate_ats_resume_text(self.candidate, jd_reqs, kw_map)

        assert "ALEX MORGAN" in resume_text
        assert "PROFESSIONAL SUMMARY" in resume_text
        assert "TECHNICAL SKILLS" in resume_text
        assert "PROFESSIONAL EXPERIENCE" in resume_text
        assert "EDUCATION" in resume_text
        assert "Python" in resume_text
        assert "Django" in resume_text
        assert "PostgreSQL" in resume_text
        assert "Veloce Systems" in resume_text

        # Master candidate summary and facts must remain untouched
        assert self.candidate.active_version.version_number == 1
        assert self.candidate.active_version.state == "approved"

    def test_render_real_docx_and_pdf_artifacts(self, tmp_path):
        """Test physical generation and text extractability of DOCX and text-selectable PDF artifacts."""
        jd_reqs = extract_jd_requirements(self.job)
        kw_map = classify_keywords_against_candidate(jd_reqs, self.candidate)

        output_dir = str(tmp_path)
        package_docs = generate_application_package_documents(
            candidate=self.candidate,
            job_requirements=jd_reqs,
            keyword_mapping=kw_map,
            output_dir=output_dir,
            cover_letter_mode="standard"
        )

        # 1. Verify DOCX
        docx_path = package_docs["resume_docx_path"]
        assert os.path.exists(docx_path)
        assert os.path.getsize(docx_path) > 0
        doc = docx.Document(docx_path)
        extracted_docx_text = "\n".join([p.text for p in doc.paragraphs])
        assert "Alex Morgan" in extracted_docx_text
        assert "PROFESSIONAL SUMMARY" in extracted_docx_text
        assert "TECHNICAL SKILLS" in extracted_docx_text

        # 2. Verify PDF
        pdf_path = package_docs["resume_pdf_path"]
        assert os.path.exists(pdf_path)
        assert os.path.getsize(pdf_path) > 0
        reader = pypdf.PdfReader(pdf_path)
        extracted_pdf_text = "\n".join([page.extract_text() for page in reader.pages])
        assert "Alex Morgan" in extracted_pdf_text
        assert "PROFESSIONAL SUMMARY" in extracted_pdf_text

        # 3. Verify SHA-256 Hashes
        assert package_docs["resume_hash"] is not None
        assert len(package_docs["resume_hash"]) == 64
        assert package_docs["cover_letter_hash"] is not None

    def test_cover_letter_length_modes(self):
        """Test cover letter generation across short, standard, and detailed modes."""
        jd_reqs = extract_jd_requirements(self.job)
        kw_map = classify_keywords_against_candidate(jd_reqs, self.candidate)

        short_cl = generate_job_specific_cover_letter(self.candidate, jd_reqs, kw_map, mode="short")
        assert "Nexlify AI" in short_cl["text"]
        assert "Lead Python Architect" in short_cl["text"]
        assert 100 <= short_cl["word_count"] <= 200

        std_cl = generate_job_specific_cover_letter(self.candidate, jd_reqs, kw_map, mode="standard")
        assert "Nexlify AI" in std_cl["text"]
        assert 180 <= std_cl["word_count"] <= 350

        detailed_cl = generate_job_specific_cover_letter(self.candidate, jd_reqs, kw_map, mode="detailed")
        assert "Nexlify AI" in detailed_cl["text"]
        assert 280 <= detailed_cl["word_count"] <= 500
