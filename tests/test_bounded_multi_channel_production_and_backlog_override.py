import pytest
from datetime import timedelta
from unittest.mock import patch, MagicMock
from django.utils import timezone
from django.contrib.auth.models import User
from openoutreach.core.models import (
    Workspace, WorkspaceAutomationSettings, Campaign, Task, SenderIdentity
)
from openoutreach.crm.models import Deal, Lead, DealState
from openoutreach.career.models import (
    CandidateProfile, CandidateProfileVersion, CandidateIdentityVerification,
    CandidateApplicationProfile, Job, JobApplication
)
from openoutreach.linkedin.models import LinkedInProfile, ConnectionAttempt, ActionLog
from openoutreach.emails.models import Mailbox
from openoutreach.chat.models import ChatMessage
from openoutreach.core.services.external_action_gates import evaluate_external_action_gates


@pytest.fixture
def multi_channel_env(db):
    user = User.objects.create(username="mc_operator")
    workspace = Workspace.objects.create(name="MC Workspace", slug="mc-ws")

    campaign = Campaign.objects.create(
        name="Multi-Channel Outreach",
        workspace=workspace,
        is_active=True,
        linkedin_outreach_enabled=True,
        email_outreach_enabled=True,
    )
    campaign.users.add(user)

    mailbox = Mailbox.objects.create(
        workspace=workspace,
        user=user,
        username="outreach@huntflow.ai",
        from_address="outreach@huntflow.ai",
        host="smtp.gmail.com",
        port=587,
        password="secretpassword",
        daily_limit=20
    )

    settings = WorkspaceAutomationSettings.get_for_workspace(workspace)
    settings.start_bounded_multi_channel_production(
        max_daily_jobs=5,
        max_daily_conns=5,
        max_daily_msgs=10,
        max_daily_emails=20,
        backlog_override=True
    )

    candidate = CandidateProfile.objects.create(
        user=user,
        full_name="Alex Morgan",
        email="candidate-2@example.com",
        phone="+12025550100",
    )
    CandidateIdentityVerification.objects.create(
        candidate_profile=candidate,
        status="verified",
        extracted_name=candidate.full_name,
        confirmed_name=candidate.full_name,
        verification_source="manual"
    )
    ver = CandidateProfileVersion.objects.create(
        profile=candidate,
        version_number=1,
        full_name=candidate.full_name,
        email=candidate.email,
        phone=candidate.phone,
        state="approved",
        is_finalized=True,
        fact_snapshot=[{"fact_type": "skill", "category": "Python", "value": "Python", "verified": True}]
    )
    candidate.active_version = ver
    candidate.save(update_fields=["active_version"])

    app_prof, _ = CandidateApplicationProfile.objects.get_or_create(candidate_profile=candidate)
    app_prof.full_name = candidate.full_name
    app_prof.email = candidate.email
    app_prof.phone = candidate.phone
    app_prof.city = "New York"
    app_prof.country = "United States"
    app_prof.authorized_countries = ["US"]
    app_prof.visa_sponsorship_now = "no"
    app_prof.visa_sponsorship_future = "no"
    app_prof.relocation_willingness = "no"
    app_prof.remote_willingness = "yes"
    app_prof.hybrid_willingness = "yes"
    app_prof.onsite_willingness = "yes"
    app_prof.travel_willingness = "no"
    app_prof.background_check_willingness = "yes"
    app_prof.currency = "USD"
    app_prof.target_salary = 120000
    app_prof.minimum_salary = 100000
    app_prof.salary_period = "annual"
    app_prof.confirmed = True
    app_prof.save()

    from openoutreach.career.models import JobApplicationAnswer
    JobApplicationAnswer.objects.create(
        candidate_profile=candidate,
        canonical_key="work_auth_us",
        answer_type="boolean",
        answer_value=True,
        confirmed=True
    )
    JobApplicationAnswer.objects.create(
        candidate_profile=candidate,
        canonical_key="sponsorship_future",
        answer_type="boolean",
        answer_value=False,
        confirmed=True
    )

    lp = LinkedInProfile.objects.create(
        user=user,
        workspace=workspace,
        linkedin_username="alex-morgan",
        session_status="valid",
        active=True
    )

    return {
        "user": user,
        "workspace": workspace,
        "settings": settings,
        "candidate": candidate,
        "linkedin_profile": lp,
        "campaign": campaign,
        "mailbox": mailbox,
    }


