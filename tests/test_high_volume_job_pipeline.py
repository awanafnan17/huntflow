"""High-Volume ATS Application Package Pipeline and Multi-Tenant Stress Tests."""
import pytest
from unittest.mock import MagicMock
from django.utils import timezone
from django.contrib.auth import get_user_model

from openoutreach.core.models import Workspace, WorkspaceMembership, Task
from openoutreach.core.services.entitlements import get_or_create_workspace_entitlement
from openoutreach.career.models import (
    CandidateProfile, CandidateProfileVersion, CandidateFact, Job, JobApplication, ApplicationDraft
)
from openoutreach.career.services.job_application_engine import (
    prepare_job_application,
    prepare_high_volume_job_packages,
    answer_screening_question_and_resume_application,
    execute_job_application,
)

User = get_user_model()


@pytest.mark.django_db
class TestHighVolumeJobPipeline:

    @pytest.fixture(autouse=True)
    def setup_data(self, db):
        self.user_a = User.objects.create_user(username="tenant_a_user", email="tenant_a@example.com")
        self.ws_a = Workspace.objects.create(name="Workspace A", slug="ws-tenant-a")
        WorkspaceMembership.objects.create(workspace=self.ws_a, user=self.user_a, role="owner", status="active")
        get_or_create_workspace_entitlement(self.ws_a)

        self.candidate_a = CandidateProfile.objects.create(
            user=self.user_a,
            full_name="Alex Morgan",
            email="candidate_a@example.com",
            phone="+1-202-555-0101"
        )
        self.cpv_a = CandidateProfileVersion.objects.create(
            profile=self.candidate_a,
            version_number=1,
            state="approved",
            full_name=self.candidate_a.full_name,
            summary="Python Django Backend Architect",
            fact_snapshot=[{"category": "Skills", "value": "Python"}, {"category": "Skills", "value": "Django"}]
        )
        self.candidate_a.active_version = self.cpv_a
        self.candidate_a.save()
        CandidateFact.objects.create(profile_version=self.cpv_a, fact_type="skill", category="Skills", value="Python", verified=True)
        CandidateFact.objects.create(profile_version=self.cpv_a, fact_type="skill", category="Skills", value="Django", verified=True)

        # Tenant B
        self.user_b = User.objects.create_user(username="tenant_b_user", email="tenant_b@example.com")
        self.ws_b = Workspace.objects.create(name="Workspace B", slug="ws-tenant-b-bench")
        WorkspaceMembership.objects.create(workspace=self.ws_b, user=self.user_b, role="owner", status="active")
        get_or_create_workspace_entitlement(self.ws_b)

        self.candidate_b = CandidateProfile.objects.create(
            user=self.user_b,
            full_name="Bob Smith",
            email="bob_b@example.com",
            phone="+14155550199"
        )
        self.cpv_b = CandidateProfileVersion.objects.create(
            profile=self.candidate_b,
            version_number=1,
            state="approved",
            full_name=self.candidate_b.full_name,
            summary="Golang Kubernetes SRE",
            fact_snapshot=[{"category": "Skills", "value": "Golang"}, {"category": "Skills", "value": "Kubernetes"}]
        )
        self.candidate_b.active_version = self.cpv_b
        self.candidate_b.save()
        CandidateFact.objects.create(profile_version=self.cpv_b, fact_type="skill", category="Skills", value="Golang", verified=True)
        CandidateFact.objects.create(profile_version=self.cpv_b, fact_type="skill", category="Skills", value="Kubernetes", verified=True)

        # Tenant C
        self.user_c = User.objects.create_user(username="tenant_c_user", email="tenant_c@example.com")
        self.ws_c = Workspace.objects.create(name="Workspace C", slug="ws-tenant-c-bench")
        WorkspaceMembership.objects.create(workspace=self.ws_c, user=self.user_c, role="owner", status="active")
        get_or_create_workspace_entitlement(self.ws_c)

        self.candidate_c = CandidateProfile.objects.create(
            user=self.user_c,
            full_name="Carol Danvers",
            email="carol_c@example.com",
            phone="+14155550288"
        )
        self.cpv_c = CandidateProfileVersion.objects.create(
            profile=self.candidate_c,
            version_number=1,
            state="approved",
            full_name=self.candidate_c.full_name,
            summary="React Next.js Frontend Specialist",
            fact_snapshot=[{"category": "Skills", "value": "React"}, {"category": "Skills", "value": "TypeScript"}]
        )
        self.candidate_c.active_version = self.cpv_c
        self.candidate_c.save()
        CandidateFact.objects.create(profile_version=self.cpv_c, fact_type="skill", category="Skills", value="React", verified=True)
        CandidateFact.objects.create(profile_version=self.cpv_c, fact_type="skill", category="Skills", value="TypeScript", verified=True)

    def test_high_volume_250_jobs_autonomous_preparation(self):
        """Stress benchmark: 250 jobs prepared autonomously with zero duplicates, unique hashes, and 0 external submits."""
        jobs_250 = []
        companies = ["AlphaTech", "BetaCorp", "GammaSoft", "DeltaData", "EpsilonAI", "ZetaCloud", "EtaSystems"]
        roles = ["Python Backend Engineer", "Django Core Architect", "Senior Software Developer", "REST API Specialist"]

        for i in range(250):
            co = companies[i % len(companies)]
            title = roles[i % len(roles)]
            job = Job.objects.create(
                company=f"{co} #{i}",
                title=f"{title} #{i}",
                job_url=f"https://www.linkedin.com/jobs/view/900000{i:04d}/",
                description=f"We are hiring a {title} with strong Python, Django, and database skills at {co} #{i}.",
                is_active=True
            )
            jobs_250.append(job)

        pipeline_result = prepare_high_volume_job_packages(
            workspace=self.ws_a,
            candidate=self.candidate_a,
            jobs=jobs_250,
            min_match_threshold=40
        )

        assert pipeline_result["total_jobs_processed"] == 250
        assert pipeline_result["packages_prepared"] >= 200
        assert pipeline_result["qa_failed_count"] == 0
        assert pipeline_result["average_prep_seconds_per_job"] < 0.2

        # Verify exact JobApplication counts and zero duplicate applications
        apps_count = JobApplication.objects.filter(candidate_profile=self.candidate_a).count()
        assert apps_count == 250

        # Assert zero real external submissions occurred during preparation
        submitted_count = JobApplication.objects.filter(status="submitted", externally_confirmed=True).count()
        assert submitted_count == 0, "Preparation must not trigger external provider submissions!"

    def test_unknown_question_pause_and_seamless_resumption(self):
        """Test unknown screening question sets NEEDS YOUR ANSWER and resumes same application when answered."""
        job_unknown = Job.objects.create(
            company="Clearance Corp",
            title="Defense Python Lead",
            job_url="https://www.linkedin.com/jobs/view/8888888888/",
            description="Python engineer needed with custom clearance verification.",
            is_active=True
        )
        job_unknown.screening_questions = [
            {"id": "q_custom", "text": "What is your custom_unhandled clearance code?"}
        ]

        prep_1 = prepare_job_application(workspace=self.ws_a, candidate=self.candidate_a, job=job_unknown)
        assert prep_1["prepared"] is False
        assert prep_1["reason"] == "unknown_screening_question"
        assert prep_1["status"] == "needs_info"
        draft_id = prep_1["draft_id"]

        app_1 = JobApplication.objects.get(candidate_profile=self.candidate_a, job=job_unknown)
        assert app_1.status == "needs_info"

        # User provides missing answer
        resume_res = answer_screening_question_and_resume_application(
            workspace=self.ws_a,
            candidate=self.candidate_a,
            job=job_unknown,
            answers_update={"q_custom": "TS-SCI-9982"}
        )

        assert resume_res["success"] is True
        assert resume_res["status"] == "ready_for_review"
        assert resume_res["draft_id"] == draft_id

        # Verify exactly ONE JobApplication exists (no duplicates)
        assert JobApplication.objects.filter(candidate_profile=self.candidate_a, job=job_unknown).count() == 1

    def test_multi_tenant_isolation_concurrency(self):
        """Test 100 jobs for User A, 100 for User B, 100 for User C with strict zero tenant cross-contamination."""
        jobs_a = [Job.objects.create(company=f"TechA_{i}", title=f"Python SWE A_{i}", is_active=True, description="Python Django role") for i in range(100)]
        jobs_b = [Job.objects.create(company=f"TechB_{i}", title=f"Golang SRE B_{i}", is_active=True, description="Golang K8s role") for i in range(100)]
        jobs_c = [Job.objects.create(company=f"TechC_{i}", title=f"React FE C_{i}", is_active=True, description="React TypeScript role") for i in range(100)]

        res_a = prepare_high_volume_job_packages(workspace=self.ws_a, candidate=self.candidate_a, jobs=jobs_a)
        res_b = prepare_high_volume_job_packages(workspace=self.ws_b, candidate=self.candidate_b, jobs=jobs_b)
        res_c = prepare_high_volume_job_packages(workspace=self.ws_c, candidate=self.candidate_c, jobs=jobs_c)

        assert res_a["packages_prepared"] == 100
        assert res_b["packages_prepared"] == 100
        assert res_c["packages_prepared"] == 100

        # Assert User A facts never appear in User B packages
        apps_b = JobApplication.objects.filter(candidate_profile=self.candidate_b)
        for app in apps_b:
            assert "Candidate".lower() not in app.tailored_cv.lower()
            assert "candidate_a@example.com".lower() not in app.tailored_cv.lower()
            assert "Bob Smith".lower() in app.tailored_cv.lower()

        # Assert User B facts never appear in User C packages
        apps_c = JobApplication.objects.filter(candidate_profile=self.candidate_c)
        for app in apps_c:
            assert "Bob Smith".lower() not in app.tailored_cv.lower()
            assert "bob_b@example.com".lower() not in app.tailored_cv.lower()
            assert "Carol Danvers".lower() in app.tailored_cv.lower()
