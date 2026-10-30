# tests/test_campaign_fairness.py
"""Tests for Campaign controls, fair scheduling, and campaign-level caps (Batch 7)."""
from __future__ import annotations

import pytest
from django.utils import timezone
from datetime import timedelta

from openoutreach.core.models import Campaign, Task
from openoutreach.linkedin.models import LinkedInProfile, ActionLog
from openoutreach.core.scheduler import allocate_slots, plan_connect_window, plan_follow_up_window
from tests.factories import UserFactory


@pytest.mark.django_db
class TestCampaignFairnessAndControls:

    def test_campaign_fields_default(self):
        """Verify the default values for the new Campaign fields."""
        c = Campaign.objects.create(name="Test Campaign 1")
        assert c.is_active is True
        assert c.daily_connect_limit == 20
        assert c.daily_follow_up_limit == 25

    def test_pending_executable_filtering(self):
        """Verify that pending_executable excludes paused campaigns' tasks while pending includes them."""
        # 1. Setup campaigns
        u = UserFactory()
        c_active = Campaign.objects.create(name="Active Campaign", is_active=True)
        c_paused = Campaign.objects.create(name="Paused Campaign", is_active=False)
        c_active.users.add(u)
        c_paused.users.add(u)

        # 2. Setup tasks
        now = timezone.now()
        t_active = Task.objects.create(
            task_type=Task.TaskType.CONNECT,
            status=Task.Status.PENDING,
            scheduled_at=now - timedelta(minutes=5),
            payload={"campaign_id": c_active.pk},
        )
        t_paused = Task.objects.create(
            task_type=Task.TaskType.CONNECT,
            status=Task.Status.PENDING,
            scheduled_at=now - timedelta(minutes=10),
            payload={"campaign_id": c_paused.pk},
        )

        # 3. Assert pending returns both
        all_pending = list(Task.objects.pending())
        assert t_active in all_pending
        assert t_paused in all_pending

        # 4. Assert pending_executable excludes paused task
        exec_pending = list(Task.objects.pending_executable())
        assert t_active in exec_pending
        assert t_paused not in exec_pending

        # 5. Assert claim_next only claims the active campaign's task
        claimed = Task.objects.claim_next()
        assert claimed == t_active

    def test_allocate_slots_fair_share(self):
        """Verify slot allocation logic distributing remaining capacity fairly."""
        c1 = Campaign(pk=1, daily_connect_limit=5)
        c2 = Campaign(pk=2, daily_connect_limit=None)
        c3 = Campaign(pk=3, daily_connect_limit=10)
        campaigns = [c1, c2, c3]

        profile = LinkedInProfile(connect_daily_limit=20)
        # Mock _campaign_daily_count helper to simulate 0 done today
        from openoutreach.core import scheduler
        def mock_daily_count(p, c, action_type):
            return 0

        original_helper = scheduler._campaign_daily_count
        scheduler._campaign_daily_count = mock_daily_count
        try:
            allocations = allocate_slots(
                total_limit=20,
                daily_count=0,
                campaigns=campaigns,
                limit_attr="daily_connect_limit",
                profile=profile,
                action_type="connect"
            )
            # Expect allocations: c1 capped at 5, remainder 15 split across c2 and c3.
            # C2: 8, C3: 7.
            assert allocations[1] == 5
            assert allocations[2] == 8
            assert allocations[3] == 7
            assert sum(allocations.values()) == 20
        finally:
            scheduler._campaign_daily_count = original_helper

    def test_planning_gates_paused_campaigns(self):
        """Verify that scheduling does not plan tasks for paused campaigns."""
        # Setup session, active/inactive profile, etc.
        u = UserFactory()
        profile = LinkedInProfile.objects.create(
            user=u,
            linkedin_username="test@example.com",
            linkedin_password="pwd",
            connect_daily_limit=20,
        )

        c_paused = Campaign.objects.create(name="Paused Campaign", is_active=False)
        c_paused.users.add(u)

        class MockSession:
            def __init__(self):
                self.linkedin_profile = profile
                self.active_timezone = None
                self.campaigns = [c_paused]

        session = MockSession()

        # Try planning connect window on paused campaign
        created_conn = plan_connect_window(session, c_paused)
        assert created_conn == 0
        assert not Task.objects.filter(payload__campaign_id=c_paused.pk).exists()

        # Try planning follow up window
        created_fu = plan_follow_up_window(session, c_paused)
        assert created_fu == 0
        assert not Task.objects.filter(payload__campaign_id=c_paused.pk).exists()
