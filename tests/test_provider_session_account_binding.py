"""
Tests for Final Provider Session & Send-Boundary Account Binding.
Verifies that all provider write operations strictly bind to the exact authenticated session LinkedInProfile,
preventing cross-account leakage, campaign/session profile mismatches, approval hijacking, and lease collisions.
"""
import hashlib
from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from openoutreach.core.models import (
    Campaign,
    OperatorActionApproval,
    Task,
    Workspace,
    WorkspaceAutomationSettings,
)
from openoutreach.career.models import CandidateProfile, Job, ApplicationDraft, IncidentHold, IncidentHoldTarget
from openoutreach.crm.models import Deal, DealState, Lead
from openoutreach.linkedin.models import (
    ActionLog,
    ConnectionAttempt,
    LinkedInProfile,
    ProviderAccountLease,
    ProviderOperationType,
    ProviderReadLedger,
    ProviderRelationship,
    ProviderWriteIntent,
)
from openoutreach.linkedin.services.account_governor import (
    account_provider_lock,
    is_account_safe_for_provider_write,
    is_account_safe_for_provider_read,
    resolve_provider_context,
)
from openoutreach.linkedin.services.idempotency import (
    claim_provider_write,
    generate_write_idempotency_key,
)
from openoutreach.core.services.external_action_gates import evaluate_external_action_gates
from openoutreach.core.content_style import validate_linkedin_outbound_message
from openoutreach.linkedin.tasks.connect import handle_connect
from openoutreach.linkedin.tasks.follow_up import handle_follow_up
from openoutreach.career.services.job_application_engine import execute_job_application


@pytest.fixture
def binding_setup(db):
    user_a = User.objects.create_user(username="operator_a", password="password")
    user_b = User.objects.create_user(username="operator_b", password="password")

    ws = Workspace.objects.create(name="Binding WS", slug="binding-ws")
    auto_st, _ = WorkspaceAutomationSettings.objects.get_or_create(
        workspace=ws,
        defaults={
            "master_automation_enabled": True,
            "auto_send_connections": True,
            "auto_followup": True,
            "auto_monitor_replies": True,
            "auto_apply_jobs": True,
            "max_daily_connections": 50,
            "max_daily_messages": 50,
        },
    )

    prof_a = LinkedInProfile.objects.create(
        user=user_a,
        workspace=ws,
        linkedin_username="user_a@example.com",
        active=True,
        session_status="valid",
        identity_validation_status="VALIDATED",
        connect_daily_limit=20,
        follow_up_daily_limit=20,
    )
    prof_b = LinkedInProfile.objects.create(
        user=user_b,
        workspace=ws,
        linkedin_username="user_b@example.com",
        active=True,
        session_status="valid",
        identity_validation_status="VALIDATED",
        connect_daily_limit=20,
        follow_up_daily_limit=20,
    )

    camp_a = Campaign.objects.create(
        workspace=ws,
        name="Campaign A",
        linkedin_profile=prof_a,
        connection_note_enabled=True,
    )
    camp_a.users.add(user_a)

    camp_b = Campaign.objects.create(
        workspace=ws,
        name="Campaign B",
        linkedin_profile=prof_b,
        connection_note_enabled=True,
    )
    camp_b.users.add(user_b)

    lead = Lead.objects.create(
        workspace=ws,
        public_identifier="prospect-lead-1",
        first_name="Jane",
        last_name="Doe",
        linkedin_url="https://www.linkedin.com/in/prospect-lead-1",
    )

    deal_a = Deal.objects.create(
        lead=lead,
        campaign=camp_a,
        state=DealState.READY_TO_CONNECT,
    )

    return {
        "workspace": ws,
        "auto_settings": auto_st,
        "user_a": user_a,
        "user_b": user_b,
        "prof_a": prof_a,
        "prof_b": prof_b,
        "camp_a": camp_a,
        "camp_b": camp_b,
        "lead": lead,
        "deal_a": deal_a,
    }


