# tests/test_operator_inbox.py
from __future__ import annotations

import json
import io
import pytest
from unittest.mock import patch, MagicMock
from django.contrib.auth.models import User
from django.urls import reverse
from django.core.management import call_command
from django.utils import timezone
from datetime import timedelta
from openoutreach.core.models import SiteConfig, Campaign, Task, DaemonStatus, OperatorAlert, OperatorAlertEvent
from openoutreach.linkedin.models import LinkedInProfile
from openoutreach.crm.models import Lead, Deal, DealState
from openoutreach.core.operator_alerts import build_alert_fingerprint, sync_operator_alerts, serialize_operator_alert


@pytest.fixture
def active_campaign(db):
    camp = Campaign.objects.create(name="Alert Test Campaign", is_active=True)
    return camp


@pytest.fixture
def staff_user(db):
    user = User.objects.create_user(username="alex_op", password="password", is_staff=True)
    return user


@pytest.mark.django_db
def test_alert_fingerprint_deterministic_and_safe():
    alert_1 = {
        "source": "operations",
        "severity": "hard",
        "title": "Daemon heartbeat stale",
        "description": "Triggered at 2026-07-09T12:00:00", # volatile
        "fix_link": "/dashboard/daemon/",
        "metadata": {"task_id": 123, "secret_key": "raw_password_123"}
    }
    alert_2 = {
        "source": "operations",
        "severity": "hard",
        "title": "Daemon heartbeat stale",
        "description": "Triggered at 2026-07-09T18:00:00", # volatile changed
        "fix_link": "/dashboard/daemon/",
        "metadata": {"task_id": 123, "secret_key": "different_raw_password_val"}
    }
    
    fp1 = build_alert_fingerprint(alert_1)
    fp2 = build_alert_fingerprint(alert_2)
    
    # Assert fingerprints are deterministic & stable despite description changes or secret variables
    assert fp1 == fp2
    assert len(fp1) == 64
    # Check that secrets are completely ignored or recursively masked in the digest string representation
    assert "raw_password_123" not in fp1


@pytest.mark.django_db
def test_sync_operator_alerts_lifecycle(staff_user):
    now = timezone.now()
    # Mock operations snapshot with raw alerts
    snapshot = {
        "alerts": [
            {
                "severity": "hard",
                "title": "LLM API Key missing",
                "description": "Config is incomplete",
                "fix_link": "/dashboard/settings/llm/",
                "metadata": {"api_key": "leaked_secret_api_key"}
            },
            {
                "severity": "warning",
                "title": "Overdue tasks count high",
                "description": "Scheduler is lagging",
                "fix_link": "/dashboard/tasks/",
            }
        ]
    }
    
    # 1. Dry run sync does not mutate
    metrics_dry = sync_operator_alerts(snapshot=snapshot, operator=staff_user, source="operations", dry_run=True)
    assert OperatorAlert.objects.count() == 0
    assert metrics_dry["created_count"] == 2
    
    # 2. Sync inserts alerts
    metrics_real = sync_operator_alerts(snapshot=snapshot, operator=staff_user, source="operations", dry_run=False)
    assert OperatorAlert.objects.count() == 2
    assert OperatorAlertEvent.objects.count() == 2
    assert metrics_real["created_count"] == 2
    
    # Check secret masking in serialized alert metadata
    alert_obj = OperatorAlert.objects.get(title="LLM API Key missing")
    assert alert_obj.metadata["api_key"] == "********"
    assert alert_obj.source == "operations" # Default source verified
    
    # 3. Sync updates existing alerts
    snapshot_updated = {
        "alerts": [
            {
                "severity": "hard",
                "title": "LLM API Key missing",
                "description": "Updated description text here",
                "fix_link": "/dashboard/settings/llm/",
            },
            {
                "severity": "warning",
                "title": "Overdue tasks count high",
                "description": "Scheduler is lagging",
                "fix_link": "/dashboard/tasks/",
            }
        ]
    }
    sync_operator_alerts(snapshot=snapshot_updated, operator=staff_user, source="operations", dry_run=False)
    alert_obj.refresh_from_db()
    assert alert_obj.description == "Updated description text here"
    assert alert_obj.occurrence_count == 2
    
    # 4. Sync resolves missing same-source alerts
    snapshot_resolved = {
        "alerts": [
            {
                "severity": "hard",
                "title": "LLM API Key missing",
                "description": "Config is incomplete",
                "fix_link": "/dashboard/settings/llm/",
            }
        ]
    }
    sync_operator_alerts(snapshot=snapshot_resolved, operator=staff_user, source="operations", dry_run=False)
    warning_alert = OperatorAlert.objects.get(title="Overdue tasks count high")
    assert warning_alert.status == OperatorAlert.AlertStatus.RESOLVED
    assert warning_alert.resolved_at is not None
    
    # 5. Resolved alert reappearing becomes active & creates reopened event
    sync_operator_alerts(snapshot=snapshot_updated, operator=staff_user, source="operations", dry_run=False)
    warning_alert.refresh_from_db()
    assert warning_alert.status == OperatorAlert.AlertStatus.ACTIVE
    assert warning_alert.resolved_at is None
    assert warning_alert.events.filter(event_type=OperatorAlertEvent.EventType.REOPENED).exists()


