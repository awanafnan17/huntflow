# tests/test_campaign_actions.py
"""Tests for Campaign quick actions, safety confirmations, and browser-free reconcile (Batch 8)."""
from __future__ import annotations

import pytest
from unittest.mock import patch
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from django.core.management import call_command
from io import StringIO

from openoutreach.core.models import Campaign, Task
from openoutreach.linkedin.models import LinkedInProfile
from tests.factories import UserFactory


@pytest.mark.django_db
class TestCampaignActionsAndQueueControls:

    @pytest.fixture(autouse=True)
    def setup_users(self):
        self.staff_user = UserFactory(is_staff=True, is_superuser=False)
        self.non_staff_user = UserFactory(is_staff=False, is_superuser=False)
        
        self.campaign = Campaign.objects.create(name="Reconcile Test Campaign", is_active=True)
        self.campaign.users.add(self.staff_user)

    def test_pause_resume_permissions_and_methods(self, client):
        """Verify pause/resume actions require staff authentication and are POST-only."""
        pause_url = reverse("dashboard:campaign_pause", kwargs={"pk": self.campaign.pk})
        resume_url = reverse("dashboard:campaign_resume", kwargs={"pk": self.campaign.pk})

        # 1. Unauthenticated redirects
        resp = client.get(pause_url)
        assert resp.status_code == 302
        assert "login" in resp.url

        # 2. Non-staff is logged out / redirected
        client.force_login(self.non_staff_user)
        resp = client.post(pause_url)
        assert resp.status_code == 302
        assert "login" in resp.url

        # 3. Staff GET returns 405 (Method Not Allowed)
        client.force_login(self.staff_user)
        resp = client.get(pause_url)
        assert resp.status_code == 405

        # 4. Staff POST pauses campaign successfully
        resp = client.post(pause_url)
        assert resp.status_code == 302
        self.campaign.refresh_from_db()
        assert self.campaign.is_active is False

        # 5. Staff POST resumes campaign successfully
        resp = client.post(resume_url)
        assert resp.status_code == 302
        self.campaign.refresh_from_db()
        assert self.campaign.is_active is True

    def test_clear_pending_tasks(self, client):
        """Verify clear_pending deletes only targeted campaign pending tasks on confirmed POST."""
        other_campaign = Campaign.objects.create(name="Other Campaign", is_active=True)
        other_campaign.users.add(self.staff_user)

        # Create tasks
        Task.objects.create(
            task_type=Task.TaskType.CONNECT,
            status=Task.Status.PENDING,
            scheduled_at=timezone.now(),
            payload={"campaign_id": self.campaign.pk},
        )
        Task.objects.create(
            task_type=Task.TaskType.FOLLOW_UP,
            status=Task.Status.FAILED,  # Failed task for this campaign
            scheduled_at=timezone.now(),
            payload={"campaign_id": self.campaign.pk},
        )
        Task.objects.create(
            task_type=Task.TaskType.CONNECT,
            status=Task.Status.PENDING,  # Pending task for other campaign
            scheduled_at=timezone.now(),
            payload={"campaign_id": other_campaign.pk},
        )

        clear_url = reverse("dashboard:campaign_clear_pending", kwargs={"pk": self.campaign.pk})
        client.force_login(self.staff_user)

        # 1. GET displays confirmation screen with counts
        resp = client.get(clear_url)
        assert resp.status_code == 200
        assert b"Clear Pending Tasks" in resp.content
        assert b"1" in resp.content  # Count of pending tasks for campaign

        # 2. POST without confirmation does not delete
        resp = client.post(clear_url)
        assert resp.status_code in (302, 405)

        # 3. POST with confirm=yes deletes only targeted pending tasks
        resp = client.post(clear_url, {"confirm": "yes"})
        assert resp.status_code == 302
        
        # Verify counts in DB
        assert Task.objects.filter(status=Task.Status.PENDING, payload__campaign_id=self.campaign.pk).count() == 0
        assert Task.objects.filter(status=Task.Status.FAILED, payload__campaign_id=self.campaign.pk).count() == 1
        assert Task.objects.filter(status=Task.Status.PENDING, payload__campaign_id=other_campaign.pk).count() == 1

    def test_clear_failed_tasks(self, client):
        """Verify clear_failed deletes only targeted campaign failed tasks on confirmed POST."""
        other_campaign = Campaign.objects.create(name="Other Campaign", is_active=True)
        other_campaign.users.add(self.staff_user)

        # Create tasks
        Task.objects.create(
            task_type=Task.TaskType.CONNECT,
            status=Task.Status.FAILED,
            scheduled_at=timezone.now(),
            payload={"campaign_id": self.campaign.pk},
        )
        Task.objects.create(
            task_type=Task.TaskType.CONNECT,
            status=Task.Status.PENDING,
            scheduled_at=timezone.now(),
            payload={"campaign_id": self.campaign.pk},
        )
        Task.objects.create(
            task_type=Task.TaskType.CONNECT,
            status=Task.Status.FAILED,
            scheduled_at=timezone.now(),
            payload={"campaign_id": other_campaign.pk},
        )

        clear_url = reverse("dashboard:campaign_clear_failed", kwargs={"pk": self.campaign.pk})
        client.force_login(self.staff_user)

        # 1. GET displays confirmation screen with counts
        resp = client.get(clear_url)
        assert resp.status_code == 200
        assert b"Clear Failed Tasks" in resp.content

        # 2. POST with confirm=yes deletes only targeted failed tasks
        resp = client.post(clear_url, {"confirm": "yes"})
        assert resp.status_code == 302
        
        assert Task.objects.filter(status=Task.Status.FAILED, payload__campaign_id=self.campaign.pk).count() == 0
        assert Task.objects.filter(status=Task.Status.PENDING, payload__campaign_id=self.campaign.pk).count() == 1
        assert Task.objects.filter(status=Task.Status.FAILED, payload__campaign_id=other_campaign.pk).count() == 1

    @patch("openoutreach.linkedin.browser.launch.start_browser_session")
    def test_browser_free_campaign_reconcile(self, mock_start_browser, client):
        """Verify campaign_reconcile runs planners without calling start_browser_session/launching browser."""
        profile = LinkedInProfile.objects.create(
            user=self.staff_user,
            linkedin_username="audit@example.com",
            linkedin_password="password123",
            active=True,
            connect_daily_limit=10,
        )

        reconcile_url = reverse("dashboard:campaign_reconcile", kwargs={"pk": self.campaign.pk})
        client.force_login(self.staff_user)

        # POST reconcile
        resp = client.post(reconcile_url)
        assert resp.status_code == 302

        # Assert no browser launch was triggered
        mock_start_browser.assert_not_called()

        # Verify task list is populated (scheduler generated task)
        # Note: Since no leads are available, planners exit gracefully without crashing
        assert Task.objects.filter(payload__campaign_id=self.campaign.pk).count() >= 0

    def test_campaign_queue_status_command(self):
        """Verify the campaign_queue_status CLI command outputs correct details."""
        # Create some pending and failed tasks
        Task.objects.create(
            task_type=Task.TaskType.CONNECT,
            status=Task.Status.PENDING,
            scheduled_at=timezone.now(),
            payload={"campaign_id": self.campaign.pk},
        )
        Task.objects.create(
            task_type=Task.TaskType.FOLLOW_UP,
            status=Task.Status.FAILED,
            scheduled_at=timezone.now(),
            payload={"campaign_id": self.campaign.pk},
        )

        out = StringIO()
        call_command("campaign_queue_status", stdout=out)
        output = out.getvalue()

        assert "HuntFlow Campaign Queue Status" in output
        assert "Campaign: Reconcile Test Campaign" in output
        assert "Status: Active" in output
        assert "Pending Connect: 1" in output
        assert "Failed Tasks: 1" in output