def _make_mock_session(profile, campaign):
    session = MagicMock()
    session.linkedin_profile = profile
    session.campaign = campaign
    session.campaigns = [campaign]
    session.page = MagicMock()
    session.ensure_browser = MagicMock()
    return session


@pytest.mark.django_db
def test_connect_session_missing_profile_fails_closed(binding_setup):
    camp_a = binding_setup["camp_a"]
    session = _make_mock_session(None, camp_a)

    task = Task.objects.create(
        task_type="connect",
        scheduled_at=timezone.now(),
        payload={"campaign_id": camp_a.id, "lead_id": binding_setup["lead"].id},
    )

    with patch("openoutreach.linkedin.browser.connect_with_note.send_connection_request_with_note") as mock_send:
        handle_connect(task, session, {})
        assert mock_send.call_count == 0


@pytest.mark.django_db
def test_connect_campaign_profile_mismatch_blocks_write(binding_setup):
    camp_a = binding_setup["camp_a"]  # bound to prof_a
    prof_b = binding_setup["prof_b"]  # mismatched profile
    session = _make_mock_session(prof_b, camp_a)

    task = Task.objects.create(
        task_type="connect",
        scheduled_at=timezone.now(),
        payload={"campaign_id": camp_a.id, "lead_id": binding_setup["lead"].id},
    )

    with patch("openoutreach.linkedin.browser.connect_with_note.send_connection_request_with_note") as mock_send:
        handle_connect(task, session, {})
        assert mock_send.call_count == 0
        assert ProviderWriteIntent.objects.count() == 0


@pytest.mark.django_db
def test_connect_deal_campaign_profile_mismatch_blocks_write(binding_setup):
    camp_b = binding_setup["camp_b"]  # bound to prof_b
    prof_b = binding_setup["prof_b"]
    deal_a = binding_setup["deal_a"]  # deal_a belongs to camp_a (prof_a)
    session = _make_mock_session(prof_b, camp_b)

    task = Task.objects.create(
        task_type="connect",
        scheduled_at=timezone.now(),
        payload={"campaign_id": camp_b.id, "lead_id": deal_a.lead.id},
    )

    with patch("openoutreach.linkedin.browser.connect_with_note.send_connection_request_with_note") as mock_send:
        handle_connect(task, session, {})
        assert mock_send.call_count == 0


@pytest.mark.django_db
def test_follow_up_campaign_profile_mismatch_blocks_write(binding_setup):
    camp_a = binding_setup["camp_a"]  # bound to prof_a
    prof_b = binding_setup["prof_b"]  # mismatched profile
    deal_a = binding_setup["deal_a"]
    deal_a.state = DealState.CONNECTED
    deal_a.save()

    session = _make_mock_session(prof_b, camp_a)
    task = Task.objects.create(
        task_type="follow_up",
        scheduled_at=timezone.now(),
        payload={"campaign_id": camp_a.id},
    )

    with patch("linkedin_cli.actions.message.send_raw_message") as mock_send:
        handle_follow_up(task, session, {})
        assert mock_send.call_count == 0
        assert ProviderWriteIntent.objects.count() == 0


@pytest.mark.django_db
def test_follow_up_historical_conversation_profile_mismatch_blocks_write(binding_setup):
    camp_a = binding_setup["camp_a"]
    prof_a = binding_setup["prof_a"]
    prof_b = binding_setup["prof_b"]
    deal_a = binding_setup["deal_a"]
    deal_a.state = DealState.CONNECTED
    deal_a.save()

    # Record historical action under prof_a
    ActionLog.objects.create(
        linkedin_profile=prof_a,
        campaign=camp_a,
        action_type=ActionLog.ActionType.CONNECT,
        metadata={"lead_public_id": deal_a.lead.public_identifier},
    )

    # Change campaign profile to prof_b
    camp_a.linkedin_profile = prof_b
    camp_a.save()

    session = _make_mock_session(prof_b, camp_a)
    task = Task.objects.create(
        task_type="follow_up",
        scheduled_at=timezone.now(),
        payload={"campaign_id": camp_a.id},
    )

    with patch("linkedin_cli.actions.message.send_raw_message") as mock_send:
        handle_follow_up(task, session, {})
        assert mock_send.call_count == 0


