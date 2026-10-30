"""
Tests proving WorkspaceAutomationSettings is strictly authoritative over legacy SiteConfig.
Verifies:
1. SiteConfig channel flags (auto_send_connections, auto_followup, auto_monitor_replies, auto_apply_jobs = True)
   NEVER override WorkspaceAutomationSettings flags set to False.
2. Workspace master_automation_enabled=False independently halts all automation even if SiteConfig master is True.
3. Missing WorkspaceAutomationSettings fails closed on all runtime and scheduling paths.
4. Provider calls remain exactly 0 on all blocked paths.
"""
from unittest.mock import MagicMock, patch
import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from openoutreach.core.models import (
    Campaign,
    OperatorActionApproval,
    SiteConfig,
    Task,
    Workspace,
    WorkspaceAutomationSettings,
)
from openoutreach.career.models import CandidateProfile, Job
from openoutreach.crm.models import Deal, DealState, Lead
from openoutreach.linkedin.models import (
    LinkedInProfile,
    ProviderRelationship,
    ProviderWriteIntent,
)
from openoutreach.core.services.external_action_gates import evaluate_external_action_gates
from openoutreach.linkedin.tasks.connect import handle_connect
from openoutreach.linkedin.tasks.follow_up import handle_follow_up
from openoutreach.career.services.job_application_engine import authorize_job_application
from openoutreach.core.scheduler import (
    plan_connect_window,
    plan_follow_up_window,
    plan_job_window,
    plan_reply_poll_window,
    plan_prospect_discovery_window,
)


@pytest.fixture
def auth_setup(db):
    # Set legacy SiteConfig to all TRUE
    sc = SiteConfig.load()
    sc.master_automation_enabled = True
    sc.auto_send_connections = True
    sc.auto_followup = True
    sc.auto_monitor_replies = True
    sc.auto_apply_jobs = True
    sc.save()

    user = User.objects.create_user(username="test_op", password="password")
    ws = Workspace.objects.create(name="Tenant WS", slug="tenant-ws")
    
    # Workspace has everything explicitly False
    auto_st, _ = WorkspaceAutomationSettings.objects.get_or_create(
        workspace=ws,
        defaults={
            "master_automation_enabled": True,
            "auto_send_connections": False,
            "auto_followup": False,
            "auto_monitor_replies": False,
            "auto_apply_jobs": False,
            "max_daily_connections": 50,
            "max_daily_messages": 50,
            "max_daily_job_applications": 50,
        }
    )

    prof = LinkedInProfile.objects.create(
        user=user,
        workspace=ws,
        linkedin_username="tenant_prof@example.com",
        active=True,
        session_status="valid",
        identity_validation_status="VALIDATED",
        connect_daily_limit=20,
        follow_up_daily_limit=20,
    )

    camp = Campaign.objects.create(
        workspace=ws,
        name="Tenant Campaign",
        linkedin_profile=prof,
        connection_note_enabled=True,
        is_active=True,
    )
    camp.users.add(user)

    lead = Lead.objects.create(
        workspace=ws,
        public_identifier="prospect-auth-1",
        first_name="Alice",
        last_name="Smith",
        linkedin_url="https://www.linkedin.com/in/prospect-auth-1",
    )

    deal = Deal.objects.create(
        lead=lead,
        campaign=camp,
        state=DealState.READY_TO_CONNECT,
    )

    cand = CandidateProfile.objects.create(
        user=user,
        full_name="Alice Candidate",
    )

    job = Job.objects.create(
        title="Software Engineer",
        company="Tech Corp",
        job_url="https://www.linkedin.com/jobs/view/123456",
        external_job_id="123456",
        provider="linkedin",
        provenance="REAL_PROVIDER",
        provider_identity_status="VERIFIED",
        is_active=True,
    )

    return {
        "workspace": ws,
        "auto_settings": auto_st,
        "user": user,
        "profile": prof,
        "campaign": camp,
        "lead": lead,
        "deal": deal,
        "candidate": cand,
        "job": job,
    }


def _make_mock_session(profile, campaign):
    session = MagicMock()
    session.linkedin_profile = profile
    session.campaign = campaign
    session.campaigns = [campaign]
    session.page = MagicMock()
    session.ensure_browser = MagicMock()
    session.active_timezone = "UTC"
    return session


@pytest.mark.django_db
def test_connection_send_blocked_when_workspace_false_despite_siteconfig_true(auth_setup):
    camp = auth_setup["campaign"]
    prof = auth_setup["profile"]
    session = _make_mock_session(prof, camp)

    task = Task.objects.create(
        task_type="connect",
        scheduled_at=timezone.now(),
        payload={"campaign_id": camp.id, "lead_id": auth_setup["lead"].id},
    )

    with patch("openoutreach.linkedin.browser.connect_with_note.send_connection_request_with_note") as mock_send:
        handle_connect(task, session, {})
        assert mock_send.call_count == 0
        assert ProviderWriteIntent.objects.count() == 0

    allowed, reason = evaluate_external_action_gates(
        workspace=auth_setup["workspace"],
        campaign=camp,
        linkedin_profile=prof,
        lead=auth_setup["lead"],
        deal=auth_setup["deal"],
        action_type="connect",
    )
    assert not allowed
    assert reason == "action_capability_disabled"