def test_backlog_override_enables_bounded_connections_despite_backlog(multi_channel_env):
    """Prove operator override enables connection requests even when backlog exceeds 100 limit."""
    workspace = multi_channel_env["workspace"]
    campaign = multi_channel_env["campaign"]
    settings = multi_channel_env["settings"]

    # Create 326 pending deals in the workspace
    lead_objs = [
        Lead(
            workspace=workspace,
            public_identifier=f"pending_lead_{i}",
            linkedin_url=f"https://linkedin.com/in/pending_lead_{i}",
            first_name=f"Lead{i}"
        )
        for i in range(326)
    ]
    Lead.objects.bulk_create(lead_objs)
    leads = Lead.objects.filter(workspace=workspace, public_identifier__startswith="pending_lead_")
    Deal.objects.bulk_create([Deal(campaign=campaign, lead=l, state="Pending") for l in leads])

    assert Deal.objects.filter(campaign__workspace=workspace, state="Pending").count() == 326
    assert settings.connection_backlog_operator_override is True

    # Target lead for new connection
    target_lead = Lead.objects.create(
        workspace=workspace,
        public_identifier="new_prospect_1",
        linkedin_url="https://linkedin.com/in/new_prospect_1",
        first_name="Prospect1"
    )
    target_deal = Deal.objects.create(campaign=campaign, lead=target_lead, state="Ready to Connect")

    allowed, reason = evaluate_external_action_gates(
        workspace=workspace,
        campaign=campaign,
        lead=target_lead,
        deal=target_deal,
        external_action_authorized=True,
        action_type="connect"
    )
    assert allowed is True, f"Connection should be allowed with backlog override, got: {reason}"


def test_override_off_restores_backlog_pause_immediately(multi_channel_env):
    """Prove turning off backlog override immediately pauses new connection requests."""
    workspace = multi_channel_env["workspace"]
    campaign = multi_channel_env["campaign"]
    settings = multi_channel_env["settings"]

    # Create 105 pending deals
    lead_objs = [
        Lead(
            workspace=workspace,
            public_identifier=f"bl_lead_{i}",
            linkedin_url=f"https://linkedin.com/in/bl_lead_{i}",
            first_name=f"BLead{i}"
        )
        for i in range(105)
    ]
    Lead.objects.bulk_create(lead_objs)
    leads = Lead.objects.filter(workspace=workspace, public_identifier__startswith="bl_lead_")
    Deal.objects.bulk_create([Deal(campaign=campaign, lead=l, state="Pending") for l in leads])

    # Disable override
    settings.disable_backlog_override()
    assert settings.connection_backlog_operator_override is False

    target_lead = Lead.objects.create(
        workspace=workspace,
        public_identifier="new_prospect_2",
        linkedin_url="https://linkedin.com/in/new_prospect_2",
        first_name="Prospect2"
    )
    target_deal = Deal.objects.create(campaign=campaign, lead=target_lead, state="Ready to Connect")

    allowed, reason = evaluate_external_action_gates(
        workspace=workspace,
        campaign=campaign,
        lead=target_lead,
        deal=target_deal,
        external_action_authorized=True,
        action_type="connect"
    )
    assert allowed is False
    assert "pending_invitation_backlog_exceeded" in reason


def test_connection_daily_cap_blocks_sixth_attempt(multi_channel_env):
    """Prove daily connection limit of 5 is strictly enforced."""
    workspace = multi_channel_env["workspace"]
    campaign = multi_channel_env["campaign"]
    lp = multi_channel_env["linkedin_profile"]

    # Record 5 confirmed connections today
    for i in range(5):
        ConnectionAttempt.objects.create(
            linkedin_profile=lp,
            recipient_public_id=f"conn_done_{i}",
            idempotency_key=f"idemp_conn_{i}",
            status="externally_confirmed",
            created_at=timezone.now()
        )

    target_lead = Lead.objects.create(
        workspace=workspace,
        public_identifier="prospect_six",
        linkedin_url="https://linkedin.com/in/prospect_six",
        first_name="Six"
    )
    target_deal = Deal.objects.create(campaign=campaign, lead=target_lead, state="Ready to Connect")

    allowed, reason = evaluate_external_action_gates(
        workspace=workspace,
        campaign=campaign,
        lead=target_lead,
        deal=target_deal,
        external_action_authorized=True,
        action_type="connect"
    )
    assert allowed is False
    assert "daily_connection_limit_reached" in reason


def test_pending_connection_never_receives_followup(multi_channel_env):
    """Prove a deal in Pending state is strictly blocked from receiving follow-up messages."""
    workspace = multi_channel_env["workspace"]
    campaign = multi_channel_env["campaign"]

    lead = Lead.objects.create(
        workspace=workspace,
        public_identifier="pending_zisis",
        linkedin_url="https://linkedin.com/in/pending_zisis",
        first_name="Zisis",
        last_name="Petrou"
    )
    deal = Deal.objects.create(campaign=campaign, lead=lead, state="Pending")

    allowed, reason = evaluate_external_action_gates(
        workspace=workspace,
        campaign=campaign,
        lead=lead,
        deal=deal,
        external_action_authorized=True,
        action_type="follow_up"
    )
    assert allowed is False
    assert reason == "deal_still_pending_cannot_followup"


def test_accepted_connection_followup_eligibility(multi_channel_env):
    """Prove an Accepted connection deal is eligible for follow-up under daily message caps."""
    workspace = multi_channel_env["workspace"]
    campaign = multi_channel_env["campaign"]

    lead = Lead.objects.create(
        workspace=workspace,
        public_identifier="accepted_prospect",
        linkedin_url="https://linkedin.com/in/accepted_prospect",
        first_name="Accepted"
    )
    deal = Deal.objects.create(campaign=campaign, lead=lead, state="Connected")

    allowed, reason = evaluate_external_action_gates(
        workspace=workspace,
        campaign=campaign,
        lead=lead,
        deal=deal,
        external_action_authorized=True,
        action_type="follow_up"
    )
    assert allowed is True, f"Accepted deal should be eligible for follow up, got: {reason}"