@pytest.mark.django_db
def test_relationship_profile_mismatch_blocks_first_message(binding_setup):
    ws = binding_setup["workspace"]
    prof_a = binding_setup["prof_a"]
    prof_b = binding_setup["prof_b"]
    camp_b = binding_setup["camp_b"]

    lead_b = Lead.objects.create(
        workspace=ws,
        public_identifier="prospect-lead-2",
        first_name="John",
        last_name="Smith",
        linkedin_url="https://www.linkedin.com/in/prospect-lead-2",
    )
    deal_b = Deal.objects.create(lead=lead_b, campaign=camp_b, state=DealState.CONNECTED)

    # Relationship CONNECTED exists on prof_a ONLY
    ProviderRelationship.objects.create(
        workspace=ws,
        linkedin_profile=prof_a,
        lead=lead_b,
        classification=ProviderRelationship.Classification.CONNECTED,
        evidence_sufficient=True,
    )

    # Evaluate external action gates for prof_b: MUST FAIL because prof_b does not have CONNECTED relationship
    allowed, reason = evaluate_external_action_gates(
        workspace=ws,
        campaign=camp_b,
        linkedin_profile=prof_b,
        lead=lead_b,
        deal=deal_b,
        action_type="first_message",
    )
    assert not allowed
    assert "relationship" in reason or "not_connected" in reason or "unconfirmed" in reason

    # Outbound content style validation for prof_b must also fail
    style_res = validate_linkedin_outbound_message(
        message_text="Hi John, great connecting!",
        content_type="first_message",
        workspace=ws,
        linkedin_profile=prof_b,
        deal=deal_b,
    )
    assert not style_res["allowed"]
    assert "RELATIONSHIP" in style_res["reason"] or "NOT_CONNECTED" in style_res["errors"][0]


@pytest.mark.django_db
def test_operator_approval_profile_mismatch_blocks_write_and_preserves_approval(binding_setup):
    ws = binding_setup["workspace"]
    user_a = binding_setup["user_a"]
    prof_a = binding_setup["prof_a"]
    prof_b = binding_setup["prof_b"]
    camp_b = binding_setup["camp_b"]

    lead_b = Lead.objects.create(
        workspace=ws,
        public_identifier="prospect-lead-3",
        first_name="Alice",
        last_name="Wonder",
        linkedin_url="https://www.linkedin.com/in/prospect-lead-3",
    )
    deal_b = Deal.objects.create(lead=lead_b, campaign=camp_b, state=DealState.CONNECTED)

    # Master automation paused to require approval
    binding_setup["auto_settings"].master_automation_enabled = False
    binding_setup["auto_settings"].save()

    # OperatorApproval created specifically for prof_a
    approval = OperatorActionApproval.objects.create(
        workspace=ws,
        operator=user_a,
        linkedin_profile=prof_a,
        lead=lead_b,
        deal=deal_b,
        action_type="follow_up",
        status="approved",
    )

    # Gate check evaluating for prof_b MUST FAIL with approval mismatch
    allowed, reason = evaluate_external_action_gates(
        workspace=ws,
        campaign=camp_b,
        linkedin_profile=prof_b,
        lead=lead_b,
        deal=deal_b,
        action_type="follow_up",
        operator_approval=approval,
    )
    assert not allowed
    assert approval.status == "approved"  # Not claimed or consumed by wrong profile


