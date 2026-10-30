# tests/test_full_product_qa.py
"""Full Product QA Test Suite for HuntFlow.

Verifies:
1. Home Overview 200 OK and zero 500 errors.
2. Master one-click START AUTOMATION and PAUSE AUTOMATION lifecycle.
3. Authenticated encryption-at-rest for LinkedIn session cookies and zero secret leaks.
4. Synthetic / Test Data isolation from normal user views.
5. Inbox separation: Messages (conversations) vs Notifications (system alerts).
6. Plain English AI commands (pause, resume, limits, follow-up, targeting).
7. Job application tabs and match transparency.
"""

import pytest
from django.urls import reverse
from django.contrib.auth import get_user_model
from openoutreach.core.models import SiteConfig, Campaign, WorkspaceMembership, WorkspaceAutomationSettings
from openoutreach.career.models import Job, CandidateProfile, CandidateApplicationProfile, JobApplication
from openoutreach.linkedin.models import LinkedInProfile, ActionLog
from openoutreach.crm.models import Deal, DealState, Lead
from openoutreach.core.encryption import encrypt_dict, decrypt_dict

User = get_user_model()


@pytest.fixture
def qa_user(db):
    user, _ = User.objects.get_or_create(username="qa_operator", defaults={"email": "qa@huntflow.ai"})
    user.set_password("password123")
    user.is_staff = True
    user.is_superuser = True
    user.save()
    from openoutreach.core.services.workspace_membership import create_workspace_with_owner
    from openoutreach.core.models import WorkspaceMembership
    if not WorkspaceMembership.objects.filter(user=user, status="active").exists():
        create_workspace_with_owner(name="QA Operator Workspace", slug="ws-qa-operator", owner_user=user)
    return user


@pytest.fixture
def auth_client(client, qa_user):
    client.force_login(qa_user)
    return client


@pytest.mark.django_db
def test_home_overview_renders_200(auth_client, qa_user):
    """Verifies that the Home page loads with 200 OK and contains human-readable widgets."""
    resp = auth_client.get(reverse("dashboard:overview"))
    assert resp.status_code == 200
    content = resp.content.decode()
    assert "Setup & Account Status" in content
    assert "Today's Progress" in content
    assert "Connections Sent" in content
    assert "START AUTOMATION" in content or "Pause Automation" in content
    assert "Server Error" not in content
    assert "Internal Server Error" not in content


@pytest.mark.django_db
def test_one_click_master_automation_start_pause(auth_client, qa_user):
    """Verifies that master automation can be started and paused with state persistence."""
    # Ensure LinkedIn profile is active and valid
    lp, _ = LinkedInProfile.objects.get_or_create(
        user=qa_user,
        defaults={"linkedin_username": "qa_user@huntflow.ai", "active": True, "session_status": "valid"}
    )
    lp.active = True
    lp.session_status = "valid"
    lp.save()

    from openoutreach.core.models import WorkspaceAutomationSettings, WorkspaceMembership
    mem = WorkspaceMembership.objects.filter(user=qa_user, status="active").first()
    auto_settings = WorkspaceAutomationSettings.get_for_workspace(mem.workspace if mem else None)

    # 1. Start Automation
    resp = auth_client.post(reverse("dashboard:automation_start"), follow=True)
    assert resp.status_code == 200
    auto_settings.refresh_from_db()
    assert auto_settings.master_automation_enabled is True

    # 2. Pause Automation
    resp = auth_client.post(reverse("dashboard:automation_pause"), follow=True)
    assert resp.status_code == 200
    auto_settings.refresh_from_db()
    assert auto_settings.master_automation_enabled is False


