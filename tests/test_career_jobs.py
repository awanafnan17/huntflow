import pytest
from django.urls import reverse
from openoutreach.career.models import CandidateProfile, Job, JobApplication
from tests.factories import UserFactory

@pytest.mark.django_db
class TestCareerJobs:

    @pytest.fixture(autouse=True)
    def _setup(self, client, db):
        self.user = UserFactory(username="seeker1", email="seeker1@example.com")
        self.profile = CandidateProfile.objects.create(
            user=self.user,
            full_name="John Doe",
            email="seeker1@example.com"
        )
        client.force_login(self.user)
        self.client = client

    def test_job_manual_import_workflow(self):
        url_add = reverse("career:job_add")
        
        # 1. Post a new job description
        resp = self.client.post(url_add, {
            "title": "Senior Python Engineer",
            "company": "TechCorp",
            "location": "San Francisco, CA",
            "description": "We are looking for an expert Python developer with Django knowledge.",
            "job_url": "https://techcorp.com/jobs/123",
            "source": "LinkedIn"
        })
        assert resp.status_code == 302  # redirects to list

        # Verify Job model is created
        assert Job.objects.filter(company="TechCorp", title="Senior Python Engineer").exists()
        job = Job.objects.get(company="TechCorp")
        assert job.location == "San Francisco, CA"
        assert job.source == "LinkedIn"
        assert job.job_url == "https://techcorp.com/jobs/123"

        # 2. Check listings view
        url_list = reverse("career:job_list")
        resp_list = self.client.get(url_list)
        assert resp_list.status_code == 200
        assert "TechCorp" in resp_list.content.decode("utf-8")

        # 3. Check detail view
        url_detail = reverse("career:job_detail", kwargs={"job_pk": job.pk})
        resp_detail = self.client.get(url_detail)
        assert resp_detail.status_code == 200
        assert "Senior Python Engineer" in resp_detail.content.decode("utf-8")

    def test_job_detail_with_started_application(self):
        job = Job.objects.create(
            title="DevOps Lead",
            company="CloudScale",
            description="Manage Kubernetes infrastructure."
        )
        # Create a draft application
        app = JobApplication.objects.create(
            candidate_profile=self.profile,
            job=job,
            status="draft",
            cover_letter="Draft cover letter..."
        )

        url_detail = reverse("career:job_detail", kwargs={"job_pk": job.pk})
        resp = self.client.get(url_detail)
        assert resp.status_code == 200
        content = resp.content.decode("utf-8")
        assert "DevOps Lead" in content
        assert "Draft" in content
        assert "Review & Edit Application Drafts" in content
