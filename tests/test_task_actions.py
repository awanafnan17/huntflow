# tests/test_task_actions.py
from __future__ import annotations

import datetime
from unittest import mock
import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from openoutreach.core.models import Task, Campaign, TaskQueueEvent
from openoutreach.crm.models import Lead, Deal, DealState
from openoutreach.dashboard.views import mask_sensitive_payload

pytestmark = pytest.mark.django_db


@pytest.fixture
def db_setup(db):
    operator = User.objects.create_user(username="operator", is_staff=True)
    campaign = Campaign.objects.create(name="LinkedIn Connect Campaign", is_active=True)
    lead = Lead.objects.create(public_identifier="test-lead-public-id")
    deal = Deal.objects.create(lead=lead, campaign=campaign, state=DealState.QUALIFIED)
    return {
        "operator": operator,
        "campaign": campaign,
        "lead": lead,
        "deal": deal,
    }


def test_payload_masking_recursive():
    payload = {
        "campaign_id": 42,
        "api_key": "secret-12345",
        "nested": {
            "password": "my-password",
            "safe_key": "safe-value"
        },
        "list_items": [
            {"token": "my-token"},
            {"safe_item": 123}
        ]
    }
    masked = mask_sensitive_payload(payload)
    assert masked["campaign_id"] == 42
    assert masked["api_key"] == "********"
    assert masked["nested"]["password"] == "********"
    assert masked["nested"]["safe_key"] == "safe-value"
    assert masked["list_items"][0]["token"] == "********"
    assert masked["list_items"][1]["safe_item"] == 123


def test_daemon_claim_exclusions(db_setup):
    now = timezone.now()
    t_pending = Task.objects.create(
        task_type=Task.TaskType.CONNECT,
        status=Task.Status.PENDING,
        scheduled_at=now - datetime.timedelta(minutes=5),
        payload={"campaign_id": db_setup["campaign"].id}
    )
    t_cancelled = Task.objects.create(
        task_type=Task.TaskType.CONNECT,
        status=Task.Status.CANCELLED,
        scheduled_at=now - datetime.timedelta(minutes=5),
        payload={"campaign_id": db_setup["campaign"].id}
    )

    # pending() query set
    pending_tasks = list(Task.objects.pending())
    assert t_pending in pending_tasks
    assert t_cancelled not in pending_tasks

    # claim_next() path
    next_task = Task.objects.claim_next()
    assert next_task == t_pending


def test_task_detail_page_view(db_setup):
    task = Task.objects.create(
        task_type=Task.TaskType.CONNECT,
        status=Task.Status.PENDING,
        scheduled_at=timezone.now(),
        payload={"campaign_id": db_setup["campaign"].id, "lead_public_id": "test-lead-public-id"}
    )
    client = Client()
    client.force_login(db_setup["operator"])
    
    url = reverse("dashboard:task_detail", kwargs={"pk": task.pk})
    response = client.get(url)
    assert response.status_code == 200
    assert f"#{task.pk}" in response.content.decode()
    assert "test-lead-public-id" in response.content.decode()


def test_retry_failed_task(db_setup):
    task = Task.objects.create(
        task_type=Task.TaskType.CONNECT,
        status=Task.Status.FAILED,
        scheduled_at=timezone.now(),
        payload={"campaign_id": db_setup["campaign"].id}
    )
    client = Client()
    client.force_login(db_setup["operator"])
    
    url = reverse("dashboard:task_detail", kwargs={"pk": task.pk})
    
    # Retry failed task
    response = client.post(url, {"action": "retry"})
    assert response.status_code == 302
    
    task.refresh_from_db()
    assert task.status == Task.Status.PENDING
    
    # Audit log check
    event = TaskQueueEvent.objects.filter(task=task, action="retry").first()
    assert event is not None
    assert event.previous_status == "failed"
    assert event.new_status == "pending"


def test_retry_guards(db_setup):
    task = Task.objects.create(
        task_type=Task.TaskType.CONNECT,
        status=Task.Status.COMPLETED,
        scheduled_at=timezone.now(),
        payload={"campaign_id": db_setup["campaign"].id}
    )
    client = Client()
    client.force_login(db_setup["operator"])
    url = reverse("dashboard:task_detail", kwargs={"pk": task.pk})
    
    response = client.post(url, {"action": "retry"})
    assert response.status_code == 302
    
    task.refresh_from_db()
    assert task.status == Task.Status.COMPLETED  # Rejected


