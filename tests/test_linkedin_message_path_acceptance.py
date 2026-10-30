# tests/test_linkedin_message_path_acceptance.py
"""
HuntFlow LinkedIn Message Path & AI Reply Full Acceptance Test Suite.
Validates end-to-end execution, reachability, canonical Candidate voice gating,
durable idempotency, deduplication, and zero-provider-write safety boundaries.
"""
import pytest
from datetime import timedelta
from unittest.mock import MagicMock, patch
from django.utils import timezone

from openoutreach.crm.models import Lead, Deal, DealState
from openoutreach.chat.models import ChatMessage
from openoutreach.core.models import (
    Campaign,
    Workspace,
    WorkspaceAutomationSettings,
    Task,
)
from openoutreach.linkedin.models import LinkedInProfile, ProviderWriteIntent, ActionLog
from openoutreach.core.agents.follow_up import FollowUpDecision
from openoutreach.linkedin.tasks.follow_up import handle_follow_up
from openoutreach.core.content_style import validate_linkedin_outbound_message


@pytest.fixture
def message_env(db):
    """Fixture providing a configured multi-tenant environment for messaging tests."""
    ws = Workspace.objects.create(name="Acceptance Workspace", slug="acc-ws")
    auto_st = WorkspaceAutomationSettings.objects.create(
        workspace=ws,
        master_automation_enabled=True,
        auto_send_connections=True,
        auto_followup=True,
        auto_monitor_replies=True,
        max_daily_messages=50,
        max_daily_ai_replies=20,
    )
    from django.contrib.auth.models import User
    user = User.objects.create(username="test_acc_user")
    profile = LinkedInProfile.objects.create(
        user=user,
        workspace=ws,
        linkedin_username="candidate.acceptance@example.com",
        session_status="authenticated",
        identity_validation_status="MATCHED_CANONICAL",
        connect_daily_limit=50,
        follow_up_daily_limit=50,
    )
    campaign = Campaign.objects.create(
        workspace=ws,
        name="Acceptance Campaign",
        is_active=True,
        linkedin_profile=profile,
    )
    lead = Lead.objects.create(
        public_identifier="prospect-acc-01",
        first_name="Prospect",
        last_name="Alpha",
        linkedin_url="https://www.linkedin.com/in/prospect-acc-01",
        headline="Principal Geoscientist at Pilbara Minerals",
    )
    deal = Deal.objects.create(
        lead=lead,
        campaign=campaign,
        state=DealState.CONNECTED.value,
    )
    from openoutreach.linkedin.models import ProviderRelationship
    ProviderRelationship.objects.create(
        workspace=ws,
        linkedin_profile=profile,
        lead=lead,
        deal=deal,
        classification="CONNECTED",
        evidence_sufficient=True,
        last_observed_at=timezone.now(),
    )
    session_mock = MagicMock()
    session_mock.linkedin_profile = profile
    session_mock.campaign = campaign
    session_mock.django_user = None

    task_mock = MagicMock()
    task_mock.name = "follow_up"

    return {
        "ws": ws,
        "auto_st": auto_st,
        "profile": profile,
        "campaign": campaign,
        "lead": lead,
        "deal": deal,
        "session": session_mock,
        "task": task_mock,
    }