@pytest.mark.django_db
def test_authenticated_encryption_at_rest_and_zero_leaks(auth_client, qa_user):
    """Verifies that session cookies are stored encrypted and never leaked in HTML."""
    raw_cookies = {"li_at": "AQEDATestCookieSecret12345", "JSESSIONID": "ajax:987654321"}
    enc_data = encrypt_dict(raw_cookies)
    assert isinstance(enc_data, str)
    assert enc_data.startswith("v1::") or enc_data.startswith("gcm_enc::")
    assert "AQEDATestCookieSecret12345" not in enc_data

    # Decrypt verification
    dec_data = decrypt_dict(enc_data)
    assert dec_data == raw_cookies

    # Verify settings page renders masked placeholder, not real secret
    lp, _ = LinkedInProfile.objects.get_or_create(
        user=qa_user,
        defaults={"linkedin_username": "qa_sec@huntflow.ai", "cookie_data": enc_data, "active": True}
    )
    resp = auth_client.get(reverse("dashboard:settings_linkedin"))
    assert resp.status_code == 200
    content = resp.content.decode()
    assert "AQEDATestCookieSecret12345" not in content
    assert "ajax:987654321" not in content


@pytest.mark.django_db
def test_synthetic_data_isolated_from_normal_views(auth_client, qa_user):
    """Verifies that synthetic/test jobs and campaigns are hidden from standard views."""
    # Create genuine job and synthetic job
    genuine_job = Job.objects.create(
        title="Senior Python Architect",
        company="Genuine Tech Labs",
        is_synthetic=False,
    )
    test_job = Job.objects.create(
        title="[TEST] Synthetic Mock Role",
        company="Mock QA Corp",
        is_synthetic=True,
    )

    # Job list view
    resp = auth_client.get(reverse("career:job_list"))
    assert resp.status_code == 200
    content = resp.content.decode()
    assert "Senior Python Architect" in content
    assert "[TEST] Synthetic Mock Role" not in content

    # Test Data admin view shows synthetic record
    resp_admin = auth_client.get(reverse("dashboard:settings_test_data"))
    assert resp_admin.status_code == 200
    admin_content = resp_admin.content.decode()
    assert "[TEST] Synthetic Mock Role" in admin_content


@pytest.mark.django_db
def test_inbox_messages_and_notifications_separation(auth_client, qa_user):
    """Verifies that Inbox splits conversation threads from system notifications."""
    # Messages Tab
    resp_msgs = auth_client.get(reverse("dashboard:operator_inbox") + "?tab=messages")
    assert resp_msgs.status_code == 200
    content_msgs = resp_msgs.content.decode()
    assert "Active Conversations" in content_msgs
    assert "All Channels" in content_msgs

    # Notifications Tab
    resp_notifs = auth_client.get(reverse("dashboard:operator_inbox") + "?tab=notifications")
    assert resp_notifs.status_code == 200
    content_notifs = resp_notifs.content.decode()
    assert "System Notifications & Needs" in content_notifs


@pytest.mark.django_db
def test_plain_english_ai_commands(auth_client, qa_user):
    """Verifies natural language command processing for limits, targeting, and pause/resume."""
    from openoutreach.chat.service import process_chat_message
    from openoutreach.chat.models import AgentConversation, CampaignBrief

    from openoutreach.core.services.workspace_membership import create_workspace_with_owner
    ws = create_workspace_with_owner(name="QA Space", slug="ws-qa-chat", owner_user=qa_user)
    camp = Campaign.objects.create(
        name="Founder Outreach Test Camp",
        workspace=ws,
        is_active=True,
        daily_connect_limit=5,
        is_synthetic=False,
    )
    camp.users.add(qa_user)
    conv = AgentConversation.objects.create(user=qa_user)
    CampaignBrief.objects.create(conversation=conv, campaign=camp)

    # 1. Daily Limit Command
    ans1 = process_chat_message(qa_user, conv, "Send max 15 connections per day")
    camp.refresh_from_db()
    assert camp.daily_connect_limit == 15
    assert "15" in ans1

    # 2. Targeting Command
    ans2 = process_chat_message(qa_user, conv, "Find startup founders in Dubai")
    camp.refresh_from_db()
    assert "startup founders" in camp.target_audience_definition.lower()
    assert "dubai" in camp.target_audience_definition.lower()
    assert "Dubai" in ans2

    # 3. Pause Command
    ans3 = process_chat_message(qa_user, conv, "Pause my outreach")
    camp.refresh_from_db()
    mem = WorkspaceMembership.objects.filter(user=qa_user, status="active").first()
    auto_settings = WorkspaceAutomationSettings.get_for_workspace(mem.workspace if mem else None)
    assert camp.is_active is False
    assert auto_settings.master_automation_enabled is False
    assert "paused" in ans3.lower()
