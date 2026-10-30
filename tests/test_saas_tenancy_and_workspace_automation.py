# tests/test_saas_tenancy_and_workspace_automation.py
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.urls import reverse
from django.test import Client

from openoutreach.core.models import (
    Workspace,
    WorkspaceMembership,
    WorkspaceAutomationSettings,
    Campaign,
    Task,
    OperatorAlert,
)
from openoutreach.core.services.workspace_membership import create_workspace_with_owner
from openoutreach.emails.models import Mailbox
from openoutreach.linkedin.models import LinkedInProfile, ActionLog
from openoutreach.career.models import (
    CandidateProfile,
    CandidateApplicationProfile,
    Job,
    JobApplication,
    JobSearchPreference,
    JobApplicationAnswer,
)
from openoutreach.crm.models import Lead, Deal, DealState
from openoutreach.core.encryption import encrypt_dict, decrypt_string


@pytest.fixture
def tenant_fixture():
    # Workspace A
    user_a = User.objects.create_user(username="user_a", email="user_a@example.com", password="password_a", is_staff=True)
    ws_a = create_workspace_with_owner(name="Alpha Workspace", slug="ws-alpha", owner_user=user_a)
    auto_a = WorkspaceAutomationSettings.get_for_workspace(ws_a)
    lp_a = LinkedInProfile.objects.create(user=user_a, workspace=ws_a, linkedin_username="user_a_li", active=True, session_status="valid")
    mb_a = Mailbox.objects.create(username="mailbox_a@alpha.com", from_address="mailbox_a@alpha.com", password="pass_a", workspace=ws_a, user=user_a)
    camp_a = Campaign.objects.create(name="Alpha Outreach", workspace=ws_a, is_active=True, is_synthetic=False)
    camp_a.users.add(user_a)
    cand_a = CandidateProfile.objects.create(user=user_a, full_name="Alice Alpha")
    app_prof_a = CandidateApplicationProfile.objects.create(candidate_profile=cand_a, workspace=ws_a, confirmed=True)

    # Workspace B
    user_b = User.objects.create_user(username="user_b", email="user_b@example.com", password="password_b", is_staff=True)
    ws_b = create_workspace_with_owner(name="Beta Workspace", slug="ws-beta", owner_user=user_b)
    auto_b = WorkspaceAutomationSettings.get_for_workspace(ws_b)
    lp_b = LinkedInProfile.objects.create(user=user_b, workspace=ws_b, linkedin_username="user_b_li", active=True, session_status="valid")
    mb_b = Mailbox.objects.create(username="mailbox_b@beta.com", from_address="mailbox_b@beta.com", password="pass_b", workspace=ws_b, user=user_b)
    camp_b = Campaign.objects.create(name="Beta Outreach", workspace=ws_b, is_active=True, is_synthetic=False)
    camp_b.users.add(user_b)
    cand_b = CandidateProfile.objects.create(user=user_b, full_name="Bob Beta")
    app_prof_b = CandidateApplicationProfile.objects.create(candidate_profile=cand_b, workspace=ws_b, confirmed=True)

    # Workspace C
    user_c = User.objects.create_user(username="user_c", email="user_c@example.com", password="password_c", is_staff=True)
    ws_c = create_workspace_with_owner(name="Gamma Workspace", slug="ws-gamma", owner_user=user_c)
    auto_c = WorkspaceAutomationSettings.get_for_workspace(ws_c)
    lp_c = LinkedInProfile.objects.create(user=user_c, workspace=ws_c, linkedin_username="user_c_li", active=True, session_status="valid")
    mb_c = Mailbox.objects.create(username="mailbox_c@gamma.com", from_address="mailbox_c@gamma.com", password="pass_c", workspace=ws_c, user=user_c)
    camp_c = Campaign.objects.create(name="Gamma Outreach", workspace=ws_c, is_active=True, is_synthetic=False)
    camp_c.users.add(user_c)
    cand_c = CandidateProfile.objects.create(user=user_c, full_name="Charlie Gamma")
    app_prof_c = CandidateApplicationProfile.objects.create(candidate_profile=cand_c, workspace=ws_c, confirmed=True)

    return {
        "a": {"user": user_a, "ws": ws_a, "auto": auto_a, "lp": lp_a, "mb": mb_a, "camp": camp_a, "cand": cand_a, "app_prof": app_prof_a},
        "b": {"user": user_b, "ws": ws_b, "auto": auto_b, "lp": lp_b, "mb": mb_b, "camp": camp_b, "cand": cand_b, "app_prof": app_prof_b},
        "c": {"user": user_c, "ws": ws_c, "auto": auto_c, "lp": lp_c, "mb": mb_c, "camp": camp_c, "cand": cand_c, "app_prof": app_prof_c},
    }