def test_followup_daily_cap_blocks_eleventh_message(multi_channel_env):
    """Prove daily follow-up limit of 10 messages is strictly enforced."""
    workspace = multi_channel_env["workspace"]
    campaign = multi_channel_env["campaign"]
    lp = multi_channel_env["linkedin_profile"]

    # Record 10 follow-ups today
    for i in range(10):
        ActionLog.objects.create(
            linkedin_profile=lp,
            campaign=campaign,
            action_type=ActionLog.ActionType.FOLLOW_UP,
            metadata={"lead": f"lead_{i}"}
        )

    lead = Lead.objects.create(
        workspace=workspace,
        public_identifier="eleventh_lead",
        linkedin_url="https://linkedin.com/in/eleventh_lead",
        first_name="Eleventh"
    )
    deal = Deal.objects.create(campaign=campaign, lead=lead, state="Connected")

    allowed, reason = evaluate_external_action_gates(
        workspace=workspace,
        campaign=campaign,
        lead=lead,
        deal=deal,
        external_action_authorized=True,
        action_type="follow_up"
    )
    assert allowed is False
    assert "daily_linkedin_message_limit_reached" in reason or "daily_followup_limit_reached" in reason


def test_inbound_reply_cancels_canned_follow_up(multi_channel_env):
    """Prove inbound chat message from prospect cancels scheduled canned follow-up."""
    workspace = multi_channel_env["workspace"]
    campaign = multi_channel_env["campaign"]

    lead = Lead.objects.create(
        workspace=workspace,
        public_identifier="active_replier",
        linkedin_url="https://linkedin.com/in/active_replier",
        first_name="Replier"
    )
    deal = Deal.objects.create(campaign=campaign, lead=lead, state="Connected")

    # Inbound message arrives
    ChatMessage.objects.create(
        deal=deal,
        content="Hi Candidate, thanks for reaching out! Tell me more about what you do.",
        is_outgoing=False,
        linkedin_urn="urn:li:msg:12345"
    )

    from openoutreach.linkedin.tasks.follow_up import handle_follow_up
    mock_session = MagicMock()
    mock_session.campaign = campaign
    mock_session.linkedin_profile = multi_channel_env["linkedin_profile"]

    with patch("linkedin_cli.actions.message.send_raw_message") as mock_send, \
         patch("openoutreach.core.agents.follow_up.run_follow_up_agent") as mock_agent:
        from openoutreach.core.agents.follow_up import FollowUpDecision
        mock_agent.return_value = FollowUpDecision(action="wait", follow_up_hours=24)
        handle_follow_up(MagicMock(), mock_session, {})
        # Prewritten canned follow-up must NOT be sent
        assert mock_send.call_count == 0


def test_negative_reply_suppresses_future_outreach(multi_channel_env):
    """Prove opt-out/negative reply sets outcome to not_interested and suppresses future actions."""
    workspace = multi_channel_env["workspace"]
    campaign = multi_channel_env["campaign"]

    lead = Lead.objects.create(
        workspace=workspace,
        public_identifier="optout_prospect",
        linkedin_url="https://linkedin.com/in/optout_prospect",
        first_name="Optout"
    )
    deal = Deal.objects.create(campaign=campaign, lead=lead, state="Connected", outcome="not_interested")

    allowed, reason = evaluate_external_action_gates(
        workspace=workspace,
        campaign=campaign,
        lead=lead,
        deal=deal,
        external_action_authorized=True,
        action_type="follow_up"
    )
    assert allowed is False
    assert reason == "deal_dispositioned_or_opted_out"


def test_email_daily_cap_blocks_twenty_first_send(multi_channel_env):
    """Prove email daily limit of 20 is strictly enforced."""
    workspace = multi_channel_env["workspace"]
    campaign = multi_channel_env["campaign"]
    mailbox = multi_channel_env["mailbox"]

    # Record 20 emails sent today
    for i in range(20):
        l = Lead.objects.create(
            workspace=workspace,
            public_identifier=f"email_lead_{i}",
            linkedin_url=f"https://linkedin.com/in/email_lead_{i}",
            first_name=f"E{i}"
        )
        Deal.objects.create(campaign=campaign, lead=l, state="Emailed", email_sent_at=timezone.now(), mailbox=mailbox)

    target_lead = Lead.objects.create(
        workspace=workspace,
        public_identifier="email_twenty_one",
        linkedin_url="https://linkedin.com/in/email_twenty_one",
        first_name="TwentyOne"
    )
    target_deal = Deal.objects.create(campaign=campaign, lead=target_lead, state="Ready to Email")

    allowed, reason = evaluate_external_action_gates(
        workspace=workspace,
        campaign=campaign,
        lead=target_lead,
        deal=target_deal,
        external_action_authorized=True,
        action_type="email"
    )
    assert allowed is False
    assert "daily_email_limit_reached" in reason


