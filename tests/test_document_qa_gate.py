"""Unit tests for Automated Document QA Gate and Hallucination Prevention."""
import os
import pytest
from unittest.mock import MagicMock
from django.contrib.auth import get_user_model

from openoutreach.core.models import Workspace, WorkspaceMembership
from openoutreach.career.models import CandidateProfile, CandidateProfileVersion, Job
from openoutreach.career.services.document_qa_gate import qa_application_package

User = get_user_model()


@pytest.mark.django_db
class TestDocumentQAGate:

    @pytest.fixture(autouse=True)
    def setup_data(self, db):
        self.user = User.objects.create_user(username="qa_gate_user", email="qa_gate@example.com")
        self.ws = Workspace.objects.create(name="QA Gate WS", slug="qa-gate-ws")
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
            summary="Python Django Engineer"
        )
        self.candidate.active_version = self.cpv
        self.candidate.save()

        self.job = Job.objects.create(
            company="Target Corporation",
            title="Senior Python Architect",
            job_url="https://www.linkedin.com/jobs/view/3333333333/",
            description="Seeking Python Django engineer.",
            is_active=True
        )

        self.valid_resume = (
            "ALEX MORGAN\n"
            "awancandidate17@example.com | +1-202-555-0101\n\n"
            "PROFESSIONAL SUMMARY\n"
            "Senior Python Architect with 5+ years experience.\n\n"
            "TECHNICAL SKILLS\n"
            "Core Technologies: Python, Django, PostgreSQL\n\n"
            "PROFESSIONAL EXPERIENCE\n"
            "Lead Developer — Target Systems (2020 - Present)\n"
            "• Built Django backend services.\n\n"
            "EDUCATION\n"
            "BS Computer Science — University"
        )

        self.valid_cover_letter = (
            "Alex Morgan\n"
            "awancandidate17@example.com | +1-202-555-0101\n\n"
            "Hiring Team\n"
            "Target Corporation\n\n"
            "Dear Hiring Manager,\n"
            "I am excited to apply for the Senior Python Architect position at Target Corporation.\n"
            "Sincerely,\nAlex Morgan"
        )

    def test_qa_clean_package_passes(self):
        """Clean application package with valid identity and structure passes QA."""
        job_reqs = {"company": "Target Corporation", "title": "Senior Python Architect"}
        kw_map = {
            "python": {"status": "SUPPORTED"},
            "django": {"status": "SUPPORTED"},
            "postgresql": {"status": "SUPPORTED"}
        }

        passed, errors, metrics = qa_application_package(
            candidate=self.candidate,
            job_requirements=job_reqs,
            resume_text=self.valid_resume,
            cover_letter_text=self.valid_cover_letter,
            keyword_mapping=kw_map
        )
        assert passed is True
        assert len(errors) == 0
        assert metrics["candidate_identity_valid"] is True
        assert metrics["template_leak_free"] is True

    def test_qa_fails_on_template_variable_leak(self):
        """QA catches and rejects unpopulated template variables like {{company}} or [Insert Title]."""
        leaky_resume = self.valid_resume.replace("Senior Python Architect", "{{job_title}} at [Insert Company]")
        job_reqs = {"company": "Target Corporation", "title": "Senior Python Architect"}

        passed, errors, metrics = qa_application_package(
            candidate=self.candidate,
            job_requirements=job_reqs,
            resume_text=leaky_resume,
            cover_letter_text=self.valid_cover_letter
        )
        assert passed is False
        assert any("Template variable or placeholder leak" in e for e in errors)
        assert metrics["template_leak_free"] is False

    def test_qa_fails_on_cross_job_contamination(self):
        """QA rejects if another company's name contaminates the current job's application package."""
        job_reqs = {"company": "Target Corporation", "title": "Senior Python Architect"}
        contaminated_cl = self.valid_cover_letter.replace("Target Corporation", "Apple Inc")

        passed, errors, metrics = qa_application_package(
            candidate=self.candidate,
            job_requirements=job_reqs,
            resume_text=self.valid_resume,
            cover_letter_text=contaminated_cl,
            other_jobs_context=[{"company": "Apple Inc", "title": "iOS Dev"}]
        )
        assert passed is False
        assert any("Cross-job contamination" in e or "Target company 'Target Corporation' missing" in e for e in errors)

    def test_qa_fails_on_unsupported_skill_fabrication(self):
        """QA strictly detects and rejects fabricated unsupported skills in the technical skills section."""
        fabricated_resume = self.valid_resume.replace("Core Technologies: Python, Django, PostgreSQL", "Core Technologies: Python, Django, Solidity, Rust")
        job_reqs = {"company": "Target Corporation", "title": "Senior Python Architect"}
        kw_map = {
            "python": {"status": "SUPPORTED"},
            "django": {"status": "SUPPORTED"},
            "solidity": {"status": "UNSUPPORTED"},
            "rust": {"status": "UNSUPPORTED"}
        }

        passed, errors, metrics = qa_application_package(
            candidate=self.candidate,
            job_requirements=job_reqs,
            resume_text=fabricated_resume,
            cover_letter_text=self.valid_cover_letter,
            keyword_mapping=kw_map
        )
        assert passed is False
        assert any("Fabrication detected: Unsupported skill" in e for e in errors)
        assert metrics["unsupported_claims_count"] >= 1