def test_ai_reply_reachable_and_successful(message_env):
    """
    Section 19: Full Mock Test Proving AI Reply Reachability and Execution.
    1. Deal is connected.
    2. Persisted inbound ChatMessage exists.
    3. AI replies are enabled.
    4. Reply generator returns valid Candidate-style text.
    5. Canonical outbound gate allows it.
    6. Exactly ONE AI_REPLY_SEND ProviderWriteIntent is created and reaches CONFIRMED.
    7. Mocked provider message sender is called exactly once.
    8. Outbound ChatMessage and ActionLog recorded.
    """
    deal = message_env["deal"]
    session = message_env["session"]
    task = message_env["task"]

    # 1 & 2: Inbound ChatMessage
    inbound_msg = ChatMessage.objects.create(
        deal=deal,
        content="Hey Candidate, thanks for reaching out. What kind of geospatial screening tools are you building?",
        is_outgoing=False,
        creation_date=timezone.now() - timedelta(minutes=30),
        linkedin_urn="urn:li:msg:acc_inbound_01",
    )

    valid_candidate_reply = "yeah makes sense... we're building automated satellite + geological anomaly screening for exploration targets. curious what tenements you're focused on rn?"
    decision = FollowUpDecision(
        action="send_message",
        message=valid_candidate_reply,
        follow_up_hours=24,
        outcome=None,
    )

    with patch("openoutreach.linkedin.services.account_governor.is_account_safe_for_provider_write", return_value=(True, "ok")), \
         patch("openoutreach.linkedin.services.account_governor.is_account_safe_for_provider_read", return_value=(True, "ok")), \
         patch("openoutreach.core.db.summaries.materialize_profile_summary_if_missing"), \
         patch("openoutreach.core.agents.follow_up.run_follow_up_agent", return_value=decision), \
         patch("linkedin_cli.actions.message.send_raw_message", return_value=True) as mock_send, \
         patch("openoutreach.linkedin.db.chat.sync_conversation"):

        handle_follow_up(task, session, qualifiers={})

    # Assert mocked sender was called exactly once with the valid Candidate text
    assert mock_send.call_count == 1
    call_args = mock_send.call_args[0]
    assert call_args[2] == valid_candidate_reply

    # Verify exactly ONE AI_REPLY_SEND ProviderWriteIntent exists and is CONFIRMED
    intents = ProviderWriteIntent.objects.filter(
        target_identity=deal.lead.public_identifier,
        operation_type=ProviderWriteIntent.OperationType.AI_REPLY_SEND,
    )
    assert intents.count() == 1
    intent = intents.first()
    assert intent.state == ProviderWriteIntent.State.CONFIRMED
    assert intent.logical_action_identity == f"deal_{deal.id}_inbound_{inbound_msg.id}"

    # Verify outgoing ChatMessage recorded
    outbound_chat = ChatMessage.objects.filter(deal=deal, is_outgoing=True).first()
    assert outbound_chat is not None
    assert outbound_chat.content == valid_candidate_reply

    # Verify ActionLog recorded
    action = ActionLog.objects.filter(
        linkedin_profile=message_env["profile"],
        action_type=ActionLog.ActionType.FOLLOW_UP,
    ).first()
    assert action is not None
    assert action.metadata.get("operation_type") == ProviderWriteIntent.OperationType.AI_REPLY_SEND


def test_ai_reply_style_rejection_blocks_send(message_env):
    """
    Section 20: AI Reply Style-Block Test.
    When generator produces corporate/cliché text:
    - Canonical gate blocks it.
    - Provider sender call count = 0.
    - ProviderWriteIntent reaches FAILED_BEFORE_PROVIDER.
    - Account soak_status / safety stop is NOT triggered merely by style failure.
    """
    deal = message_env["deal"]
    session = message_env["session"]
    task = message_env["task"]
    auto_st = message_env["auto_st"]

    ChatMessage.objects.create(
        deal=deal,
        content="Tell me more about your work.",
        is_outgoing=False,
        creation_date=timezone.now() - timedelta(minutes=15),
        linkedin_urn="urn:li:msg:acc_inbound_02",
    )

    bad_corporate_reply = "Thank you for your insightful response. I would be delighted to discuss this further and leverage our synergies."
    decision = FollowUpDecision(
        action="send_message",
        message=bad_corporate_reply,
        follow_up_hours=24,
        outcome=None,
    )

    with patch("openoutreach.linkedin.services.account_governor.is_account_safe_for_provider_write", return_value=(True, "ok")), \
         patch("openoutreach.linkedin.services.account_governor.is_account_safe_for_provider_read", return_value=(True, "ok")), \
         patch("openoutreach.core.db.summaries.materialize_profile_summary_if_missing"), \
         patch("openoutreach.core.agents.follow_up.run_follow_up_agent", return_value=decision), \
         patch("linkedin_cli.actions.message.send_raw_message", return_value=True) as mock_send, \
         patch("openoutreach.linkedin.db.chat.sync_conversation"):

        handle_follow_up(task, session, qualifiers={})

    # Provider sender must NEVER be called with rejected style
    assert mock_send.call_count == 0

    # Write intent failed before provider
    intent = ProviderWriteIntent.objects.filter(
        target_identity=deal.lead.public_identifier,
        operation_type=ProviderWriteIntent.OperationType.AI_REPLY_SEND,
    ).first()
    assert intent is None or intent.state == ProviderWriteIntent.State.FAILED_BEFORE_PROVIDER

    # Account safety stop NOT triggered
    auto_st.refresh_from_db()
    assert auto_st.soak_status != "SAFETY_STOP"