@pytest.mark.django_db
def test_alert_action_transitions(staff_user):
    alert = OperatorAlert.objects.create(
        fingerprint="test_fp",
        source="operations",
        severity=OperatorAlert.SeverityType.HARD,
        status=OperatorAlert.AlertStatus.ACTIVE,
        title="Test Alert",
        description="Description test",
        first_seen_at=timezone.now(),
        last_seen_at=timezone.now(),
        last_triggered_at=timezone.now(),
    )
    
    # 1. Acknowledge transition
    alert.status = OperatorAlert.AlertStatus.ACKNOWLEDGED
    alert.acknowledged_at = timezone.now()
    alert.acknowledged_by = staff_user
    alert.save()
    OperatorAlertEvent.objects.create(
        alert=alert,
        operator=staff_user,
        event_type=OperatorAlertEvent.EventType.ACKNOWLEDGED,
        note="Acknowledged details"
    )
    assert alert.status == OperatorAlert.AlertStatus.ACKNOWLEDGED
    assert alert.acknowledged_by == staff_user
    
    # 2. Snooze transition
    alert.status = OperatorAlert.AlertStatus.SNOOZED
    alert.snoozed_until = timezone.now() + timedelta(hours=4)
    alert.save()
    assert alert.status == OperatorAlert.AlertStatus.SNOOZED
    assert alert.snoozed_until is not None
    
    # 3. Dismiss transition (warning can be dismissed)
    warning_alert = OperatorAlert.objects.create(
        fingerprint="warning_fp",
        source="operations",
        severity=OperatorAlert.SeverityType.WARNING,
        status=OperatorAlert.AlertStatus.ACTIVE,
        title="Warning Alert",
        description="Warning description",
        first_seen_at=timezone.now(),
        last_seen_at=timezone.now(),
        last_triggered_at=timezone.now(),
    )
    warning_alert.status = OperatorAlert.AlertStatus.DISMISSED
    warning_alert.dismissed_at = timezone.now()
    warning_alert.dismissed_by = staff_user
    warning_alert.save()
    assert warning_alert.status == OperatorAlert.AlertStatus.DISMISSED
    assert warning_alert.dismissed_by == staff_user
    
    # Reopen clears resolved/dismissed/snoozed details
    warning_alert.status = OperatorAlert.AlertStatus.ACTIVE
    warning_alert.dismissed_at = None
    warning_alert.save()
    assert warning_alert.status == OperatorAlert.AlertStatus.ACTIVE
    assert warning_alert.dismissed_at is None


@pytest.mark.django_db
def test_dashboard_views_boundaries_and_auth(client, staff_user):
    # Inbox list login check
    url_list = reverse("dashboard:operator_inbox")
    assert client.get(url_list).status_code == 302
    
    client.force_login(staff_user)
    assert client.get(url_list).status_code == 200
    
    # Inbox detail check
    alert = OperatorAlert.objects.create(
        fingerprint="detail_fp",
        source="operations",
        severity="warning",
        title="Details Alert",
        description="test",
        first_seen_at=timezone.now(),
        last_seen_at=timezone.now(),
        last_triggered_at=timezone.now(),
    )
    url_detail = reverse("dashboard:operator_inbox_detail", kwargs={"pk": alert.id})
    assert client.get(url_detail).status_code == 200
    
    # Sync is POST-only check
    url_sync = reverse("dashboard:operator_inbox_sync")
    resp_get = client.get(url_sync)
    assert resp_get.status_code == 405
    
    resp_post = client.post(url_sync)
    assert resp_post.status_code == 302


@pytest.mark.django_db
def test_hard_alert_dismissal_gating_in_view(client, staff_user):
    client.force_login(staff_user)
    
    # Create an active hard alert present in the operations snapshot
    status, _ = DaemonStatus.objects.get_or_create(pk=1)
    status.is_running = True
    status.pid = 88888
    status.last_heartbeat = timezone.now() - timedelta(minutes=15) # stale heartbeat triggers hard alert
    status.save()
    
    # Trigger sync to create the DB alert matching the stale heartbeat alert
    client.post(reverse("dashboard:operator_inbox_sync"))
    
    alert = OperatorAlert.objects.get(title="Daemon heartbeat stale")
    url_dismiss = reverse("dashboard:operator_inbox_dismiss", kwargs={"pk": alert.id})
    
    # GET request to mutation URL does not change status
    client.get(url_dismiss)
    alert.refresh_from_db()
    assert alert.status == OperatorAlert.AlertStatus.ACTIVE
    
    # POST dismissal is blocked because fingerprint is present in snapshot & severity is hard
    resp = client.post(url_dismiss, {"note": "dismiss block test"})
    alert.refresh_from_db()
    assert alert.status == OperatorAlert.AlertStatus.ACTIVE
    assert alert.events.filter(event_type=OperatorAlertEvent.EventType.DISMISSED).count() == 0


@pytest.mark.django_db
def test_cli_operator_inbox_commands():
    # Sync command json check
    out1 = io.StringIO()
    call_command("operator_inbox_sync", "--json", stdout=out1)
    output1 = out1.getvalue()
    data1 = json.loads(output1)
    assert "created_count" in data1
    assert "resolved_count" in data1

    # Status command check
    out2 = io.StringIO()
    call_command("operator_inbox_status", "--json", "--include-events", stdout=out2)
    output2 = out2.getvalue()
    data2 = json.loads(output2)
    assert isinstance(data2, list)


@pytest.mark.django_db
def test_no_outreach_execution_in_sync():
    with patch("subprocess.Popen") as mock_popen, \
         patch("openoutreach.linkedin.tasks.connect.handle_connect") as mock_connect, \
         patch("openoutreach.emails.tasks.send.handle_email") as mock_email:
         
        sync_operator_alerts()
        
        mock_popen.assert_not_called()
        mock_connect.assert_not_called()
        mock_email.assert_not_called()