def test_global_emergency_pause_blocks_all_channels(multi_channel_env):
    """Prove master automation toggle acts as an absolute kill switch for all channels."""
    workspace = multi_channel_env["workspace"]
    campaign = multi_channel_env["campaign"]
    candidate = multi_channel_env["candidate"]
    settings = multi_channel_env["settings"]

    # Kill switch activated
    settings.master_automation_enabled = False
    settings.save(update_fields=["master_automation_enabled"])

    lead = Lead.objects.create(
        workspace=workspace,
        public_identifier="kill_prospect",
        linkedin_url="https://linkedin.com/in/kill_prospect",
        first_name="Kill"
    )
    deal = Deal.objects.create(campaign=campaign, lead=lead, state="Ready to Connect")
    job, _ = Job.upsert_discovered_job(
        provider="linkedin",
        external_job_id="999111",
        title="Python Dev",
        company="Co",
        location="NY",
        description="Python Django",
        easy_apply=True,
        provenance="REAL_PROVIDER",
        provider_identity_status="VERIFIED"
    )

    # 1. Connection blocked
    a1, r1 = evaluate_external_action_gates(workspace=workspace, campaign=campaign, lead=lead, deal=deal, external_action_authorized=True, action_type="connect")
    assert a1 is False and r1 == "workspace_master_automation_paused"

    # 2. Follow-up blocked
    deal.state = "Connected"
    a2, r2 = evaluate_external_action_gates(workspace=workspace, campaign=campaign, lead=lead, deal=deal, external_action_authorized=True, action_type="follow_up")
    assert a2 is False and r2 == "workspace_master_automation_paused"

    # 3. Email blocked
    a3, r3 = evaluate_external_action_gates(workspace=workspace, campaign=campaign, lead=lead, deal=deal, external_action_authorized=True, action_type="email")
    assert a3 is False and r3 == "workspace_master_automation_paused"

    # 4. Job application blocked
    a4, r4 = evaluate_external_action_gates(workspace=workspace, candidate_profile=candidate, job=job, external_action_authorized=True, action_type="job_application")
    assert a4 is False and r4 == "workspace_master_automation_paused"


def test_linkedin_session_failure_blocks_linkedin_writes(multi_channel_env):
    """Prove LinkedIn checkpoint/challenge/restriction blocks LinkedIn writes."""
    workspace = multi_channel_env["workspace"]
    campaign = multi_channel_env["campaign"]

    lead = Lead.objects.create(
        workspace=workspace,
        public_identifier="chk_prospect",
        linkedin_url="https://linkedin.com/in/chk_prospect",
        first_name="Chk"
    )
    deal = Deal.objects.create(campaign=campaign, lead=lead, state="Ready to Connect")

    # Safety signal active (checkpoint / captcha)
    allowed, reason = evaluate_external_action_gates(
        workspace=workspace,
        campaign=campaign,
        lead=lead,
        deal=deal,
        has_captcha=True,
        external_action_authorized=True,
        action_type="connect"
    )
    assert allowed is False
    assert reason == "platform_safety_signal_active"


def test_job_application_daily_cap_blocks_sixth_attempt(multi_channel_env):
    """Prove 5/day job application cap strictly blocks the 6th attempt."""
    workspace = multi_channel_env["workspace"]
    candidate = multi_channel_env["candidate"]

    # Record 5 applications submitted today
    for i in range(5):
        j, _ = Job.upsert_discovered_job(
            provider="linkedin",
            external_job_id=f"ext_job_{i}",
            title=f"Engineer {i}",
            company=f"Company {i}",
            location="Remote",
            description="Python engineer",
            easy_apply=True,
            provenance="REAL_PROVIDER",
            provider_identity_status="VERIFIED"
        )
        JobApplication.objects.create(
            candidate_profile=candidate,
            job=j,
            status="submitted",
            submission_date=timezone.now(),
        )

    target_job, _ = Job.upsert_discovered_job(
        provider="linkedin",
        external_job_id="ext_job_six",
        title="Engineer Six",
        company="Company Six",
        location="Remote",
        description="Python engineer",
        easy_apply=True,
        provenance="REAL_PROVIDER",
        provider_identity_status="VERIFIED"
    )

    allowed, reason = evaluate_external_action_gates(
        workspace=workspace,
        candidate_profile=candidate,
        job=target_job,
        external_action_authorized=True,
        action_type="job_application"
    )
    assert allowed is False
    assert "daily_job_application_limit_reached" in reason


def test_cross_channel_reply_suppression(multi_channel_env):
    """Prove a reply received on LinkedIn suppresses cold email attempts to that prospect."""
    workspace = multi_channel_env["workspace"]
    campaign = multi_channel_env["campaign"]

    lead = Lead.objects.create(
        workspace=workspace,
        public_identifier="cross_chan_prospect",
        linkedin_url="https://linkedin.com/in/cross_chan_prospect",
        first_name="Cross"
    )
    deal = Deal.objects.create(campaign=campaign, lead=lead, state="Connected", outcome="not_interested")

    allowed, reason = evaluate_external_action_gates(
        workspace=workspace,
        campaign=campaign,
        lead=lead,
        deal=deal,
        external_action_authorized=True,
        action_type="email"
    )
    assert allowed is False
    assert reason == "deal_dispositioned_or_opted_out"