def test_duplicate_inbound_reply_deduplication(message_env):
    """
    Section 21: Deduplication Test.
    Processing the same inbound event twice must produce exactly 1 provider send
    and exactly 1 confirmed ProviderWriteIntent.
    """
    deal = message_env["deal"]
    session = message_env["session"]
    task = message_env["task"]

    inbound_msg = ChatMessage.objects.create(
        deal=deal,
        content="Sounds interesting, how does the model work?",
        is_outgoing=False,
        creation_date=timezone.now() - timedelta(minutes=10),
        linkedin_urn="urn:li:msg:acc_inbound_03",
    )

    valid_reply = "we use spectral bands + magnetics to highlight target structures... happy to show you a quick demo if you're keen"
    decision = FollowUpDecision(
        action="send_message",
        message=valid_reply,
        follow_up_hours=24,
        outcome=None,
    )

    with patch("openoutreach.linkedin.services.account_governor.is_account_safe_for_provider_write", return_value=(True, "ok")), \
         patch("openoutreach.linkedin.services.account_governor.is_account_safe_for_provider_read", return_value=(True, "ok")), \
         patch("openoutreach.core.db.summaries.materialize_profile_summary_if_missing"), \
         patch("openoutreach.core.agents.follow_up.run_follow_up_agent", return_value=decision), \
         patch("linkedin_cli.actions.message.send_raw_message", return_value=True) as mock_send, \
         patch("openoutreach.linkedin.db.chat.sync_conversation"):

        # First run: should send and confirm
        handle_follow_up(task, session, qualifiers={})
        assert mock_send.call_count == 1

        # Second run with same inbound: must be deduplicated
        handle_follow_up(task, session, qualifiers={})
        assert mock_send.call_count == 1  # Still 1, not 2!

    # Verify only 1 confirmed intent
    intents = ProviderWriteIntent.objects.filter(
        target_identity=deal.lead.public_identifier,
        operation_type=ProviderWriteIntent.OperationType.AI_REPLY_SEND,
    )
    assert intents.count() == 1
    assert intents.first().state == ProviderWriteIntent.State.CONFIRMED


def test_inbound_reply_with_ai_replies_disabled_suppresses_send(message_env):
    """
    Section 6: When inbound message exists but AI replies are disabled:
    suppresses canned follow-up and returns without provider send.
    """
    deal = message_env["deal"]
    session = message_env["session"]
    task = message_env["task"]
    auto_st = message_env["auto_st"]
    auto_st.auto_monitor_replies = False
    auto_st.save()

    ChatMessage.objects.create(
        deal=deal,
        content="Let's chat next month.",
        is_outgoing=False,
        creation_date=timezone.now() - timedelta(minutes=5),
        linkedin_urn="urn:li:msg:acc_inbound_04",
    )

    with patch("openoutreach.linkedin.services.account_governor.is_account_safe_for_provider_write", return_value=(True, "ok")), \
         patch("openoutreach.linkedin.services.account_governor.is_account_safe_for_provider_read", return_value=(True, "ok")), \
         patch("linkedin_cli.actions.message.send_raw_message", return_value=True) as mock_send:

        handle_follow_up(task, session, qualifiers={})

    assert mock_send.call_count == 0
    assert ProviderWriteIntent.objects.filter(target_identity=deal.lead.public_identifier).count() == 0