@pytest.mark.django_db
def test_operator_approval_matching_profile_claims_and_authorizes(binding_setup):
    ws = binding_setup["workspace"]
    user_a = binding_setup["user_a"]
    prof_a = binding_setup["prof_a"]
    lead = binding_setup["lead"]
    camp_a = binding_setup["camp_a"]
    deal_a = binding_setup["deal_a"]
    deal_a.state = DealState.CONNECTED
    deal_a.save()

    # Create matching relationship for prof_a
    ProviderRelationship.objects.create(
        workspace=ws,
        linkedin_profile=prof_a,
        lead=lead,
        classification=ProviderRelationship.Classification.CONNECTED,
        evidence_sufficient=True,
    )

    approval = OperatorActionApproval.objects.create(
        workspace=ws,
        operator=user_a,
        linkedin_profile=prof_a,
        lead=lead,
        deal=deal_a,
        action_type="follow_up",
        status="approved",
    )

    # Gate check for matching prof_a MUST SUCCEED
    allowed, reason = evaluate_external_action_gates(
        workspace=ws,
        campaign=camp_a,
        linkedin_profile=prof_a,
        lead=lead,
        deal=deal_a,
        action_type="follow_up",
        operator_approval=approval,
    )
    assert allowed
    assert reason == "authorized"


@pytest.mark.django_db
def test_operator_approval_message_hash_mismatch_blocks(binding_setup):
    ws = binding_setup["workspace"]
    user_a = binding_setup["user_a"]
    prof_a = binding_setup["prof_a"]
    lead = binding_setup["lead"]
    deal_a = binding_setup["deal_a"]

    expected_msg = "Hello Jane, saw your work on distributed systems."
    expected_hash = hashlib.sha256(expected_msg.strip().encode("utf-8")).hexdigest()

    approval = OperatorActionApproval.objects.create(
        workspace=ws,
        operator=user_a,
        linkedin_profile=prof_a,
        lead=lead,
        deal=deal_a,
        message_hash=expected_hash,
        action_type="follow_up",
        status="approved",
    )

    # Valid with matching text
    assert approval.is_valid_for(
        workspace=ws,
        linkedin_profile=prof_a,
        lead=lead,
        deal=deal_a,
        message_text=expected_msg,
        action_type="follow_up",
    )

    # Invalid with tampered text
    assert not approval.is_valid_for(
        workspace=ws,
        linkedin_profile=prof_a,
        lead=lead,
        deal=deal_a,
        message_text="Tampered text",
        action_type="follow_up",
    )


@pytest.mark.django_db
def test_governor_is_account_safe_for_provider_write_requires_valid_profile(binding_setup):
    ws = binding_setup["workspace"]
    prof_a = binding_setup["prof_a"]

    # None profile must fail closed
    is_safe, reason = is_account_safe_for_provider_write(
        workspace=ws,
        linkedin_profile=None,
        action_type="connect",
    )
    assert not is_safe
    assert reason == "missing_provider_account_fail_closed"

    # Valid active profile succeeds
    is_safe, reason = is_account_safe_for_provider_write(
        workspace=ws,
        linkedin_profile=prof_a,
        action_type="connect",
    )
    assert is_safe
    assert reason == "ok"


@pytest.mark.django_db
def test_governor_profile_mismatch_with_campaign_fails_closed(binding_setup):
    ws = binding_setup["workspace"]
    prof_b = binding_setup["prof_b"]
    camp_a = binding_setup["camp_a"]  # bound to prof_a

    is_safe, reason = is_account_safe_for_provider_write(
        workspace=ws,
        linkedin_profile=prof_b,
        campaign=camp_a,
        action_type="connect",
    )
    assert not is_safe
    assert reason == "campaign_linkedin_profile_mismatch"