def test_cancel_pending_task(db_setup):
    task = Task.objects.create(
        task_type=Task.TaskType.CONNECT,
        status=Task.Status.PENDING,
        scheduled_at=timezone.now(),
        payload={"campaign_id": db_setup["campaign"].id}
    )
    client = Client()
    client.force_login(db_setup["operator"])
    url = reverse("dashboard:task_detail", kwargs={"pk": task.pk})
    
    # Without confirm=yes
    response = client.post(url, {"action": "cancel"})
    task.refresh_from_db()
    assert task.status == Task.Status.PENDING
    
    # With confirm=yes
    response = client.post(url, {"action": "cancel", "confirm": "yes"})
    assert response.status_code == 302
    
    task.refresh_from_db()
    assert task.status == Task.Status.CANCELLED
    
    # Preserved, not deleted
    assert Task.objects.filter(pk=task.pk).exists()
    
    event = TaskQueueEvent.objects.filter(task=task, action="cancel").first()
    assert event is not None


def test_reschedule_pending_task(db_setup):
    task = Task.objects.create(
        task_type=Task.TaskType.CONNECT,
        status=Task.Status.PENDING,
        scheduled_at=timezone.now(),
        payload={"campaign_id": db_setup["campaign"].id}
    )
    client = Client()
    client.force_login(db_setup["operator"])
    url = reverse("dashboard:task_detail", kwargs={"pk": task.pk})
    
    new_time_str = "2026-10-10T12:00"
    response = client.post(url, {"action": "reschedule", "scheduled_at": new_time_str})
    assert response.status_code == 302
    
    task.refresh_from_db()
    assert task.scheduled_at.year == 2026
    assert task.scheduled_at.month == 10
    
    event = TaskQueueEvent.objects.filter(task=task, action="reschedule").first()
    assert event is not None


def test_mark_stale_running_task(db_setup):
    task = Task.objects.create(
        task_type=Task.TaskType.CONNECT,
        status=Task.Status.RUNNING,
        scheduled_at=timezone.now(),
        payload={"campaign_id": db_setup["campaign"].id}
    )
    client = Client()
    client.force_login(db_setup["operator"])
    url = reverse("dashboard:task_detail", kwargs={"pk": task.pk})
    
    # Without confirm=yes
    response = client.post(url, {"action": "mark_stale_failed"})
    task.refresh_from_db()
    assert task.status == Task.Status.RUNNING
    
    # With confirm=yes
    response = client.post(url, {"action": "mark_stale_failed", "confirm": "yes"})
    assert response.status_code == 302
    
    task.refresh_from_db()
    assert task.status == Task.Status.FAILED
    
    event = TaskQueueEvent.objects.filter(task=task, action="mark_stale_failed").first()
    assert event is not None


@mock.patch("openoutreach.linkedin.browser.connect_with_note.send_connection_request_with_note")
@mock.patch("openoutreach.emails.tasks.send.handle_email")
def test_manual_actions_no_outreach(mock_email, mock_connect, db_setup):
    task = Task.objects.create(
        task_type=Task.TaskType.CONNECT,
        status=Task.Status.FAILED,
        scheduled_at=timezone.now(),
        payload={"campaign_id": db_setup["campaign"].id}
    )
    client = Client()
    client.force_login(db_setup["operator"])
    url = reverse("dashboard:task_detail", kwargs={"pk": task.pk})
    
    # Retry failed task
    client.post(url, {"action": "retry"})
    
    # Verify no outreach functions were called
    mock_connect.assert_not_called()
    mock_email.assert_not_called()


def test_task_queue_status_cli(db_setup, capsys):
    from openoutreach.core.management.commands.task_queue_status import Command
    
    Task.objects.create(
        task_type=Task.TaskType.CONNECT,
        status=Task.Status.PENDING,
        scheduled_at=timezone.now() - datetime.timedelta(hours=1),
        payload={"campaign_id": db_setup["campaign"].id, "api_key": "my-secret-key"}
    )
    
    cmd = Command()
    # Execute with default options
    cmd.handle(status=None, task_type=None, campaign_id=None, overdue_only=False, limit=10, include_payload=False)
    captured = capsys.readouterr()
    
    assert "TASK QUEUE STATE SUMMARY" in captured.out
    assert "Pending" in captured.out
    assert "my-secret-key" not in captured.out
    
    # Execute with include-payload option
    cmd.handle(status=None, task_type=None, campaign_id=None, overdue_only=False, limit=10, include_payload=True)
    captured_payload = capsys.readouterr()
    assert "Payload" in captured_payload.out
    assert "********" in captured_payload.out
    assert "my-secret-key" not in captured_payload.out