def test_first_message_and_follow_up_routing(message_env):
    """
    Section 14: Verify FIRST_MESSAGE_SEND vs FOLLOW_UP_SEND routing and write intents.
    """
    deal = message_env["deal"]
    session = message_env["session"]
    task = message_env["task"]

    # 1. No outbound or inbound messages -> FIRST_MESSAGE_SEND
    decision_first = FollowUpDecision(
        action="send_message",
        message="hey Prospect, saw your work at Pilbara Minerals... curious what exploration projects you're running rn?",
        follow_up_hours=72,
        outcome=None,
    )

    with patch("openoutreach.linkedin.services.account_governor.is_account_safe_for_provider_write", return_value=(True, "ok")), \
         patch("openoutreach.linkedin.services.account_governor.is_account_safe_for_provider_read", return_value=(True, "ok")), \
         patch("openoutreach.core.db.summaries.materialize_profile_summary_if_missing"), \
         patch("openoutreach.core.agents.follow_up.run_follow_up_agent", return_value=decision_first), \
         patch("linkedin_cli.actions.message.send_raw_message", return_value=True) as mock_send, \
         patch("openoutreach.linkedin.db.chat.sync_conversation"):

        handle_follow_up(task, session, qualifiers={})

    assert mock_send.call_count == 1
    intent_first = ProviderWriteIntent.objects.filter(
        target_identity=deal.lead.public_identifier,
        operation_type=ProviderWriteIntent.OperationType.FIRST_MESSAGE_SEND,
    ).first()
    assert intent_first is not None
    assert intent_first.state == ProviderWriteIntent.State.CONFIRMED


def test_legacy_corporate_draft_blocked_by_canonical_gate(message_env):
    """
    Section 17: Pre-persisted legacy draft created before the patch must be blocked
    by the canonical outbound gate before reaching provider send.
    """
    deal = message_env["deal"]
    ws = message_env["ws"]
    profile = message_env["profile"]

    legacy_corporate_draft = "Hello, I would be delighted to connect and exchange insights on your professional journey."

    res = validate_linkedin_outbound_message(
        message_text=legacy_corporate_draft,
        content_type="follow_up",
        workspace=ws,
        linkedin_profile=profile,
        deal=deal,
    )

    assert not res["allowed"]
    assert res["reason"] == "MESSAGE_BLOCKED_STYLE"
    assert "CORPORATE_LANGUAGE" in res["error_codes"] or "FORMAL_OPENING" in res["error_codes"]


def test_ai_reply_fail_closed_missing_workspace(message_env):
    """
    Section 8A: Workspace is None -> Fail closed immediately, 0 provider calls.
    """
    deal = message_env["deal"]
    session = message_env["session"]
    task = message_env["task"]
    session.campaign.workspace = None

    ChatMessage.objects.create(
        deal=deal,
        content="Hey, what tools are you building?",
        is_outgoing=False,
        creation_date=timezone.now() - timedelta(minutes=10),
    )

    with patch("linkedin_cli.actions.message.send_raw_message", return_value=True) as mock_send:
        handle_follow_up(task, session, qualifiers={})

    assert mock_send.call_count == 0
    assert ProviderWriteIntent.objects.filter(target_identity=deal.lead.public_identifier).count() == 0


def test_ai_reply_fail_closed_missing_automation_settings(message_env):
    """
    Section 8B: Workspace exists but automation settings row is missing/deleted -> Fail closed, 0 provider calls.
    """
    deal = message_env["deal"]
    session = message_env["session"]
    task = message_env["task"]
    message_env["auto_st"].delete()

    ChatMessage.objects.create(
        deal=deal,
        content="Hey, what tools are you building?",
        is_outgoing=False,
        creation_date=timezone.now() - timedelta(minutes=10),
    )

    with patch("linkedin_cli.actions.message.send_raw_message", return_value=True) as mock_send:
        handle_follow_up(task, session, qualifiers={})

    assert mock_send.call_count == 0
    assert ProviderWriteIntent.objects.filter(target_identity=deal.lead.public_identifier).count() == 0


def test_ai_reply_fail_closed_missing_provider_profile(message_env):
    """
    Section 8D: Provider LinkedInProfile is unresolved -> Fail closed, 0 provider calls.
    """
    deal = message_env["deal"]
    session = message_env["session"]
    task = message_env["task"]
    session.linkedin_profile = None

    ChatMessage.objects.create(
        deal=deal,
        content="Hey, what tools are you building?",
        is_outgoing=False,
        creation_date=timezone.now() - timedelta(minutes=10),
    )

    with patch("linkedin_cli.actions.message.send_raw_message", return_value=True) as mock_send:
        handle_follow_up(task, session, qualifiers={})

    assert mock_send.call_count == 0
    assert ProviderWriteIntent.objects.filter(target_identity=deal.lead.public_identifier).count() == 0