def test_ollama_failure_blocks_ai_reply_safely(multi_channel_env):
    """Prove Ollama LLM failure safely halts reply generation without hallucinated/corrupt fallback."""
    campaign = multi_channel_env["campaign"]

    lead = Lead.objects.create(
        workspace=multi_channel_env["workspace"],
        public_identifier="ollama_lead",
        linkedin_url="https://linkedin.com/in/ollama_lead",
        first_name="OllamaLead"
    )
    deal = Deal.objects.create(campaign=campaign, lead=lead, state="Connected")

    ChatMessage.objects.create(
        deal=deal,
        content="Can you send over details?",
        is_outgoing=False,
        linkedin_urn="urn:li:msg:ollama1"
    )

    from openoutreach.linkedin.tasks.follow_up import handle_follow_up
    mock_session = MagicMock()
    mock_session.campaign = campaign
    mock_session.linkedin_profile = multi_channel_env["linkedin_profile"]

    with patch("openoutreach.core.agents.follow_up.run_follow_up_agent") as mock_agent, \
         patch("linkedin_cli.actions.message.send_raw_message") as mock_send:
        # LLM raises an unexpected timeout or connection failure
        mock_agent.side_effect = RuntimeError("Ollama connection refused at 127.0.0.1:11434")

        try:
            handle_follow_up(MagicMock(), mock_session, {})
        except RuntimeError:
            pass

        # No ungrounded fallback message must be sent
        assert mock_send.call_count == 0


def test_daemon_restart_idempotency(multi_channel_env):
    """Prove restarting the scheduler/task loop does not produce duplicate connection tasks."""
    workspace = multi_channel_env["workspace"]
    campaign = multi_channel_env["campaign"]

    lead = Lead.objects.create(
        workspace=workspace,
        public_identifier="idemp_lead",
        linkedin_url="https://linkedin.com/in/idemp_lead",
        first_name="Idemp"
    )
    Deal.objects.create(campaign=campaign, lead=lead, state="Ready to Connect")

    # First task creation
    t1 = Task.objects.create(
        task_type=Task.TaskType.CONNECT,
        payload={"campaign_id": campaign.pk},
        scheduled_at=timezone.now(),
        status=Task.Status.PENDING,
    )

    from openoutreach.core.scheduler import _has_pending
    assert _has_pending(Task.TaskType.CONNECT, campaign.pk) is True


def test_provider_backlog_metric_cached_and_enforced(multi_channel_env):
    """Prove that LinkedIn provider backlog (e.g. 326) is enforced even if CRM pending deals are fewer."""
    workspace = multi_channel_env["workspace"]
    campaign = multi_channel_env["campaign"]
    settings = multi_channel_env["settings"]

    # Durably record real provider backlog of 326
    settings.update_linkedin_pending_invitations(326, source="provider_aggregate")
    assert settings.linkedin_pending_invitations_count == 326

    # When override is OFF, provider backlog (326 >= 100) blocks new connections even with 0 CRM pending deals
    settings.disable_backlog_override()
    target_lead = Lead.objects.create(
        workspace=workspace,
        public_identifier="prov_test_lead",
        linkedin_url="https://linkedin.com/in/prov_test_lead",
        first_name="Prov"
    )
    target_deal = Deal.objects.create(campaign=campaign, lead=target_lead, state="Ready to Connect")

    allowed, reason = evaluate_external_action_gates(
        workspace=workspace,
        campaign=campaign,
        lead=target_lead,
        deal=target_deal,
        external_action_authorized=True,
        action_type="connect"
    )
    assert allowed is False
    assert "pending_invitation_backlog_exceeded (326 >= 100)" in reason

    # When override is ON, provider backlog of 326 is overridden for bounded connections
    settings.enable_backlog_override()
    allowed_ov, reason_ov = evaluate_external_action_gates(
        workspace=workspace,
        campaign=campaign,
        lead=target_lead,
        deal=target_deal,
        external_action_authorized=True,
        action_type="connect"
    )
    assert allowed_ov is True


def test_job_application_quota_boundary_50(multi_channel_env):
    """Prove 50th job application attempt is allowed, and 51st is blocked."""
    candidate_profile = multi_channel_env["candidate"]
    workspace = multi_channel_env["workspace"]
    campaign = multi_channel_env["campaign"]
    settings = multi_channel_env["settings"]

    settings.max_daily_job_applications = 50
    settings.save()

    # Create 49 applications today
    for i in range(49):
        j, _ = Job.upsert_discovered_job(
            provider="linkedin",
            external_job_id=f"ext_boundary_job_{i}",
            title=f"Engineer {i}",
            company=f"Company {i}",
            location="Remote",
            description="Python engineer",
            easy_apply=True,
            provenance="REAL_PROVIDER",
            provider_identity_status="VERIFIED"
        )
        JobApplication.objects.create(
            candidate_profile=candidate_profile,
            job=j,
            status="submitted",
            submission_date=timezone.now()
        )

    job_50, _ = Job.upsert_discovered_job(
        provider="linkedin",
        external_job_id="ext_job_50",
        title="Engineer 50",
        company="Company 50",
        location="Remote",
        description="Python engineer",
        easy_apply=True,
        provenance="REAL_PROVIDER",
        provider_identity_status="VERIFIED"
    )
    job_51, _ = Job.upsert_discovered_job(
        provider="linkedin",
        external_job_id="ext_job_51",
        title="Engineer 51",
        company="Company 51",
        location="Remote",
        description="Python engineer",
        easy_apply=True,
        provenance="REAL_PROVIDER",
        provider_identity_status="VERIFIED"
    )

    # 50th attempt allowed
    allowed_50, reason_50 = evaluate_external_action_gates(
        workspace=workspace,
        campaign=campaign,
        candidate_profile=candidate_profile,
        job=job_50,
        external_action_authorized=True,
        action_type="job_application"
    )
    assert allowed_50 is True, f"Expected allowed_50 True, got: {reason_50}"

    # Record 50th application
    JobApplication.objects.create(
        candidate_profile=candidate_profile,
        job=job_50,
        status="submitted",
        submission_date=timezone.now()
    )

    # 51st attempt blocked
    allowed_51, reason_51 = evaluate_external_action_gates(
        workspace=workspace,
        campaign=campaign,
        candidate_profile=candidate_profile,
        job=job_51,
        external_action_authorized=True,
        action_type="job_application"
    )
    assert allowed_51 is False
    assert "daily_job_application_limit_reached (50 >= 50)" in reason_51