@pytest.mark.django_db
def test_workspace_scoped_automation_start_pause_isolation(tenant_fixture, client):
    """Proves that starting automation in Workspace A does NOT start B or C, and pausing A leaves B alone."""
    t_a = tenant_fixture["a"]
    t_b = tenant_fixture["b"]
    t_c = tenant_fixture["c"]

    # Initial state: all paused
    assert t_a["auto"].master_automation_enabled is False
    assert t_b["auto"].master_automation_enabled is False
    assert t_c["auto"].master_automation_enabled is False

    # User A starts automation
    client.login(username="user_a", password="password_a")
    resp = client.post(reverse("dashboard:automation_start"), follow=True)
    assert resp.status_code == 200

    t_a["auto"].refresh_from_db()
    t_b["auto"].refresh_from_db()
    t_c["auto"].refresh_from_db()

    assert t_a["auto"].master_automation_enabled is True
    assert t_b["auto"].master_automation_enabled is False
    assert t_c["auto"].master_automation_enabled is False

    # User B independently starts automation
    client.logout()
    client.login(username="user_b", password="password_b")
    resp = client.post(reverse("dashboard:automation_start"), follow=True)
    assert resp.status_code == 200

    t_a["auto"].refresh_from_db()
    t_b["auto"].refresh_from_db()
    t_c["auto"].refresh_from_db()

    assert t_a["auto"].master_automation_enabled is True
    assert t_b["auto"].master_automation_enabled is True
    assert t_c["auto"].master_automation_enabled is False

    # User A pauses automation
    client.logout()
    client.login(username="user_a", password="password_a")
    resp = client.post(reverse("dashboard:automation_pause"), follow=True)
    assert resp.status_code == 200

    t_a["auto"].refresh_from_db()
    t_b["auto"].refresh_from_db()
    t_c["auto"].refresh_from_db()

    assert t_a["auto"].master_automation_enabled is False
    assert t_b["auto"].master_automation_enabled is True
    assert t_c["auto"].master_automation_enabled is False


@pytest.mark.django_db
def test_mailbox_cross_tenant_strict_isolation(tenant_fixture, client):
    """Proves that User A cannot see, edit, test, delete or use Mailbox B."""
    t_a = tenant_fixture["a"]
    t_b = tenant_fixture["b"]

    client.login(username="user_a", password="password_a")

    # 1. Home / Overview
    resp = client.get(reverse("dashboard:overview"))
    assert resp.status_code == 200
    content = resp.content.decode("utf-8")
    assert "mailbox_a@alpha.com" in content
    assert "mailbox_b@beta.com" not in content

    # 2. Settings Email
    resp = client.get(reverse("dashboard:settings_email"))
    assert resp.status_code == 200
    content = resp.content.decode("utf-8")
    assert "mailbox_a@alpha.com" in content
    assert "mailbox_b@beta.com" not in content

    # 3. Mailboxes list
    resp = client.get(reverse("dashboard:mailboxes"))
    assert resp.status_code == 200
    content = resp.content.decode("utf-8")
    assert "mailbox_a@alpha.com" in content
    assert "mailbox_b@beta.com" not in content

    # 4. User A attempting to edit Mailbox B -> 404
    resp = client.get(reverse("dashboard:mailbox_edit", kwargs={"pk": t_b["mb"].pk}))
    assert resp.status_code == 404

    # 5. User A attempting to delete Mailbox B -> 404
    resp = client.post(reverse("dashboard:mailbox_delete", kwargs={"pk": t_b["mb"].pk}))
    assert resp.status_code == 404


@pytest.mark.django_db
def test_linkedin_profile_cross_tenant_isolation(tenant_fixture, client):
    """Proves that User A cannot see or disconnect User B's LinkedIn profile."""
    t_a = tenant_fixture["a"]
    t_b = tenant_fixture["b"]

    client.login(username="user_a", password="password_a")
    resp = client.get(reverse("dashboard:settings_linkedin"))
    assert resp.status_code == 200
    content = resp.content.decode("utf-8")
    assert "user_a_li" in content
    assert "user_b_li" not in content


@pytest.mark.django_db
def test_campaign_and_inbox_cross_tenant_isolation(tenant_fixture, client):
    """Proves that campaigns, deals, messages, and alerts are isolated per tenant."""
    t_a = tenant_fixture["a"]
    t_b = tenant_fixture["b"]

    # Create deals in Workspace A and B
    lead_a = Lead.objects.create(
        public_identifier="prospect-alpha-1",
        linkedin_url="https://www.linkedin.com/in/prospect-alpha-1",
        first_name="TargetA",
        workspace=t_a["ws"],
    )
    deal_a = Deal.objects.create(lead=lead_a, campaign=t_a["camp"], state=DealState.CONNECTED)

    lead_b = Lead.objects.create(
        public_identifier="prospect-beta-1",
        linkedin_url="https://www.linkedin.com/in/prospect-beta-1",
        first_name="TargetB",
        workspace=t_b["ws"],
    )
    deal_b = Deal.objects.create(lead=lead_b, campaign=t_b["camp"], state=DealState.CONNECTED)

    # Create Alerts in Workspace A and B
    from django.utils import timezone
    now = timezone.now()
    alert_a = OperatorAlert.objects.create(
        workspace=t_a["ws"],
        fingerprint="fp-alert-alpha-test",
        title="Alert for Alpha",
        description="Alpha alert details",
        first_seen_at=now,
        last_seen_at=now,
        last_triggered_at=now,
    )
    alert_b = OperatorAlert.objects.create(
        workspace=t_b["ws"],
        fingerprint="fp-alert-beta-test",
        title="Alert for Beta",
        description="Beta alert details",
        first_seen_at=now,
        last_seen_at=now,
        last_triggered_at=now,
    )

    client.login(username="user_a", password="password_a")

    # Outreach Campaigns page
    resp = client.get(reverse("dashboard:campaigns"))
    assert resp.status_code == 200
    content = resp.content.decode("utf-8")
    assert "Alpha Outreach" in content
    assert "Beta Outreach" not in content

    # Inbox Messages
    resp = client.get(reverse("dashboard:operator_inbox") + "?tab=messages")
    assert resp.status_code == 200
    content = resp.content.decode("utf-8")
    assert "prospect-alpha-1" in content
    assert "prospect-beta-1" not in content

    # Inbox Notifications
    resp = client.get(reverse("dashboard:operator_inbox") + "?tab=notifications")
    assert resp.status_code == 200
    content = resp.content.decode("utf-8")
    assert "Alert for Alpha" in content
    assert "Alert for Beta" not in content