@pytest.mark.django_db
def test_governor_checkpointed_or_held_profile_fails_closed(binding_setup):
    ws = binding_setup["workspace"]
    prof_a = binding_setup["prof_a"]

    # 1. Session checkpoint status
    prof_a.session_status = "checkpoint"
    prof_a.save()
    is_safe, reason = is_account_safe_for_provider_write(
        workspace=ws,
        linkedin_profile=prof_a,
        action_type="connect",
    )
    assert not is_safe
    assert reason == "provider_session_status_checkpoint"

    # Restore status
    prof_a.session_status = "valid"
    prof_a.save()

    # 2. Incident hold on profile
    from django.contrib.contenttypes.models import ContentType
    ct = ContentType.objects.get_for_model(LinkedInProfile)
    hold = IncidentHold.objects.create(reason="ACCOUNT_AUDIT", active=True)
    IncidentHoldTarget.objects.create(incident_hold=hold, content_type=ct, object_id=prof_a.id)

    is_safe, reason = is_account_safe_for_provider_write(
        workspace=ws,
        linkedin_profile=prof_a,
        action_type="connect",
    )
    assert not is_safe
    assert reason == "incident_hold_active_on_provider_account"


@pytest.mark.django_db
def test_account_provider_lock_leases_exact_profile(binding_setup):
    ws = binding_setup["workspace"]
    prof_a = binding_setup["prof_a"]
    prof_b = binding_setup["prof_b"]

    with account_provider_lock(linkedin_profile=prof_a, workspace=ws, operation_name="write_op") as locked_prof:
        assert locked_prof == prof_a
        assert ProviderAccountLease.objects.filter(linkedin_profile=prof_a).exists()

        # Concurrent lock attempt for prof_a must fail / yield None
        with account_provider_lock(linkedin_profile=prof_a, workspace=ws, operation_name="concurrent_write") as locked_again:
            assert locked_again is None

        # Lock attempt for prof_b in same workspace MUST SUCCEED independently
        with account_provider_lock(linkedin_profile=prof_b, workspace=ws, operation_name="write_b") as locked_b:
            assert locked_b == prof_b
            assert ProviderAccountLease.objects.filter(linkedin_profile=prof_b).exists()

    # Leases released after context exit
    assert not ProviderAccountLease.objects.filter(linkedin_profile=prof_a).exists()
    assert not ProviderAccountLease.objects.filter(linkedin_profile=prof_b).exists()


@pytest.mark.django_db
def test_write_intent_idempotency_key_strictly_profile_scoped(binding_setup):
    prof_a = binding_setup["prof_a"]
    prof_b = binding_setup["prof_b"]

    key_a = generate_write_idempotency_key(
        linkedin_profile_id=prof_a.id,
        operation_type=ProviderWriteIntent.OperationType.FIRST_MESSAGE_SEND,
        target_identity="prospect-1",
        logical_action_identity="deal_10_first_msg",
    )

    key_b = generate_write_idempotency_key(
        linkedin_profile_id=prof_b.id,
        operation_type=ProviderWriteIntent.OperationType.FIRST_MESSAGE_SEND,
        target_identity="prospect-1",
        logical_action_identity="deal_10_first_msg",
    )

    assert key_a != key_b


@pytest.mark.django_db
def test_write_intent_cross_profile_mismatch_blocks(binding_setup):
    ws = binding_setup["workspace"]
    prof_a = binding_setup["prof_a"]
    prof_b = binding_setup["prof_b"]

    # Claim intent under prof_a with a custom key
    custom_key = "custom_shared_idempotency_key_123"
    allowed_a, reason_a, intent_a = claim_provider_write(
        workspace=ws,
        linkedin_profile=prof_a,
        operation_type=ProviderWriteIntent.OperationType.CONNECTION_SEND,
        target_identity="prospect-1",
        custom_idempotency_key=custom_key,
    )
    assert allowed_a
    assert intent_a.linkedin_profile == prof_a

    # Attempting to claim the same custom key with prof_b must be BLOCKED with profile mismatch
    allowed_b, reason_b, intent_b = claim_provider_write(
        workspace=ws,
        linkedin_profile=prof_b,
        operation_type=ProviderWriteIntent.OperationType.CONNECTION_SEND,
        target_identity="prospect-1",
        custom_idempotency_key=custom_key,
    )
    assert not allowed_b
    assert reason_b == "provider_profile_intent_mismatch"