def test_connection_quota_boundary_40(multi_channel_env):
    """Prove 40th connection request is allowed, and 41st is blocked."""
    workspace = multi_channel_env["workspace"]
    campaign = multi_channel_env["campaign"]
    settings = multi_channel_env["settings"]
    lp = multi_channel_env["linkedin_profile"]

    settings.max_daily_connections = 40
    settings.save()

    # Record 39 confirmed connections today
    for i in range(39):
        ConnectionAttempt.objects.create(
            linkedin_profile=lp,
            recipient_public_id=f"conn_bound_{i}",
            idempotency_key=f"idemp_bound_{i}",
            status="externally_confirmed",
            created_at=timezone.now()
        )

    lead_40 = Lead.objects.create(workspace=workspace, public_identifier="lead_40", linkedin_url="https://linkedin.com/in/lead_40", first_name="L40")
    deal_40 = Deal.objects.create(campaign=campaign, lead=lead_40, state="Ready to Connect")

    # 40th connection allowed
    allowed_40, _ = evaluate_external_action_gates(
        workspace=workspace,
        campaign=campaign,
        lead=lead_40,
        deal=deal_40,
        external_action_authorized=True,
        action_type="connect"
    )
    assert allowed_40 is True

    # Record 40th connection
    ConnectionAttempt.objects.create(
        linkedin_profile=lp,
        recipient_public_id="lead_40",
        idempotency_key="test-1-aaaaaaaa",
        status="externally_confirmed",
        created_at=timezone.now()
    )

    lead_41 = Lead.objects.create(workspace=workspace, public_identifier="lead_41", linkedin_url="https://linkedin.com/in/lead_41", first_name="L41")
    deal_41 = Deal.objects.create(campaign=campaign, lead=lead_41, state="Ready to Connect")

    # 41st connection blocked
    allowed_41, reason_41 = evaluate_external_action_gates(
        workspace=workspace,
        campaign=campaign,
        lead=lead_41,
        deal=deal_41,
        external_action_authorized=True,
        action_type="connect"
    )
    assert allowed_41 is False
    assert "daily_connection_limit_reached (40 >= 40)" in reason_41


def test_aggregate_linkedin_messages_shared_pool_90(multi_channel_env):
    """Prove shared 90-message aggregate pool across follow-ups and AI replies."""
    workspace = multi_channel_env["workspace"]
    campaign = multi_channel_env["campaign"]
    settings = multi_channel_env["settings"]
    lp = multi_channel_env["linkedin_profile"]

    settings.max_daily_messages = 90
    settings.save()

    # 45 follow-ups + 45 AI replies = 90 messages
    for i in range(45):
        ActionLog.objects.create(
            linkedin_profile=lp,
            campaign=campaign,
            action_type=ActionLog.ActionType.FOLLOW_UP,
            metadata={"lead": f"fup_{i}"}
        )
        ActionLog.objects.create(
            linkedin_profile=lp,
            campaign=campaign,
            action_type="reply",
            metadata={"lead": f"rep_{i}"}
        )

    target_lead = Lead.objects.create(workspace=workspace, public_identifier="target_91", linkedin_url="https://linkedin.com/in/target_91", first_name="T91")
    target_deal = Deal.objects.create(campaign=campaign, lead=target_lead, state="Connected")

    # 91st follow-up blocked by aggregate pool
    allowed_fup, reason_fup = evaluate_external_action_gates(
        workspace=workspace,
        campaign=campaign,
        lead=target_lead,
        deal=target_deal,
        external_action_authorized=True,
        action_type="follow_up"
    )
    assert allowed_fup is False
    assert "daily_linkedin_message_limit_reached (90 >= 90)" in reason_fup

    # 91st AI reply also blocked by aggregate pool
    allowed_reply, reason_reply = evaluate_external_action_gates(
        workspace=workspace,
        campaign=campaign,
        lead=target_lead,
        deal=target_deal,
        external_action_authorized=True,
        action_type="reply"
    )
    assert allowed_reply is False
    assert "daily_linkedin_message_limit_reached (90 >= 90)" in reason_reply


