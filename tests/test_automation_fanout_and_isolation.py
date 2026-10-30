# tests/test_automation_fanout_and_isolation.py
import pytest
from django.contrib.auth.models import User
from openoutreach.core.models import Campaign, Task
from openoutreach.linkedin.models import LinkedInProfile
from openoutreach.core.scheduler import plan_follow_up_window, plan_connect_window
from openoutreach.linkedin.tasks.connect import handle_connect
from openoutreach.linkedin.tasks.follow_up import handle_follow_up
from openoutreach.linkedin.tasks.check_pending import handle_check_pending
from unittest.mock import MagicMock


@pytest.mark.django_db
class TestAutomationFanoutAndIsolation:
    @pytest.fixture(autouse=True)
    def setup_data(self):
        self.user = User.objects.create_user(username="testfanout", email="fanout@example.com")
        self.profile = LinkedInProfile.objects.create(
            user=self.user,
            linkedin_username="testfanout",
            session_status="checkpoint",
            connect_daily_limit=20,
            follow_up_daily_limit=25,
        )
        self.campaign = Campaign.objects.create(name="Test Fanout Campaign", is_active=True)
        self.campaign.users.add(self.user)
        self.session = MagicMock()
        self.session.campaign = self.campaign
        self.session.linkedin_profile = self.profile
        self.session.active_timezone = "UTC"

    def test_no_500_task_fanout_on_unlimited_or_bounded(self):
        """Verify scheduler allocates a bounded count (max daily limit, e.g. 25) instead of 500 tasks."""
        planned = plan_follow_up_window(self.session, self.campaign)
        assert planned <= 25
        assert Task.objects.filter(task_type=Task.TaskType.FOLLOW_UP, status=Task.Status.PENDING).count() <= 25

    def test_checkpointed_linkedin_skips_writes_gracefully(self):
        """Verify handlers skip execution when session_status == checkpoint without crashing or modifying DB."""
        from django.utils import timezone
        task = Task.objects.create(task_type=Task.TaskType.CONNECT, scheduled_at=timezone.now(), payload={"campaign_id": self.campaign.id})
        
        # Handlers should log warning and return early
        handle_connect(task, self.session, {})
        handle_follow_up(task, self.session, {})
        handle_check_pending(task, self.session, {})

        assert task.status == Task.Status.PENDING