def test_ai_reply_fail_closed_ambiguous_provider_context(message_env):
    """
    Section 8E: Provider profile workspace does not match campaign workspace -> Fail closed, 0 provider calls.
    """
    deal = message_env["deal"]
    session = message_env["session"]
    task = message_env["task"]

    other_ws = Workspace.objects.create(name="Other Workspace", slug="other-ws")
    session.linkedin_profile.workspace = other_ws
    session.linkedin_profile.save()

    ChatMessage.objects.create(
        deal=deal,
        content="Hey, what tools are you building?",
        is_outgoing=False,
        creation_date=timezone.now() - timedelta(minutes=10),
    )

    with patch("linkedin_cli.actions.message.send_raw_message", return_value=True) as mock_send:
        handle_follow_up(task, session, qualifiers={})

    assert mock_send.call_count == 0
    assert ProviderWriteIntent.objects.filter(target_identity=deal.lead.public_identifier).count() == 0


def test_first_message_relationship_eligibility_matrix(message_env):
    """
    Section 18: Relationship eligibility matrix for first_message.
    Only CONNECTED deals with zero outbound and no uncertain intents are allowed.
    All other states (Pending, Invitation Sent, Qualified, Ready to Connect, Unknown, Failed) are BLOCKED.
    """
    deal = message_env["deal"]
    ws = message_env["ws"]
    campaign = message_env["campaign"]
    lead = message_env["lead"]
    profile = message_env["profile"]

    from openoutreach.core.services.external_action_gates import evaluate_external_action_gates
    from openoutreach.core.content_style import validate_linkedin_outbound_message
    from openoutreach.crm.models import DealState

    valid_draft = "hey sam, saw your work at pilbara minerals... curious what projects you're focused on rn?"

    # 1. CONNECTED -> Allowed
    deal.state = DealState.CONNECTED
    deal.save()
    gate_ok, gate_reason = evaluate_external_action_gates(
        workspace=ws, campaign=campaign, lead=lead, deal=deal, action_type="first_message"
    )
    assert gate_ok is True
    out_res = validate_linkedin_outbound_message(
        message_text=valid_draft, content_type="first_message", workspace=ws, linkedin_profile=profile, deal=deal
    )
    assert out_res["allowed"] is True

    # 2. Non-connected persisted states -> Strictly BLOCKED
    persisted_blocked_states = [
        DealState.PENDING,
        DealState.QUALIFIED,
        DealState.READY_TO_CONNECT,
        DealState.READY_TO_EMAIL,
        DealState.EMAILED,
        DealState.FAILED,
        DealState.COMPLETED,
    ]
    for st in persisted_blocked_states:
        deal.state = st
        deal.save()
        gate_ok, gate_reason = evaluate_external_action_gates(
            workspace=ws, campaign=campaign, lead=lead, deal=deal, action_type="first_message"
        )
        assert gate_ok is False, f"State '{st}' should be blocked by external action gates"
        assert "deal_not_connected" in gate_reason or "deal_state_invalid" in gate_reason

        out_res = validate_linkedin_outbound_message(
            message_text=valid_draft, content_type="first_message", workspace=ws, linkedin_profile=profile, deal=deal
        )
        assert out_res["allowed"] is False, f"State '{st}' should be blocked by outbound message gate"
        assert out_res["reason"] == "MESSAGE_BLOCKED_RELATIONSHIP"

    # Also test unpersisted external/transient state strings (e.g. Invitation Sent, Unknown, Opted Out)
    for raw_st in ["Invitation Sent", "Unknown", "Opted Out", "Withdrawn"]:
        deal.state = raw_st
        gate_ok, gate_reason = evaluate_external_action_gates(
            workspace=ws, campaign=campaign, lead=lead, deal=deal, action_type="first_message"
        )
        assert gate_ok is False
        out_res = validate_linkedin_outbound_message(
            message_text=valid_draft, content_type="first_message", workspace=ws, linkedin_profile=profile, deal=deal
        )
        assert out_res["allowed"] is False
        assert out_res["reason"] == "MESSAGE_BLOCKED_RELATIONSHIP"

    # 3. CONNECTED + duplicate outbound -> BLOCKED
    deal.state = DealState.CONNECTED
    deal.save()
    ChatMessage.objects.create(deal=deal, content="Prior first message", is_outgoing=True)
    gate_ok, gate_reason = evaluate_external_action_gates(
        workspace=ws, campaign=campaign, lead=lead, deal=deal, action_type="first_message"
    )
    assert gate_ok is False
    assert gate_reason == "first_message_already_sent"
    out_res = validate_linkedin_outbound_message(
        message_text=valid_draft, content_type="first_message", workspace=ws, linkedin_profile=profile, deal=deal
    )
    assert out_res["allowed"] is False
    assert out_res["reason"] == "MESSAGE_BLOCKED_DUPLICATE"

    # Clean up chat message
    ChatMessage.objects.filter(deal=deal).delete()

    # 4. CONNECTED + UNCERTAIN ProviderWriteIntent -> BLOCKED
    intent = ProviderWriteIntent.objects.create(
        workspace=ws,
        linkedin_profile=profile,
        target_identity=lead.public_identifier,
        logical_action_identity=f"deal_{deal.id}_first_msg",
        operation_type=ProviderWriteIntent.OperationType.FIRST_MESSAGE_SEND,
        state=ProviderWriteIntent.State.UNCERTAIN,
    )
    gate_ok, gate_reason = evaluate_external_action_gates(
        workspace=ws, campaign=campaign, lead=lead, deal=deal, action_type="first_message"
    )
    assert gate_ok is False
    assert gate_reason == "uncertain_write_intent_active"
    out_res = validate_linkedin_outbound_message(
        message_text=valid_draft, content_type="first_message", workspace=ws, linkedin_profile=profile, deal=deal
    )
    assert out_res["allowed"] is False
    assert out_res["reason"] == "MESSAGE_BLOCKED_UNCERTAIN_WRITE"