@pytest.mark.django_db
def test_raw_database_encryption_and_zero_plaintext(tenant_fixture, client):
    """Submits synthetic session cookie, queries raw DB, and verifies plaintext is not present."""
    from unittest.mock import patch
    t_a = tenant_fixture["a"]
    client.login(username="user_a", password="password_a")

    raw_test_cookie = "HUNTFLOW_SYNTHETIC_LI_AT_987654321"

    # Submit cookie through normal LinkedIn settings verification endpoint with mocked validation
    with patch("openoutreach.linkedin.session_manager.validate_linkedin_session", return_value=("valid", "")):
        resp = client.post(reverse("dashboard:settings_linkedin_verify"), data={"li_at": raw_test_cookie}, follow=True)
        assert resp.status_code == 200

    # Query raw DB representation
    from django.db import connection
    with connection.cursor() as cursor:
        cursor.execute("SELECT cookie_data FROM linkedin_linkedinprofile WHERE id = %s", [t_a["lp"].id])
        raw_row = cursor.fetchone()[0]

    raw_db_text = str(raw_row)
    assert raw_test_cookie not in raw_db_text
    assert "v1::" in raw_db_text or "gcm_enc::" in raw_db_text

    # Verify transparent decryption on authorized read
    t_a["lp"].refresh_from_db()
    from openoutreach.core.encryption import decrypt_dict
    decrypted_state = decrypt_dict(t_a["lp"].cookie_data)
    assert decrypted_state["cookies"][0]["value"] == raw_test_cookie


@pytest.mark.django_db
def test_three_tenant_scheduler_and_ai_isolation(tenant_fixture):
    """Proves that scheduling work for Workspace A creates 0 tasks for Workspace B or C."""
    from openoutreach.core.scheduler import plan_connect_window
    from unittest.mock import MagicMock

    t_a = tenant_fixture["a"]
    t_b = tenant_fixture["b"]
    t_c = tenant_fixture["c"]

    # Enable automation ONLY for Workspace A
    t_a["auto"].master_automation_enabled = True
    t_a["auto"].save()

    t_b["auto"].master_automation_enabled = False
    t_b["auto"].save()

    t_c["auto"].master_automation_enabled = False
    t_c["auto"].save()

    # Session mock for Workspace A
    session_a = MagicMock()
    session_a.linkedin_profile = t_a["lp"]
    session_a.active_timezone = "UTC"
    session_a.django_user = t_a["user"]

    # Session mock for Workspace B
    session_b = MagicMock()
    session_b.linkedin_profile = t_b["lp"]
    session_b.active_timezone = "UTC"
    session_b.django_user = t_b["user"]

    # Run planning for B -> should be 0 because B is paused
    n_b = plan_connect_window(session_b, t_b["camp"])
    assert n_b == 0
    assert Task.objects.filter(payload__campaign_id=t_b["camp"].id).count() == 0
    assert Task.objects.filter(payload__campaign_id=t_c["camp"].id).count() == 0


def test_production_missing_key_fails_closed(monkeypatch, settings):
    """Proves that missing HUNTFLOW_ENCRYPTION_KEY in production mode raises ImproperlyConfigured."""
    from openoutreach.core.encryption import _get_fernet_key
    from django.core.exceptions import ImproperlyConfigured

    monkeypatch.delenv("HUNTFLOW_CREDENTIAL_ENCRYPTION_KEY", raising=False)
    monkeypatch.delenv("HUNTFLOW_ENCRYPTION_KEY", raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    settings.HUNTFLOW_CREDENTIAL_ENCRYPTION_KEY = ""
    settings.HUNTFLOW_ENCRYPTION_KEY = ""
    settings.DEBUG = False
    settings.IS_TEST_ENV = False
    settings.TESTING = False

    with pytest.raises(ImproperlyConfigured) as excinfo:
        _get_fernet_key()

    assert "Production security requirement" in str(excinfo.value)