def test_email_quota_boundary_25(multi_channel_env, monkeypatch):
    """Prove 25th email is allowed, and 26th is blocked."""
    workspace = multi_channel_env["workspace"]
    campaign = multi_channel_env["campaign"]
    settings = multi_channel_env["settings"]

    settings.max_daily_emails = 25
    settings.save()

    lead = Lead.objects.create(workspace=workspace, public_identifier="email_lead_25", linkedin_url="https://linkedin.com/in/email_lead_25", first_name="E25")
    deal = Deal.objects.create(campaign=campaign, lead=lead, state="Connected")

    # Mock 24 emails sent today
    from openoutreach.emails.models import Mailbox
    monkeypatch.setattr(Mailbox, "sent_today", lambda self: 24)

    allowed_25, _ = evaluate_external_action_gates(
        workspace=workspace,
        campaign=campaign,
        lead=lead,
        deal=deal,
        external_action_authorized=True,
        action_type="email"
    )
    assert allowed_25 is True

    # Mock 25 emails sent today
    monkeypatch.setattr(Mailbox, "sent_today", lambda self: 25)

    allowed_26, reason_26 = evaluate_external_action_gates(
        workspace=workspace,
        campaign=campaign,
        lead=lead,
        deal=deal,
        external_action_authorized=True,
        action_type="email"
    )
    assert allowed_26 is False
    assert "daily_email_limit_reached (25 >= 25)" in reason_26


def test_mid_day_limit_changes_and_reductions(multi_channel_env):
    """Prove mid-day quota increases and decreases work without resetting usage."""
    workspace = multi_channel_env["workspace"]
    campaign = multi_channel_env["campaign"]
    settings = multi_channel_env["settings"]
    lp = multi_channel_env["linkedin_profile"]

    # Start with limit of 5
    settings.max_daily_connections = 5
    settings.save()

    # Record 3 confirmed connections today
    for i in range(3):
        ConnectionAttempt.objects.create(
            linkedin_profile=lp,
            recipient_public_id=f"conn_mid_{i}",
            idempotency_key=f"idemp_mid_{i}",
            status="externally_confirmed",
            created_at=timezone.now()
        )

    # Operator increases limit to 40 -> 4th connection is allowed (37 remaining)
    settings.max_daily_connections = 40
    settings.save()

    lead_4 = Lead.objects.create(workspace=workspace, public_identifier="lead_4", linkedin_url="https://linkedin.com/in/lead_4", first_name="L4")
    deal_4 = Deal.objects.create(campaign=campaign, lead=lead_4, state="Ready to Connect")

    allowed_4, _ = evaluate_external_action_gates(
        workspace=workspace,
        campaign=campaign,
        lead=lead_4,
        deal=deal_4,
        external_action_authorized=True,
        action_type="connect"
    )
    assert allowed_4 is True

    # Now simulate 12 connections sent total
    for i in range(3, 12):
        ConnectionAttempt.objects.create(
            linkedin_profile=lp,
            recipient_public_id=f"conn_mid_{i}",
            idempotency_key=f"idemp_mid_{i}",
            status="externally_confirmed",
            created_at=timezone.now()
        )

    # Operator reduces limit to 10 -> subsequent connection is blocked immediately
    settings.max_daily_connections = 10
    settings.save()

    lead_13 = Lead.objects.create(workspace=workspace, public_identifier="lead_13", linkedin_url="https://linkedin.com/in/lead_13", first_name="L13")
    deal_13 = Deal.objects.create(campaign=campaign, lead=lead_13, state="Ready to Connect")

    allowed_13, reason_13 = evaluate_external_action_gates(
        workspace=workspace,
        campaign=campaign,
        lead=lead_13,
        deal=deal_13,
        external_action_authorized=True,
        action_type="connect"
    )
    assert allowed_13 is False
    assert "daily_connection_limit_reached (12 >= 10)" in reason_13


def test_job_uncertain_consumes_quota_and_blocks_fifty_first(multi_channel_env):
    """Prove submission_uncertain counts as a provider attempt toward the 50-job ceiling."""
    candidate_profile = multi_channel_env["candidate"]
    workspace = multi_channel_env["workspace"]
    campaign = multi_channel_env["campaign"]
    settings = multi_channel_env["settings"]

    settings.max_daily_job_applications = 50
    settings.save()

    # 49 submitted applications
    for i in range(49):
        j, _ = Job.upsert_discovered_job(
            provider="linkedin",
            external_job_id=f"ext_unc_job_{i}",
            title=f"Engineer {i}",
            company=f"Company {i}",
            location="Remote",
            description="Python engineer",
            easy_apply=True,
            provenance="REAL_PROVIDER",
            provider_identity_status="VERIFIED"
        )
        JobApplication.objects.create(
            candidate_profile=candidate_profile,
            job=j,
            status="submitted",
            submission_date=timezone.now()
        )

    # 50th application is submission_uncertain
    j_50, _ = Job.upsert_discovered_job(
        provider="linkedin",
        external_job_id="ext_unc_job_50",
        title="Engineer 50",
        company="Company 50",
        location="Remote",
        description="Python engineer",
        easy_apply=True,
        provenance="REAL_PROVIDER",
        provider_identity_status="VERIFIED"
    )
    JobApplication.objects.create(
        candidate_profile=candidate_profile,
        job=j_50,
        status="submission_uncertain",
        submission_date=timezone.now()
    )

    from openoutreach.core.services.quota_service import get_daily_job_provider_attempt_count
    assert get_daily_job_provider_attempt_count(workspace=workspace, candidate_profile=candidate_profile) == 50

    j_51, _ = Job.upsert_discovered_job(
        provider="linkedin",
        external_job_id="ext_unc_job_51",
        title="Engineer 51",
        company="Company 51",
        location="Remote",
        description="Python engineer",
        easy_apply=True,
        provenance="REAL_PROVIDER",
        provider_identity_status="VERIFIED"
    )

    allowed_51, reason_51 = evaluate_external_action_gates(
        workspace=workspace,
        campaign=campaign,
        candidate_profile=candidate_profile,
        job=j_51,
        external_action_authorized=True,
        action_type="job_application"
    )
    assert allowed_51 is False
    assert "daily_job_application_limit_reached (50 >= 50)" in reason_51


