"""Unit tests for ATS Job Description Analyzer and Evidence Matcher."""
import pytest
from unittest.mock import MagicMock
from django.utils import timezone
from django.contrib.auth import get_user_model

from openoutreach.core.models import Workspace, WorkspaceMembership
from openoutreach.core.services.entitlements import get_or_create_workspace_entitlement
from openoutreach.career.models import (
    CandidateProfile, CandidateProfileVersion, CandidateFact, Job
)
from openoutreach.career.services.ats_analyzer import (
    extract_jd_requirements,
    get_candidate_factual_evidence,
    classify_keywords_against_candidate,
    calculate_ats_match_score,
)

User = get_user_model()


@pytest.mark.django_db
class TestATSAnalyzer:

    @pytest.fixture(autouse=True)
    def setup_data(self, db):
        self.user = User.objects.create_user(username="ats_cand_user", email="ats_cand@example.com")
        self.ws = Workspace.objects.create(name="ATS Workspace", slug="ats-ws")
        WorkspaceMembership.objects.create(workspace=self.ws, user=self.user, role="owner", status="active")
        get_or_create_workspace_entitlement(self.ws)

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
            summary="Senior Software Engineer with experience in Python, Django, PostgreSQL, Docker, and REST APIs.",
            fact_snapshot=[
                {"category": "Skills", "value": "Python", "id": 101},
                {"category": "Skills", "value": "Django", "id": 102},
                {"category": "Skills", "value": "PostgreSQL", "id": 103},
                {"category": "Skills", "value": "Docker", "id": 104},
                {"category": "Experience", "value": {"company": "Acme Tech", "title": "Senior Engineer", "period": "2021 - Present"}}
            ]
        )
        self.candidate.active_version = self.cpv
        self.candidate.save()

        # Add verified candidate facts
        CandidateFact.objects.create(profile_version=self.cpv, fact_type="skill", category="Skills", value="Python", verified=True)
        CandidateFact.objects.create(profile_version=self.cpv, fact_type="skill", category="Skills", value="Django", verified=True)
        CandidateFact.objects.create(profile_version=self.cpv, fact_type="skill", category="Skills", value="PostgreSQL", verified=True)
        CandidateFact.objects.create(profile_version=self.cpv, fact_type="skill", category="Skills", value="Docker", verified=True)
        CandidateFact.objects.create(profile_version=self.cpv, fact_type="experience", category="Work", value={"company": "Acme Tech", "title": "Senior Engineer"}, verified=True)
        CandidateFact.objects.create(profile_version=self.cpv, fact_type="education", category="Degree", value={"degree": "BS Computer Science", "university": "NUST"}, verified=True)

    def test_extract_jd_requirements_structured_output(self):
        """Test accurate structured extraction of skills, seniority, experience, and must-haves from real JD."""
        jd_text = """
        About Us:
        CloudScale Inc is looking for a Senior Python Developer to join our backend infrastructure team in a remote capacity.
        
        Requirements:
        • 5+ years of experience in backend development.
        • Strong proficiency with Python and Django framework.
        • Solid experience with PostgreSQL database optimization and Docker containerization.
        • Experience designing REST APIs.
        • Bachelor's degree in Computer Science or related field.
        
        Preferred Qualifications:
        • Experience with Kubernetes and Redis caching.
        • Familiarity with AWS cloud deployment.
        """
        job = Job.objects.create(
            company="CloudScale Inc",
            title="Senior Python Developer",
            job_url="https://www.linkedin.com/jobs/view/1111111111/",
            description=jd_text,
            is_active=True
        )

        extracted = extract_jd_requirements(job)
        assert extracted["title"] == "Senior Python Developer"
        assert extracted["company"] == "CloudScale Inc"
        assert extracted["seniority"] == "senior"
        assert extracted["min_years_experience"] == 5
        assert extracted["work_model"] == "remote"
        assert "python" in extracted["required_skills"]
        assert "django" in extracted["required_skills"]
        assert "postgresql" in extracted["required_skills"]
        assert "docker" in extracted["required_skills"]
        assert "kubernetes" in extracted["preferred_skills"] or "kubernetes" in extracted["all_technologies"]
        assert extracted["content_hash"] is not None

    def test_classify_keywords_against_candidate_truth(self):
        """Test keyword classification into SUPPORTED (with evidence), and strictly UNSUPPORTED."""
        extracted = {
            "required_skills": ["python", "django", "postgresql", "rust", "solidity"],
            "preferred_skills": ["docker", "kubernetes"],
            "ats_keywords": ["python", "django", "postgresql", "rust", "solidity", "docker", "kubernetes"]
        }

        classification = classify_keywords_against_candidate(extracted, self.candidate)

        # Supported verified skills
        assert classification["python"]["status"] == "SUPPORTED"
        assert classification["django"]["status"] == "SUPPORTED"
        assert classification["postgresql"]["status"] == "SUPPORTED"
        assert classification["docker"]["status"] == "SUPPORTED"

        # Strictly unsupported skills
        assert classification["rust"]["status"] == "UNSUPPORTED"
        assert classification["rust"]["confidence"] == 0.0
        assert classification["solidity"]["status"] == "UNSUPPORTED"
        assert classification["solidity"]["confidence"] == 0.0

    def test_calculate_ats_match_score_and_priority_band(self):
        """Test non-inflated ATS match score calculation and priority band assignment."""
        extracted = {
            "title": "Senior Backend Engineer",
            "required_skills": ["python", "django", "postgresql"],
            "preferred_skills": ["docker"],
            "min_years_experience": 3,
            "education_requirements": ["computer science"]
        }

        score_info = calculate_ats_match_score(extracted, self.candidate)
        assert score_info["overall_score"] >= 80
        assert score_info["priority_band"] in ["Excellent Match", "Strong Match"]
        assert "python" in score_info["supported_skills"]
        assert "django" in score_info["supported_skills"]
        assert "postgresql" in score_info["supported_skills"]

    def test_unsupported_skills_reduce_score_without_inflation(self):
        """Test that missing required skills lower the match score and assign lower priority band."""
        extracted_unsupported = {
            "title": "Solidity Blockchain Lead",
            "required_skills": ["solidity", "rust", "web3", "c++"],
            "preferred_skills": ["kubernetes"],
            "min_years_experience": 8,
            "education_requirements": ["phd"]
        }

        score_info = calculate_ats_match_score(extracted_unsupported, self.candidate)
        assert score_info["overall_score"] < 50
        assert score_info["priority_band"] == "Low Match / Reject"
        assert len(score_info["unsupported_skills"]) >= 3
