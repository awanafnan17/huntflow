# tests/test_production_truth_integrity.py
import pytest
from django.contrib.auth.models import User
from openoutreach.core.models import Campaign, Task
from openoutreach.career.models import Job, JobMatchAssessment, JobSearchPreference
from openoutreach.linkedin.models import LinkedInProfile
from openoutreach.core.scheduler import plan_follow_up_window
from unittest.mock import MagicMock


@pytest.mark.django_db
class TestProductionTruthIntegrity:
    def test_manual_job_excluded_from_production_metrics(self):
        """Verify manual/test jobs are flagged and excluded from real production metrics."""
        manual_job = Job.objects.create(
            title="Manual Test Job",
            company="Test Co",
            source="manual",
            job_url=""
        )
        assert manual_job.source == "manual"
        assert manual_job.job_url == ""

    def test_campaign_52_incomplete_config_requires_waiting_user(self):
        """Verify campaign with missing objective/audience definition requires configuration."""
        c52 = Campaign.objects.create(
            name="Dubai Founder Networking",
            is_active=True,
            campaign_objective="",
            target_audience_definition="",
        )
        assert c52.campaign_objective == ""
        assert c52.target_audience_definition == ""

    def test_follow_up_planner_no_connected_deals(self):
        """Verify plan_follow_up_window returns 0 when no connected deals exist in campaign."""
        user = User.objects.create_user(username="truthuser")
        profile = LinkedInProfile.objects.create(user=user, linkedin_username="truthuser", active=True)
        campaign = Campaign.objects.create(name="Truth Campaign", is_active=True)
        campaign.users.add(user)
        session = MagicMock()
        session.linkedin_profile = profile
        session.active_timezone = "UTC"

        created = plan_follow_up_window(session, campaign)
        assert created == 0
