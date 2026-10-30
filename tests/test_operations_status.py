# tests/test_operations_status.py
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
from openoutreach.core.models import SiteConfig, Campaign, Task, DaemonStatus, LaunchRun
from openoutreach.linkedin.models import LinkedInProfile
from openoutreach.crm.models import Lead, Deal, DealState
from openoutreach.core.operations import get_operations_snapshot


@pytest.fixture
def active_campaign(db):
    camp = Campaign.objects.create(name="LinkedIn Outreach Camp", is_active=True, connection_note_enabled=False)
    return camp


@pytest.fixture
def setup_ready_state(db, active_campaign):
    # Setup LLM configuration
    cfg = SiteConfig.load()
    cfg.llm_api_key = "test-llm-key"
    cfg.ai_model = "openai:gpt-4o"
    cfg.save()
    
    # Setup active profile
    user = User.objects.create_user(username="test_op", password="password", is_staff=True)
    lead = Lead.objects.create(public_identifier="test_op", linkedin_url="https://linkedin.com/in/test_op")
    p = LinkedInProfile.objects.create(
        user=user,
        linkedin_username="alex@test.com",
        linkedin_password="test-2-aaaaaaaa",
        active=True,
        self_lead=lead
    )
    
    active_campaign.users.add(user)
    
    # Create pending and future tasks
    Task.objects.create(
        task_type=Task.TaskType.CONNECT,
        status=Task.Status.PENDING,
        scheduled_at=timezone.now() - timedelta(hours=1),
        payload={"campaign_id": active_campaign.id, "secret_key_val": "dontleakthisplease123"}
    )
    
    return user, p


@pytest.mark.django_db
def test_get_operations_snapshot_structure(setup_ready_state):
    snapshot = get_operations_snapshot()
    
    # Verify structure is explicit and returned cleanly
    assert "daemon_health" in snapshot
    assert "readiness_summary" in snapshot
    assert "latest_launch_run" in snapshot
    assert "running_tasks" in snapshot
    assert "next_task" in snapshot
    assert "pending_executable_count" in snapshot
    assert "overdue_count" in snapshot
    assert "failed_count" in snapshot
    assert "cancelled_count" in snapshot
    assert "recent_completed_tasks" in snapshot
    assert "recent_failed_tasks" in snapshot
    assert "recent_action_logs" in snapshot
    assert "recent_lead_events" in snapshot
    assert "recent_queue_events" in snapshot
    assert "recent_launch_events" in snapshot
    assert "active_campaigns" in snapshot
    assert "paused_campaigns_with_ignored_pending" in snapshot
    assert "alerts" in snapshot
    assert "next_recommended_action" in snapshot


@pytest.mark.django_db
def test_alert_triggers(setup_ready_state):
    now = timezone.now()
    
    # Setup state for alert triggers:
    # 1. Heartbeat stale alert
    status, _ = DaemonStatus.objects.get_or_create(pk=1)
    status.is_running = True
    status.pid = 99999
    status.last_heartbeat = now - timedelta(minutes=15)
    status.save()
    
    # 2. Stuck task alert
    stuck_task = Task.objects.create(
        task_type=Task.TaskType.CONNECT,
        status=Task.Status.RUNNING,
        scheduled_at=now - timedelta(hours=2),
        started_at=now - timedelta(minutes=45),
        payload={"campaign_id": 1}
    )
    
    # 3. Failed task warning alert
    Task.objects.create(
        task_type=Task.TaskType.CONNECT,
        status=Task.Status.FAILED,
        scheduled_at=now - timedelta(hours=2),
        payload={"campaign_id": 1}
    )
    
    # 4. BetterContact warning while deals in READY_TO_EMAIL
    cfg = SiteConfig.load()
    cfg.bettercontact_api_key = ""
    cfg.save()
    lead = Lead.objects.create(public_identifier="deal_lead", linkedin_url="https://linkedin.com")
    Deal.objects.create(lead=lead, campaign=Campaign.objects.first(), state=DealState.READY_TO_EMAIL)
    
    snapshot = get_operations_snapshot()
    
    # Verify alerts list contains triggers
    alert_titles = [a["title"] for a in snapshot["alerts"]]
    assert "Daemon heartbeat stale" in alert_titles
    assert "Running task stuck" in alert_titles
    assert "Failed tasks present" in alert_titles
    assert "BetterContact unconfigured" in alert_titles


@pytest.mark.django_db
def test_cli_operations_status(setup_ready_state):
    # Test stdout
    out1 = io.StringIO()
    call_command("operations_status", stdout=out1)
    output1 = out1.getvalue()
    assert "HUNTFLOW OPERATIONS MONITOR CONSOLE" in output1
    assert "Next Operator Action:" in output1
    # Verify no secret leaked by default
    assert "dontleakthisplease123" not in output1

    # Test --json
    out2 = io.StringIO()
    call_command("operations_status", "--json", stdout=out2)
    output2 = out2.getvalue()
    data2 = json.loads(output2)
    assert "daemon_health" in data2
    # Verify secrets masked recursively
    assert "dontleakthisplease123" not in output2
    assert data2["next_task"]["payload"]["secret_key_val"] == "********"

    # Test --include-alerts
    out3 = io.StringIO()
    call_command("operations_status", "--include-alerts", stdout=out3)
    output3 = out3.getvalue()
    assert "ACTIVE ALERTS:" in output3


@pytest.mark.django_db
def test_dashboard_operations_view(client, setup_ready_state):
    # Unauthenticated gating check
    resp = client.get(reverse("dashboard:operations"))
    assert resp.status_code == 302
    
    user = setup_ready_state[0]
    client.force_login(user)
    
    # Renders successfully
    resp = client.get(reverse("dashboard:operations"))
    assert resp.status_code == 200
    html = resp.content.decode("utf-8")
    assert "Live Operations Console" in html
    assert "Runtime Health State" in html


@pytest.mark.django_db
def test_no_outreach_execution_in_ops_snapshot(setup_ready_state):
    with patch("subprocess.Popen") as mock_popen, \
         patch("openoutreach.linkedin.tasks.connect.handle_connect") as mock_connect, \
         patch("openoutreach.emails.tasks.send.handle_email") as mock_email:
         
        snapshot = get_operations_snapshot()
        
        # Ensure no subprocess started
        mock_popen.assert_not_called()
        mock_email.assert_not_called()
        
        # Verify no claims or execution transitions occurred
        assert snapshot["running_tasks"] == []
