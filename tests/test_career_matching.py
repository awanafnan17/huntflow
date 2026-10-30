import pytest
import numpy as np
from unittest.mock import patch, MagicMock
from django.urls import reverse
from django.contrib.auth.models import User
from openoutreach.career.models import (
    CandidateProfile,
    CandidateProfileVersion,
    CandidateFact,
    Job,
    CandidateAuditEvent
)
from openoutreach.career.service import (
    build_candidate_profile_text,
    compute_job_match,
    JobMatchResult
)
from tests.factories import UserFactory

@pytest.mark.django_db
class TestCareerMatching:

    @pytest.fixture(autouse=True)
    def _setup(self, client, db):
        from openoutreach.core.models import SiteConfig
        cfg = SiteConfig.load()
        cfg.llm_api_key = "dummy-key"
        cfg.ai_model = "openai:gpt-4o"
        cfg.save()

        self.user = UserFactory(username="match_seeker", email="seeker@match.com")
        self.profile = CandidateProfile.objects.create(
            user=self.user,
            full_name="Alice Candidate",
            email="seeker@match.com",
            summary="Python Developer with Cloud experience"
        )
        self.profile_version = CandidateProfileVersion.objects.create(
            profile=self.profile,
            version_number=1,
            full_name=self.profile.full_name,
            email=self.profile.email,
            summary=self.profile.summary,
            created_by=self.user
        )
        # Add some facts
        CandidateFact.objects.create(
            profile_version=self.profile_version,
            fact_type="experience",
            category="Stark Industries",
            value={"company": "Stark Industries", "title": "Software Engineer", "bullet_points": ["Wrote code", "Fixed bugs"]}
        )
        CandidateFact.objects.create(
            profile_version=self.profile_version,
            fact_type="skill",
            category="Skill",
            value={"name": "Django"}
        )
        
        self.job = Job.objects.create(
            title="Django Developer",
            company="Oscorp",
            description="Looking for Alice Candidate or anyone with Django/Python experience.",
            source="manual"
        )
        client.force_login(self.user)
        self.client = client

    def test_build_candidate_profile_text(self):
        text = build_candidate_profile_text(self.profile_version)
        assert "Alice Candidate" in text
        assert "Python Developer" in text
        assert "Stark Industries" in text
        assert "Django" in text

    @patch("openoutreach.career.service.run_agent_sync")
    def test_compute_job_match_service(self, mock_run):
        def side_effect(coro):
            coro.close()
            return MagicMock(
                output=JobMatchResult(
                    match_score=85,
                    match_explanation="Strong Django match, some gaps in cloud experience."
                )
            )
        mock_run.side_effect = side_effect
        
        res = compute_job_match(self.profile_version, self.job)
        assert res.match_score == 85
        assert "Strong Django match" in res.match_explanation

    @patch("openoutreach.career.service.run_agent_sync")
    def test_job_match_view_post(self, mock_run):
        def side_effect(coro):
            coro.close()
            return MagicMock(
                output=JobMatchResult(
                    match_score=92,
                    match_explanation="Excellent match with Django and Python skills."
                )
            )
        mock_run.side_effect = side_effect

        url = reverse("career:job_match", kwargs={"job_pk": self.job.pk})
        resp = self.client.post(url)
        assert resp.status_code == 302  # redirects back to detail page
        
        self.job.refresh_from_db()
        assert self.job.match_score == 92
        assert "Excellent match" in self.job.match_explanation

        # Verify security audit event logged
        assert CandidateAuditEvent.objects.filter(
            user=self.user,
            event_type="job_matched",
            description__contains="92%"
        ).exists()