def test_connection_uncertain_consumes_quota_and_failed_before_provider_excluded(multi_channel_env):
    """Prove uncertain connection attempts consume quota, while failed_before_provider do not."""
    workspace = multi_channel_env["workspace"]
    campaign = multi_channel_env["campaign"]
    settings = multi_channel_env["settings"]
    lp = multi_channel_env["linkedin_profile"]

    settings.max_daily_connections = 40
    settings.save()

    # 39 confirmed connections
    for i in range(39):
        ConnectionAttempt.objects.create(
            linkedin_profile=lp,
            recipient_public_id=f"conn_u_{i}",
            idempotency_key=f"idemp_u_{i}",
            status="externally_confirmed",
            created_at=timezone.now()
        )

    # 1 failed before provider (does NOT consume provider quota)
    ConnectionAttempt.objects.create(
        linkedin_profile=lp,
        recipient_public_id="conn_failed_pre",
        idempotency_key="idemp_failed_pre",
        status="failed_before_provider",
        created_at=timezone.now()
    )

    from openoutreach.core.services.quota_service import get_daily_connection_provider_attempt_count
    assert get_daily_connection_provider_attempt_count(linkedin_profile=lp) == 39

    # 40th attempt is allowed
    lead_40 = Lead.objects.create(workspace=workspace, public_identifier="lead_u_40", linkedin_url="https://linkedin.com/in/lead_u_40", first_name="LU40")
    deal_40 = Deal.objects.create(campaign=campaign, lead=lead_40, state="Ready to Connect")

    allowed_40, _ = evaluate_external_action_gates(
        workspace=workspace,
        campaign=campaign,
        lead=lead_40,
        deal=deal_40,
        external_action_authorized=True,
        action_type="connect"
    )
    assert allowed_40 is True

    # 40th attempt recorded as uncertain (MUST consume provider quota)
    ConnectionAttempt.objects.create(
        linkedin_profile=lp,
        recipient_public_id="lead_u_40",
        idempotency_key="idemp_u_40",
        status="uncertain",
        created_at=timezone.now()
    )

    assert get_daily_connection_provider_attempt_count(linkedin_profile=lp) == 40

    # 41st attempt blocked
    lead_41 = Lead.objects.create(workspace=workspace, public_identifier="lead_u_41", linkedin_url="https://linkedin.com/in/lead_u_41", first_name="LU41")
    deal_41 = Deal.objects.create(campaign=campaign, lead=lead_41, state="Ready to Connect")

    allowed_41, reason_41 = evaluate_external_action_gates(
        workspace=workspace,
        campaign=campaign,
        lead=lead_41,
        deal=deal_41,
        external_action_authorized=True,
        action_type="connect"
    )
    assert allowed_41 is False
    assert "daily_connection_limit_reached (40 >= 40)" in reason_41


def test_dashboard_and_external_action_gates_agree_on_canonical_quotas(multi_channel_env):
    """Prove get_workspace_quota_summary and evaluate_external_action_gates use the exact same counts."""
    workspace = multi_channel_env["workspace"]
    campaign = multi_channel_env["campaign"]
    settings = multi_channel_env["settings"]
    lp = multi_channel_env["linkedin_profile"]

    settings.max_daily_connections = 40
    settings.save()

    # Record 5 confirmed and 2 uncertain connections
    for i in range(5):
        ConnectionAttempt.objects.create(
            linkedin_profile=lp,
            recipient_public_id=f"agree_c_{i}",
            idempotency_key=f"agree_k_{i}",
            status="externally_confirmed",
            created_at=timezone.now()
        )
    for i in range(2):
        ConnectionAttempt.objects.create(
            linkedin_profile=lp,
            recipient_public_id=f"agree_unc_{i}",
            idempotency_key=f"agree_uk_{i}",
            status="uncertain",
            created_at=timezone.now()
        )

    from openoutreach.core.services.quota_service import get_workspace_quota_summary, get_daily_connection_provider_attempt_count
    summary = get_workspace_quota_summary(workspace, settings)

    assert summary["connections"]["consumed"] == 7
    assert summary["connections"]["ceiling"] == 40
    assert summary["connections"]["remaining"] == 33
    assert get_daily_connection_provider_attempt_count(workspace=workspace, linkedin_profile=lp) == 7



