# tests/test_launch_readiness.py
from __future__ import annotations

import json
import pytest
from django.contrib.auth.models import User
from django.urls import reverse
from django.core.management import call_command
from django.utils import timezone
from openoutreach.core.models import SiteConfig, Campaign, Task, DaemonStatus
from openoutreach.linkedin.models import LinkedInProfile
from openoutreach.crm.models import Lead, Deal, DealState
from openoutreach.core.readiness import check_launch_readiness


@pytest.fixture
def active_campaign(db):
    camp = Campaign.objects.create(name="Active Camp", is_active=True, connection_note_enabled=False)
    return camp


@pytest.fixture
def setup_ready_state(db, active_campaign):
    # Setup LLM
    cfg = SiteConfig.load()
    cfg.llm_api_key = "test-llm-key"
    cfg.ai_model = "openai:gpt-4o"
    cfg.save()
    
    # Setup active profile with a self_lead to avoid warnings
    user = User.objects.create_user(username="test_op", password="password", is_staff=True)
    lead = Lead.objects.create(public_identifier="test_op", linkedin_url="https://linkedin.com/in/test_op")
    p = LinkedInProfile.objects.create(
        user=user,
        linkedin_username="alex@test.com",
        linkedin_password="test-2-aaaaaaaa",
        active=True,
        self_lead=lead
    )
    
    # Map user to campaign
    active_campaign.users.add(user)
    
    # Create a ready recipient so the task resolves
    target_lead = Lead.objects.create(public_identifier="target_recipient", linkedin_url="https://linkedin.com/in/target")
    Deal.objects.create(campaign=active_campaign, lead=target_lead, state=DealState.READY_TO_CONNECT)
    
    # Create an executable pending task
    Task.objects.create(
        task_type=Task.TaskType.CONNECT,
        status=Task.Status.PENDING,
        scheduled_at=timezone.now(),
        payload={"campaign_id": active_campaign.id}
    )
    
    return user, p


@pytest.mark.django_db
def test_readiness_fully_ready(setup_ready_state):
    res = check_launch_readiness()
    assert res["overall_status"] == "ready"
    assert len(res["hard_blockers"]) == 0
    assert res["daemon_process_state"] == "stopped"


@pytest.mark.django_db
def test_readiness_blocked_no_active_profile(setup_ready_state):
    # Disable the profile
    profile = LinkedInProfile.objects.first()
    profile.active = False
    profile.save()
    
    res = check_launch_readiness()
    assert res["overall_status"] == "blocked"
    assert any(b["title"] == "No active LinkedIn account" for b in res["hard_blockers"])


@pytest.mark.django_db
def test_readiness_blocked_missing_credentials(setup_ready_state):
    # Remove credentials from the active profile
    profile = LinkedInProfile.objects.first()
    profile.linkedin_password = ""
    profile.save()
    
    res = check_launch_readiness()
    assert res["overall_status"] == "blocked"
    assert any("credentials" in b["title"].lower() for b in res["hard_blockers"])


@pytest.mark.django_db
def test_readiness_blocked_no_executable_pending_tasks(setup_ready_state):
    # Delete all tasks
    Task.objects.all().delete()
    
    res = check_launch_readiness()
    assert res["overall_status"] == "blocked"
    assert any(b["title"] == "No executable pending tasks" for b in res["hard_blockers"])


@pytest.mark.django_db
def test_readiness_warnings(setup_ready_state):
    # Create a failed task
    Task.objects.create(
        task_type=Task.TaskType.CONNECT,
        status=Task.Status.FAILED,
        scheduled_at=timezone.now(),
        payload={"campaign_id": Campaign.objects.first().id}
    )
    
    res = check_launch_readiness()
    assert res["overall_status"] == "warning"
    assert any("failed tasks" in w["title"].lower() for w in res["warnings"])


@pytest.mark.django_db
def test_campaign_scoped_readiness(setup_ready_state):
    camp2 = Campaign.objects.create(name="Another Campaign", is_active=True)
    res = check_launch_readiness(campaign_id=camp2.id)
    # camp2 has no tasks scheduled, so it must be blocked for camp2 scope
    assert res["campaign_scoped"] is True
    assert res["scoped_campaign_id"] == camp2.id
    assert any("pending tasks" in b["title"].lower() for b in res["hard_blockers"])


@pytest.mark.django_db
def test_no_secret_leaks_in_readiness(setup_ready_state):
    res = check_launch_readiness()
    # Serialize to check no raw secrets are leaked
    res_str = json.dumps(res)
    assert "test-2-aaaaaaaa" not in res_str
    assert "test-llm-key" not in res_str


@pytest.mark.django_db
def test_cli_json_output(setup_ready_state, capsys):
    import io
    from django.core.management import call_command
    out = io.StringIO()
    with pytest.raises(SystemExit) as exc:
        call_command("launch_readiness", "--json", stdout=out)
    assert exc.value.code == 0
    output = out.getvalue()
    data = json.loads(output)
    assert data["overall_status"] == "ready"
    assert "test-2-aaaaaaaa" not in output


@pytest.mark.django_db
def test_cli_exit_codes(setup_ready_state):
    # 1. Ready state returns 0
    with pytest.raises(SystemExit) as exc:
        call_command("launch_readiness")
    assert exc.value.code == 0
    
    # 2. Warning state returns 2 under --strict
    Task.objects.create(
        task_type=Task.TaskType.CONNECT,
        status=Task.Status.FAILED,
        scheduled_at=timezone.now(),
        payload={"campaign_id": Campaign.objects.first().id}
    )
    with pytest.raises(SystemExit) as exc:
        call_command("launch_readiness", "--strict")
    assert exc.value.code == 2
    
    # 3. Blocked state returns 1
    Task.objects.all().delete()
    with pytest.raises(SystemExit) as exc:
        call_command("launch_readiness")
    assert exc.value.code == 1


@pytest.mark.django_db
def test_readiness_dashboard_permissions(client, setup_ready_state):
    url = reverse("dashboard:readiness")
    
    # Anonymous users are redirected
    resp = client.get(url)
    assert resp.status_code == 302
    
    # Logged in staff user gets 200
    user = setup_ready_state[0]
    client.force_login(user)
    resp = client.get(url)
    assert resp.status_code == 200
    
    # Check all sections are rendered in template
    html = resp.content.decode("utf-8")
    assert "System Config" in html
    assert "LinkedIn Accounts" in html
    assert "Campaign Status" in html
    assert "CRM Pipeline" in html
    assert "Task Queue" in html
    assert "Daemon Control" in html
    
    # Check no secret leaks
    assert "test-2-aaaaaaaa" not in html
    assert "test-llm-key" not in html


@pytest.mark.django_db
def test_overview_page_readiness_integration(client, setup_ready_state):
    user = setup_ready_state[0]
    client.force_login(user)
    
    url = reverse("dashboard:overview")
    resp = client.get(url)
    assert resp.status_code == 200
    
    html = resp.content.decode("utf-8")
    assert "HuntFlow" in html


@pytest.mark.django_db
def test_daemon_page_readiness_integration(client, setup_ready_state):
    user = setup_ready_state[0]
    client.force_login(user)
    
    url = reverse("dashboard:daemon")
    resp = client.get(url)
    assert resp.status_code == 200
    
    html = resp.content.decode("utf-8")
    assert "Active Launch Blockers" not in html
    
    # Add a blocker
    Task.objects.all().delete()
    resp = client.get(url)
    html = resp.content.decode("utf-8")
    assert "Active Launch Blockers" in html
    # Start button should be disabled
    assert 'disabled' in html.lower()
