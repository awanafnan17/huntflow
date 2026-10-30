# tests/test_launch_run_preview.py
from __future__ import annotations

import json
import io
import pytest
from django.contrib.auth.models import User
from django.urls import reverse
from django.core.management import call_command
from django.utils import timezone
from datetime import timedelta
from openoutreach.core.models import SiteConfig, Campaign, Task, DaemonStatus, LaunchRun, LaunchRunEvent
from openoutreach.linkedin.models import LinkedInProfile
from openoutreach.crm.models import Lead, Deal, DealState
from openoutreach.core.launch_preview import generate_launch_preview


@pytest.fixture
def active_campaign(db):
    camp = Campaign.objects.create(name="Outreach Camp", is_active=True, connection_note_enabled=False)
    return camp


@pytest.fixture
def setup_ready_state(db, active_campaign):
    # Setup LLM
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
    
    # Create a ready recipient so the task resolves
    target_lead = Lead.objects.create(public_identifier="target_recipient", linkedin_url="https://linkedin.com/in/target")
    Deal.objects.create(campaign=active_campaign, lead=target_lead, state=DealState.READY_TO_CONNECT)
    
    # Create an executable pending task (scheduled in past)
    Task.objects.create(
        task_type=Task.TaskType.CONNECT,
        status=Task.Status.PENDING,
        scheduled_at=timezone.now() - timedelta(hours=1),
        payload={"campaign_id": active_campaign.id, "secret_token": "supertest-2-aaaaaaaa123"}
    )

    # Create a future pending task
    Task.objects.create(
        task_type=Task.TaskType.FOLLOW_UP,
        status=Task.Status.PENDING,
        scheduled_at=timezone.now() + timedelta(hours=5),
        payload={"campaign_id": active_campaign.id, "secret_token": "anothertest-2-aaaaaaaa999"}
    )
    
    return user, p


@pytest.mark.django_db
def test_launch_run_models(setup_ready_state):
    user = setup_ready_state[0]
    camp = Campaign.objects.first()
    
    # Create LaunchRun
    run = LaunchRun.objects.create(
        operator=user,
        source="cli",
        mode="live",
        status="started",
        campaign=camp,
        readiness_status="ready",
    )
    assert run.status == "started"
    assert run.operator == user
    assert run.campaign == camp
    
    # Create LaunchRunEvent
    event = LaunchRunEvent.objects.create(
        launch_run=run,
        operator=user,
        event_type="test_event",
        note="Test Note"
    )
    assert event.launch_run == run
    assert event.event_type == "test_event"


@pytest.mark.django_db
def test_generate_launch_preview(setup_ready_state):
    preview = generate_launch_preview()
    assert preview["readiness_status"] == "ready"
    assert preview["executable_now_count"] == 1
    assert preview["future_executable_count"] == 1
    
    # Check payload masking
    task_summary = preview["next_executable_tasks"][0]
    assert task_summary["payload_summary"]["secret_token"] == "********"


@pytest.mark.django_db
def test_daemon_start_blocked(client, setup_ready_state):
    # Make LLM unconfigured so it gets blocked
    cfg = SiteConfig.load()
    cfg.llm_api_key = ""
    cfg.save()
    
    user = setup_ready_state[0]
    client.force_login(user)
    
    url = reverse("dashboard:daemon")
    resp = client.post(url, {"action": "start"})
    assert resp.status_code == 302
    
    # Should record a blocked launch run
    blocked_run = LaunchRun.objects.filter(status="blocked").first()
    assert blocked_run is not None
    assert blocked_run.blocker_count > 0
    assert blocked_run.events.filter(event_type="start_blocked").exists()


@pytest.mark.django_db
def test_daemon_start_accepted(client, setup_ready_state, monkeypatch):
    # Mock subprocess.Popen to not start actual process
    class MockProcess:
        pid = 99999
    
    monkeypatch.setattr("subprocess.Popen", lambda *args, **kwargs: MockProcess())
    
    user = setup_ready_state[0]
    client.force_login(user)
    
    url = reverse("dashboard:daemon")
    resp = client.post(url, {"action": "start"})
    assert resp.status_code == 302
    
    # Should record running launch run with pid
    run = LaunchRun.objects.filter(status="running").first()
    assert run is not None
    assert run.daemon_pid == 99999
    assert run.events.filter(event_type="start_accepted").exists()


@pytest.mark.django_db
def test_daemon_stop_run(client, setup_ready_state):
    user = setup_ready_state[0]
    client.force_login(user)
    
    # Pre-create running launch run
    run = LaunchRun.objects.create(
        operator=user,
        source="dashboard",
        mode="live",
        status="running",
        daemon_pid=12345,
    )
    
    # Setup daemon status in db
    status = DaemonStatus.objects.create(
        pk=1,
        is_running=True,
        pid=12345,
        last_heartbeat=timezone.now()
    )
    
    from unittest.mock import patch
    with patch("openoutreach.core.readiness.is_pid_alive") as mock_alive, \
         patch("openoutreach.core.readiness.safe_stop_daemon") as mock_stop:
        mock_alive.return_value = "running"
        mock_stop.return_value = {"success": True, "result": "stopped", "process_state": "stopped"}
        url = reverse("dashboard:daemon")
        # Post stop
        resp = client.post(url, {"action": "stop"})
        assert resp.status_code == 302
    
    # Run should now be marked stopped with ended_at and event
    run.refresh_from_db()
    assert run.status == "stopped"
    assert run.ended_at is not None
    assert run.stop_reason == "Operator stopped daemon from dashboard"
    assert run.events.filter(event_type="stop_requested").exists()


