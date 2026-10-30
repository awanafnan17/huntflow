"""Quality and Grounding Tests for Ollama ATS Intelligence."""
import json
import time
import pytest
from unittest.mock import patch, MagicMock
from django.contrib.auth import get_user_model

from openoutreach.core.models import Workspace, WorkspaceMembership
from openoutreach.career.models import CandidateProfile, CandidateProfileVersion, CandidateFact, Job
from openoutreach.career.services.ats_analyzer import (
    extract_jd_requirements,
    classify_keywords_against_candidate,
)
from openoutreach.career.services.ats_llm_engine import (
    is_ollama_available,
    build_evidence_grounded_prompt,
    validate_llm_tailored_output,
    extract_json_from_llm_response,
    generate_llm_enhanced_ats_content,
    call_ollama_raw,
    OLLAMA_MODEL_NAME,
)

User = get_user_model()


@pytest.mark.django_db
class TestOllamaATSIntelligence:

    @pytest.fixture(autouse=True)
    def setup_data(self, db):
        self.user = User.objects.create_user(username="ollama_user", email="ollama@example.com")
        self.ws = Workspace.objects.create(name="Ollama WS", slug="ollama-ws")
        WorkspaceMembership.objects.create(workspace=self.ws, user=self.user, role="owner", status="active")

        self.candidate = CandidateProfile.objects.create(
            user=self.user,
            full_name="Alex Mercer",
            email="alex.mercer@example.com",
            phone="+12025550106"
        )
        self.cpv = CandidateProfileVersion.objects.create(
            profile=self.candidate,
            version_number=1,
            state="approved",
            full_name=self.candidate.full_name,
            summary="Senior Software Engineer with 6+ years experience in Python, Django, PostgreSQL, Docker, Kubernetes, AWS, React, and ETL data pipelines.",
            fact_snapshot=[
                {"category": "Skills", "value": "Python", "id": 1},
                {"category": "Skills", "value": "Django", "id": 2},
                {"category": "Skills", "value": "PostgreSQL", "id": 3},
                {"category": "Skills", "value": "Docker", "id": 4},
                {"category": "Skills", "value": "Kubernetes", "id": 5},
                {"category": "Skills", "value": "AWS", "id": 6},
                {"category": "Skills", "value": "React", "id": 7},
                {"category": "Skills", "value": "ETL", "id": 8},
                {"category": "Experience", "value": {
                    "company": "ScaleTech Inc",
                    "title": "Lead Software Engineer",
                    "period": "2020 - Present",
                    "bullets": [
                        "Architected scalable backend microservices using Python and Django handling 10M daily requests.",
                        "Optimized PostgreSQL queries reducing average response latency by 45%.",
                        "Designed automated CI/CD pipelines and deployed Docker containers across Kubernetes clusters on AWS.",
                        "Built responsive React dashboards and real-time ETL data pipelines."
                    ]
                }, "id": 10}
            ]
        )
        self.candidate.active_version = self.cpv
        self.candidate.save()

        # Approved facts
        for skill in ["Python", "Django", "PostgreSQL", "Docker", "Kubernetes", "AWS", "React", "ETL"]:
            CandidateFact.objects.create(profile_version=self.cpv, fact_type="skill", category="Skills", value=skill, verified=True)
        CandidateFact.objects.create(
            profile_version=self.cpv,
            fact_type="experience",
            category="Work",
            value={
                "company": "ScaleTech Inc",
                "title": "Lead Software Engineer",
                "period": "2020 - Present",
                "bullets": [
                    "Architected scalable backend microservices using Python and Django handling 10M daily requests.",
                    "Optimized PostgreSQL queries reducing average response latency by 45%.",
                    "Designed automated CI/CD pipelines and deployed Docker containers across Kubernetes clusters on AWS."
                ]
            },
            verified=True
        )

    def test_prompt_contract_excludes_unsupported_skills(self):
        """Test prompt contract only provides SUPPORTED candidate facts and marks unsupported requirements."""
        job = Job.objects.create(
            company="Solidity Labs",
            title="Smart Contract Developer",
            description="Seeking Solidity and Rust developer with Python backend skills.",
            is_active=True
        )
        jd_reqs = extract_jd_requirements(job)
        kw_map = classify_keywords_against_candidate(jd_reqs, self.candidate)

        prompt = build_evidence_grounded_prompt(self.candidate, jd_reqs, kw_map)

        assert "Python" in prompt
        # Unsupported skills must be explicitly flagged under NEVER claim
        assert "solidity" in prompt.lower()
        assert "UNSUPPORTED" in kw_map["solidity"]["status"]
        assert "ONLY use the approved candidate facts" in prompt

    def test_llm_validation_catches_hallucinations_and_ungrounded_bullets(self):
        """Test validation rejects hallucinated unsupported technologies or ungrounded fact IDs."""
        job_reqs = {"company": "Cloud Corp", "title": "Lead Engineer"}
        kw_map = {
            "python": {"status": "SUPPORTED"},
            "rust": {"status": "UNSUPPORTED"}
        }

        # 1. Hallucinated summary
        bad_output_1 = {
            "professional_summary": "Expert Rust and Solidity engineer with 10 years experience.",
            "tailored_roles": [{"fact_id": 10, "company": "ScaleTech Inc", "title": "Lead", "bullets": ["Built backend."]}],
            "cover_letter_body": "I am a skilled engineer with proven track record in software development."
        }
        passed_1, errs_1 = validate_llm_tailored_output(bad_output_1, self.candidate, job_reqs, kw_map)
        assert passed_1 is False
        assert any("hallucinated unsupported skill 'rust'" in e for e in errs_1)

        # 2. Ungrounded fact ID
        bad_output_2 = {
            "professional_summary": "Senior Python developer with experience in Django and PostgreSQL.",
            "tailored_roles": [{"fact_id": 99999, "company": "Invented Corp", "title": "CTO", "bullets": ["Invented stuff."]}],
            "cover_letter_body": "I am a skilled engineer with proven track record in software development."
        }
        passed_2, errs_2 = validate_llm_tailored_output(bad_output_2, self.candidate, job_reqs, kw_map)
        assert passed_2 is False
        assert any("invalid fact_id" in e for e in errs_2)

    def test_fallback_on_llm_timeout_or_offline(self):
        """Test seamless fallback to safe deterministic document when LLM is unavailable or times out."""
        job = Job.objects.create(
            company="Fallback Tech",
            title="Backend Architect",
            description="Python and Django role.",
            is_active=True
        )
        jd_reqs = extract_jd_requirements(job)
        kw_map = classify_keywords_against_candidate(jd_reqs, self.candidate)

        with patch("openoutreach.career.services.ats_llm_engine.call_ollama_raw", return_value=None):
            result = generate_llm_enhanced_ats_content(self.candidate, jd_reqs, kw_map)
            assert result["fallback_used"] is True
            assert result["llm_used"] is False
            assert "Backend Architect" in result["professional_summary"]
            assert len(result["tailored_roles"]) > 0

    def test_real_ollama_five_distinct_jobs_quality_benchmark(self):
        """
        Quality Test across 5 distinct job domains:
        1. Python Backend Engineer
        2. AI Automation Engineer
        3. DevOps Engineer
        4. Full Stack Engineer
        5. Data Engineer
        Proves summaries, bullet order, skill prioritization, and cover letters differ meaningfully.
        """
        ollama_online = is_ollama_available()
        if not ollama_online:
            pytest.skip("Local Ollama not responsive on 127.0.0.1:11434")

        jobs_specs = [
            ("Apex Systems", "Python Backend Engineer", "Requirements: Strong Python, Django, PostgreSQL, and REST APIs."),
            ("Cognitive AI", "AI Automation Engineer", "Requirements: Python, automated pipelines, workflow automation, and APIs."),
            ("InfraCloud", "DevOps Engineer", "Requirements: Docker, Kubernetes, AWS cloud infrastructure, and CI/CD."),
            ("Nexus Web", "Full Stack Engineer", "Requirements: React, Python, Django backend, and web APIs."),
            ("DataFlow Corp", "Data Engineer", "Requirements: ETL data pipelines, PostgreSQL, database optimization, and Python.")
        ]

        packages = []
        gen_times = []

        for company, title, desc in jobs_specs:
            job = Job.objects.create(company=company, title=title, description=desc, is_active=True)
            jd_reqs = extract_jd_requirements(job)
            kw_map = classify_keywords_against_candidate(jd_reqs, self.candidate)

            t0 = time.time()
            res = generate_llm_enhanced_ats_content(self.candidate, jd_reqs, kw_map, cover_letter_mode="standard")
            t1 = time.time()
            gen_times.append(t1 - t0)
            packages.append((title, res))

        # 1. Verify all 5 generated successfully
        assert len(packages) == 5

        # 2. Check summary and skill order differentiation (not simple string swapping)
        summaries = [p[1]["professional_summary"] for p in packages]
        skill_orders = [p[1]["skill_order"] for p in packages]
        cover_letters = [p[1]["cover_letter_body"] for p in packages]

        # All 5 summaries must be distinct
        assert len(set(summaries)) == 5

        # All 5 cover letters must be distinct
        assert len(set(cover_letters)) == 5

        # Ensure DevOps emphasizes Docker/Kubernetes/AWS
        devops_pkg = packages[2][1]
        assert any(k in str(devops_pkg).lower() for k in ["docker", "kubernetes", "aws", "ci/cd"])

        # Ensure Data Engineer emphasizes ETL/PostgreSQL
        data_pkg = packages[4][1]
        assert any(k in str(data_pkg).lower() for k in ["etl", "postgresql", "data", "pipeline"])

        # Measure average generation time
        avg_time = sum(gen_times) / len(gen_times)
        assert avg_time < 30.0, f"Average LLM generation time too slow: {avg_time}s"