@pytest.mark.django_db
def test_follow_up_send_blocked_when_workspace_false_despite_siteconfig_true(auth_setup):
    camp = auth_setup["campaign"]
    prof = auth_setup["profile"]
    deal = auth_setup["deal"]
    deal.state = DealState.CONNECTED
    deal.save()

    ProviderRelationship.objects.create(
        workspace=auth_setup["workspace"],
        linkedin_profile=prof,
        lead=deal.lead,
        classification=ProviderRelationship.Classification.CONNECTED,
        evidence_sufficient=True,
    )

    session = _make_mock_session(prof, camp)
    task = Task.objects.create(
        task_type="follow_up",
        scheduled_at=timezone.now(),
        payload={"campaign_id": camp.id},
    )

    with patch("linkedin_cli.actions.message.send_raw_message") as mock_send:
        handle_follow_up(task, session, {})
        assert mock_send.call_count == 0
        assert ProviderWriteIntent.objects.count() == 0

    allowed, reason = evaluate_external_action_gates(
        workspace=auth_setup["workspace"],
        campaign=camp,
        linkedin_profile=prof,
        lead=deal.lead,
        deal=deal,
        action_type="follow_up",
    )
    assert not allowed
    assert reason == "action_capability_disabled"


@pytest.mark.django_db
def test_ai_reply_send_blocked_when_workspace_false_despite_siteconfig_true(auth_setup):
    camp = auth_setup["campaign"]
    prof = auth_setup["profile"]
    deal = auth_setup["deal"]
    deal.state = DealState.CONNECTED
    deal.save()

    ProviderRelationship.objects.create(
        workspace=auth_setup["workspace"],
        linkedin_profile=prof,
        lead=deal.lead,
        classification=ProviderRelationship.Classification.CONNECTED,
        evidence_sufficient=True,
    )

    allowed, reason = evaluate_external_action_gates(
        workspace=auth_setup["workspace"],
        campaign=camp,
        linkedin_profile=prof,
        lead=deal.lead,
        deal=deal,
        action_type="ai_reply",
    )
    assert not allowed
    assert reason == "action_capability_disabled"


@pytest.mark.django_db
def test_job_application_blocked_when_workspace_false_despite_siteconfig_true(auth_setup):
    res = authorize_job_application(
        workspace=auth_setup["workspace"],
        candidate=auth_setup["candidate"],
        job=auth_setup["job"],
    )
    assert not res["authorized"]
    assert res["reason"] == "action_capability_disabled"


@pytest.mark.django_db
def test_missing_workspace_settings_fails_closed(auth_setup):
    # Delete WorkspaceAutomationSettings
    auth_setup["auto_settings"].delete()

    camp = auth_setup["campaign"]
    prof = auth_setup["profile"]
    session = _make_mock_session(prof, camp)

    # 1. Connect handler fails closed without calling provider or creating intent
    task = Task.objects.create(
        task_type="connect",
        scheduled_at=timezone.now(),
        payload={"campaign_id": camp.id, "lead_id": auth_setup["lead"].id},
    )
    with patch("openoutreach.linkedin.browser.connect_with_note.send_connection_request_with_note") as mock_send:
        handle_connect(task, session, {})
        assert mock_send.call_count == 0
        assert ProviderWriteIntent.objects.count() == 0

    # 2. Scheduler planner fails closed (0 slots)
    assert plan_connect_window(session, camp) == 0
    assert plan_follow_up_window(session, camp) == 0
    assert plan_job_window(session, workspace=auth_setup["workspace"], candidate=auth_setup["candidate"]) == 0


@pytest.mark.django_db
def test_scheduler_does_not_plan_when_workspace_disabled_despite_siteconfig_true(auth_setup):
    camp = auth_setup["campaign"]
    prof = auth_setup["profile"]
    session = _make_mock_session(prof, camp)

    # 1. Connect planner returns 0
    connect_slots = plan_connect_window(session, camp)
    assert connect_slots == 0

    # 2. Follow-up planner returns 0
    followup_slots = plan_follow_up_window(session, camp)
    assert followup_slots == 0

    # 3. Discovery planner returns 0
    discovery_slots = plan_prospect_discovery_window(session, camp)
    assert discovery_slots == 0

    # 4. Job planner returns 0
    job_slots = plan_job_window(session, workspace=auth_setup["workspace"], candidate=auth_setup["candidate"])
    assert job_slots == 0


@pytest.mark.django_db
def test_workspace_master_pause_independently_halts_provider_work(auth_setup):
    # Turn individual switches True in workspace, but master False
    auto_st = auth_setup["auto_settings"]
    auto_st.master_automation_enabled = False
    auto_st.auto_send_connections = True
    auto_st.save()

    camp = auth_setup["campaign"]
    prof = auth_setup["profile"]
    session = _make_mock_session(prof, camp)

    task = Task.objects.create(
        task_type="connect",
        scheduled_at=timezone.now(),
        payload={"campaign_id": camp.id, "lead_id": auth_setup["lead"].id},
    )

    with patch("openoutreach.linkedin.browser.connect_with_note.send_connection_request_with_note") as mock_send:
        handle_connect(task, session, {})
        assert mock_send.call_count == 0

    allowed, reason = evaluate_external_action_gates(
        workspace=auth_setup["workspace"],
        campaign=camp,
        linkedin_profile=prof,
        lead=auth_setup["lead"],
        deal=auth_setup["deal"],
        action_type="connect",
    )
    assert not allowed
    assert reason == "workspace_master_automation_paused"