@pytest.mark.django_db
def test_cli_launch_preview_options(setup_ready_state):
    # Test --json outputs valid JSON
    out = io.StringIO()
    call_command("launch_preview", "--json", stdout=out)
    output = out.getvalue()
    data = json.loads(output)
    assert data["readiness_status"] == "ready"
    assert data["executable_now_count"] == 1
    
    # Verify no secret leaked by default and payloads not printed
    assert "supertest-2-aaaaaaaa123" not in output
    assert "payload_summary" not in output

    # Test --include-payload masks recursively
    out2 = io.StringIO()
    call_command("launch_preview", "--json", "--include-payload", stdout=out2)
    output2 = out2.getvalue()
    data2 = json.loads(output2)
    assert "payload_summary" in output2
    assert "supertest-2-aaaaaaaa123" not in output2
    # Verify masked
    assert data2["executable_now_tasks"][0]["payload_summary"]["secret_token"] == "********"

    # Test --task-type filters preview tasks
    out3 = io.StringIO()
    call_command("launch_preview", "--json", "--task-type", "follow_up", stdout=out3)
    output3 = out3.getvalue()
    data3 = json.loads(output3)
    # The overdue one was "connect", future was "follow_up"
    assert data3["executable_now_count"] == 0
    assert data3["future_executable_count"] == 1

    # Test --include-future prints future tasks separately
    out4 = io.StringIO()
    call_command("launch_preview", "--include-future", stdout=out4)
    output4 = out4.getvalue()
    assert "EXECUTABLE-NOW TASKS:" in output4
    assert "FUTURE PENDING TASKS:" in output4


@pytest.mark.django_db
def test_cli_launch_history(setup_ready_state):
    user = setup_ready_state[0]
    LaunchRun.objects.create(
        operator=user,
        source="dashboard",
        mode="live",
        status="running",
        daemon_pid=77777,
        readiness_status="ready",
        readiness_snapshot={"api_key": "secret_token_val"}
    )
    LaunchRun.objects.create(
        operator=user,
        source="cli",
        mode="live",
        status="stopped",
        daemon_pid=88888,
        readiness_status="ready",
    )

    # Test stdout launch_history
    out1 = io.StringIO()
    call_command("launch_history", "--limit", "2", stdout=out1)
    output1 = out1.getvalue()
    assert "HUNTFLOW LAUNCH RUN HISTORY" in output1
    assert "Run #" in output1
    assert "RUNNING" in output1
    assert "STOPPED" in output1

    # Test --json launch_history
    out2 = io.StringIO()
    call_command("launch_history", "--json", stdout=out2)
    output2 = out2.getvalue()
    data2 = json.loads(output2)
    assert len(data2) >= 2
    # Secret masking in snapshot check
    assert "secret_token_val" not in output2

    # Test --status launch_history
    out3 = io.StringIO()
    call_command("launch_history", "--status", "running", stdout=out3)
    output3 = out3.getvalue()
    assert "RUNNING" in output3
    assert "STOPPED" not in output3


@pytest.mark.django_db
def test_dashboard_views(client, setup_ready_state):
    user = setup_ready_state[0]
    client.force_login(user)
    
    # Create a LaunchRun to list
    run = LaunchRun.objects.create(
        operator=user,
        source="dashboard",
        mode="live",
        status="running",
    )
    
    # 1. Preview view
    resp = client.get(reverse("dashboard:launch_preview"))
    assert resp.status_code == 200
    html = resp.content.decode("utf-8")
    assert "Estimated Launch Outcome" in html
    
    # 2. History view
    resp = client.get(reverse("dashboard:launch_history"))
    assert resp.status_code == 200
    html = resp.content.decode("utf-8")
    assert "Launch Run History" in html
    assert f"#{run.id}" in html
    
    # 3. History Detail view
    resp = client.get(reverse("dashboard:launch_history_detail", kwargs={"pk": run.id}))
    assert resp.status_code == 200
    html = resp.content.decode("utf-8")
    assert f"Launch Run #{run.id} Details" in html


@pytest.mark.django_db
def test_daemon_start_rejected_no_tasks(client, setup_ready_state):
    user = setup_ready_state[0]
    client.force_login(user)
    
    # Delete all tasks to trigger the "No executable pending tasks" blocker
    Task.objects.all().delete()
    
    url = reverse("dashboard:daemon")
    resp = client.post(url, {"action": "start"})
    assert resp.status_code == 302
    
    # Check that error message was set
    from django.contrib.messages import get_messages
    msgs = list(get_messages(resp.wsgi_request))
    assert any("Daemon was not started: No executable outreach task is available." in str(m) for m in msgs)