def test_operator_approval_cannot_override_pending_relationship(message_env):
    """
    Section 19: An OperatorActionApproval for first_message on a Pending deal must fail closed.
    Approval authorizes the execution budget, but does not substitute for LinkedIn relationship state.
    """
    deal = message_env["deal"]
    ws = message_env["ws"]
    campaign = message_env["campaign"]
    lead = message_env["lead"]
    profile = message_env["profile"]
    session = message_env["session"]
    task = message_env["task"]
    user = profile.user

    deal.state = "Pending"
    deal.save()

    from openoutreach.core.models import OperatorActionApproval
    approval = OperatorActionApproval.objects.create(
        workspace=ws,
        operator=user,
        action_type="follow_up",
        status="approved",
    )

    from openoutreach.core.services.external_action_gates import evaluate_external_action_gates
    gate_ok, gate_reason = evaluate_external_action_gates(
        workspace=ws,
        campaign=campaign,
        lead=lead,
        deal=deal,
        action_type="first_message",
        operator_approval=approval,
    )
    assert gate_ok is False
    assert gate_reason == "deal_not_connected_cannot_first_message"

    with patch("linkedin_cli.actions.message.send_raw_message", return_value=True) as mock_send:
        handle_follow_up(task, session, qualifiers={})

    assert mock_send.call_count == 0
    assert ProviderWriteIntent.objects.filter(target_identity=lead.public_identifier).count() == 0


def test_execution_time_relationship_revalidation(message_env):
    """
    Section 20: Execution-time revalidation — if relationship state changes after candidate selection,
    final pre-send gate catches it and fails write intent closed without provider action.
    """
    deal = message_env["deal"]
    session = message_env["session"]
    task = message_env["task"]

    deal.state = "Connected"
    deal.save()

    decision = FollowUpDecision(
        action="send_message",
        message="hey sam, saw your work at pilbara minerals... curious what projects you're focused on rn?",
        follow_up_hours=72,
        outcome=None,
    )

    # Mutate deal state during agent run to simulate race condition / state transition
    def simulate_relationship_revocation(sess, d, *args, **kwargs):
        d.state = "Failed"
        d.save()
        return decision

    with patch("openoutreach.linkedin.services.account_governor.is_account_safe_for_provider_write", return_value=(True, "ok")), \
         patch("openoutreach.linkedin.services.account_governor.is_account_safe_for_provider_read", return_value=(True, "ok")), \
         patch("openoutreach.core.db.summaries.materialize_profile_summary_if_missing"), \
         patch("openoutreach.core.agents.follow_up.run_follow_up_agent", side_effect=simulate_relationship_revocation), \
         patch("linkedin_cli.actions.message.send_raw_message", return_value=True) as mock_send, \
         patch("openoutreach.linkedin.db.chat.sync_conversation"):

        handle_follow_up(task, session, qualifiers={})

    assert mock_send.call_count == 0


