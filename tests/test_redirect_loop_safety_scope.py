# tests/test_redirect_loop_safety_scope.py
import uuid
import pytest
from unittest.mock import MagicMock, patch
from django.utils import timezone
from django.contrib.auth import get_user_model

from openoutreach.core.models import (
    Workspace,
    WorkspaceAutomationSettings,
    Campaign,
    OperatorActionApproval,
)
from openoutreach.crm.models import Deal, DealState, Lead, SuppressionRecord
from openoutreach.linkedin.models import (
    LinkedInProfile,
    ProviderWriteIntent,
    ConnectionAttempt,
    ProviderReadLedger,
    ProviderOperationType,
)
from openoutreach.linkedin.services.account_governor import (
    is_account_safe_for_provider_read,
    is_account_safe_for_provider_write,
)

User = get_user_model()


@pytest.fixture
def safety_scope_setup(db):
    uid = uuid.uuid4().hex[:8]
    user = User.objects.create_user(username=f"user_{uid}")
    ws = Workspace.objects.create(name=f"WS_{uid}", slug=f"ws-{uid}")
    auto_st = WorkspaceAutomationSettings.get_for_workspace(ws)
    auto_st.master_automation_enabled = True
    auto_st.auto_find_prospects = True
    auto_st.auto_send_connections = True
    auto_st.auto_followup = True
    auto_st.auto_find_jobs = True
    auto_st.auto_apply_jobs = True
    auto_st.auto_send_emails = True
    auto_st.auto_monitor_replies = True
    auto_st.soak_status = "ACTIVE"
    auto_st.soak_stop_reason = ""
    auto_st.provider_safety_state = "NORMAL"
    auto_st.save()

    profile = LinkedInProfile.objects.create(
        user=user,
        workspace=ws,
        linkedin_username=f"tester_{uid}",
        active=True,
        session_status="valid",
        identity_validation_status="VALID_IDENTITY_MATCH",
    )
    campaign = Campaign.objects.create(
        name=f"Campaign_{uid}",
        workspace=ws,
        linkedin_profile=profile,
        is_active=True,
        linkedin_outreach_enabled=True,
    )
    campaign.users.add(user)

    lead = Lead.objects.create(
        workspace=ws,
        first_name="Andy",
        last_name="Lojas",
        public_identifier="andy-lojas",
        linkedin_url="https://www.linkedin.com/in/andy-lojas/",
    )
    deal = Deal.objects.create(
        lead=lead,
        campaign=campaign,
        state=DealState.READY_TO_CONNECT,
    )
    approval = OperatorActionApproval.objects.create(
        workspace=ws,
        operator=user,
        linkedin_profile=profile,
        lead=lead,
        deal=deal,
        action_type="connect",
        status="approved",
    )

    return {
        "user": user,
        "workspace": ws,
        "auto_st": auto_st,
        "profile": profile,
        "campaign": campaign,
        "lead": lead,
        "deal": deal,
        "approval": approval,
    }


def test_1_target_redirect_loop_fails_closed_on_target_before_write(safety_scope_setup):
    """Test 1: Target profile navigation returns redirect loop before write boundary.
    Expected: target/action blocked, no provider write, no retry, all 8 automation preferences remain ON,
    account session remains valid."""
    ws = safety_scope_setup["workspace"]
    auto_st = safety_scope_setup["auto_st"]
    prof = safety_scope_setup["profile"]
    lead = safety_scope_setup["lead"]
    deal = safety_scope_setup["deal"]
    approval = safety_scope_setup["approval"]

    # Verify initial clean state
    assert prof.session_status == "valid"
    assert auto_st.provider_safety_state == "NORMAL"
    assert auto_st.soak_status == "ACTIVE"
    assert auto_st.all_eight_switches_on()

    # Simulate target profile redirect loop hold
    auto_st.soak_status = "SAFETY_STOP"
    auto_st.soak_stop_reason = "provider_target_redirect_loop"
    auto_st.save()

    # Reconcile target safety hold
    ok, msg = auto_st.reconcile_target_scoped_safety_hold(deal=deal, lead=lead, approval=approval)
    assert ok is True
    assert msg == "target_scoped_safety_hold_reconciled"

    # Deal must be FAILED, not qualified or pending
    deal.refresh_from_db()
    assert deal.state == DealState.FAILED
    assert "provider_target_redirect_loop" in deal.reason

    # Lead must be suppressed
    supp = SuppressionRecord.objects.filter(workspace=ws, linkedin_url=lead.linkedin_url).first()
    assert supp is not None
    assert "provider_target_redirect_loop" in supp.reason

    # Approval must be revoked
    approval.refresh_from_db()
    assert approval.status == "revoked"

    # Zero writes occurred
    assert ProviderWriteIntent.objects.filter(workspace=ws, target_identity=lead.public_identifier).count() == 0
    assert ConnectionAttempt.objects.filter(recipient_public_id=lead.public_identifier).count() == 0

    # Account preferences and session remain valid & ON
    auto_st.refresh_from_db()
    prof.refresh_from_db()
    assert auto_st.all_eight_switches_on()
    assert prof.session_status == "valid"
    assert auto_st.soak_status == "ACTIVE"
    assert auto_st.soak_stop_reason == ""


def test_2_session_auth_redirect_loop_triggers_account_safety_hold(safety_scope_setup):
    """Test 2: session-health/feed navigation itself returns redirect loop.
    Expected: account-level provider_auth_redirect_loop safety hold, provider work blocked."""
    ws = safety_scope_setup["workspace"]
    auto_st = safety_scope_setup["auto_st"]
    prof = safety_scope_setup["profile"]

    # Session health check encounters redirect loop on /feed
    changed = auto_st.recover_from_session_health("REDIRECT_LOOP", profile=prof)
    assert changed is True

    auto_st.refresh_from_db()
    assert auto_st.soak_status == "SAFETY_STOP"
    assert auto_st.soak_stop_reason == "provider_auth_redirect_loop"

    # Provider writes are now blocked by safety stop
    safe_write, reason = is_account_safe_for_provider_write(workspace=ws, linkedin_profile=prof)
    assert safe_write is False
    assert any(sub in reason for sub in ["checkpoint_required", "provider_auth_redirect_loop"])


def test_3_auth_redirect_loop_recovers_canonically_when_session_healthy(safety_scope_setup):
    """Test 3: provider_auth_redirect_loop then later genuine HEALTHY authentication.
    Expected: account-level auth redirect hold can recover canonically."""
    ws = safety_scope_setup["workspace"]
    auto_st = safety_scope_setup["auto_st"]
    prof = safety_scope_setup["profile"]

    # Put in auth redirect loop
    auto_st.soak_status = "SAFETY_STOP"
    auto_st.soak_stop_reason = "provider_auth_redirect_loop"
    auto_st.save()

    # Session health later returns HEALTHY
    changed = auto_st.recover_from_session_health("HEALTHY", profile=prof)
    assert changed is True

    auto_st.refresh_from_db()
    assert auto_st.soak_status == "ACTIVE"
    assert auto_st.soak_stop_reason == ""
    assert auto_st.provider_safety_state == "NORMAL"
    assert auto_st.all_eight_switches_on()


def test_4_target_redirect_loop_not_erased_by_healthy_session(safety_scope_setup):
    """Test 4: provider_target_redirect_loop then later HEALTHY session health.
    Expected: target remains blocked, target failure is NOT erased merely by HEALTHY account auth,
    and provider_target_redirect_loop is NOT in RECOVERABLE_SESSION_STOP_REASONS."""
    auto_st = safety_scope_setup["auto_st"]
    prof = safety_scope_setup["profile"]
    deal = safety_scope_setup["deal"]

    # Mark deal failed from target redirect loop
    deal.state = DealState.FAILED
    deal.reason = "provider_target_redirect_loop"
    deal.save()

    # Workspace in target redirect stop
    auto_st.soak_status = "SAFETY_STOP"
    auto_st.soak_stop_reason = "provider_target_redirect_loop"
    auto_st.save()

    # Generic provider_redirect_loop and provider_target_redirect_loop must NOT be in RECOVERABLE_SESSION_STOP_REASONS
    assert "provider_redirect_loop" not in auto_st.RECOVERABLE_SESSION_STOP_REASONS
    assert "provider_target_redirect_loop" not in auto_st.RECOVERABLE_SESSION_STOP_REASONS
    assert "provider_auth_redirect_loop" in auto_st.RECOVERABLE_SESSION_STOP_REASONS

    # Session health check returns HEALTHY
    changed = auto_st.recover_from_session_health("HEALTHY", profile=prof)
    # recover_from_session_health must NOT clear provider_target_redirect_loop!
    assert changed is False
    auto_st.refresh_from_db()
    assert auto_st.soak_status == "SAFETY_STOP"
    assert auto_st.soak_stop_reason == "provider_target_redirect_loop"

    # Deal remains FAILED
    deal.refresh_from_db()
    assert deal.state == DealState.FAILED


def test_5_unrelated_safety_holds_preserved_against_healthy_session(safety_scope_setup):
    """Test 5: CAPTCHA/restriction/high-volume/manual hold expected:
    unrelated HEALTHY auth does not clear them."""
    auto_st = safety_scope_setup["auto_st"]
    prof = safety_scope_setup["profile"]

    for unrecoverable in ["provider_restriction", "high_volume_data_access_warning", "manual_operator_incident_hold"]:
        auto_st.soak_status = "SAFETY_STOP"
        auto_st.soak_stop_reason = unrecoverable
        if "restrict" in unrecoverable:
            auto_st.provider_safety_state = "ACCOUNT_RESTRICTED"
        elif "high_volume" in unrecoverable:
            auto_st.provider_safety_state = "HIGH_VOLUME_DATA_ACCESS_WARNING"
        auto_st.save()

        changed = auto_st.recover_from_session_health("HEALTHY", profile=prof)
        assert changed is False
        auto_st.refresh_from_db()
        assert auto_st.soak_status == "SAFETY_STOP"
        assert auto_st.soak_stop_reason == unrecoverable


def test_6_historical_current_hold_reconciliation_fixture(safety_scope_setup):
    """Test 6: Exact historical current hold migration fixture:
    Profile #2 HEALTHY, Workspace SAFETY_STOP, reason provider_redirect_loop,
    failure source target profile navigation, transport_invoked False,
    Lead #169, Deal #157, Approval #19."""
    ws = safety_scope_setup["workspace"]
    auto_st = safety_scope_setup["auto_st"]
    prof = safety_scope_setup["profile"]
    lead = safety_scope_setup["lead"]
    deal = safety_scope_setup["deal"]
    approval = safety_scope_setup["approval"]

    # Historical state
    prof.session_status = "valid"
    prof.save()
    auto_st.soak_status = "SAFETY_STOP"
    auto_st.soak_stop_reason = "provider_redirect_loop"
    auto_st.provider_safety_state = "NORMAL"
    auto_st.save()

    # Pre-checks
    read_safe, _ = is_account_safe_for_provider_read(workspace=ws, linkedin_profile=prof)
    assert read_safe is False  # Blocked by SAFETY_STOP

    # Reconcile canonically
    ok, msg = auto_st.reconcile_target_scoped_safety_hold(deal=deal, lead=lead, approval=approval)
    assert ok is True
    assert msg == "target_scoped_safety_hold_reconciled"

    # Post-checks
    auto_st.refresh_from_db()
    assert auto_st.soak_status == "ACTIVE"
    assert auto_st.soak_stop_reason == ""
    assert auto_st.provider_safety_state == "NORMAL"
    assert auto_st.all_eight_switches_on()

    deal.refresh_from_db()
    assert deal.state == DealState.FAILED

    approval.refresh_from_db()
    assert approval.status == "revoked"

    supp = SuppressionRecord.objects.filter(workspace=ws, linkedin_url=lead.linkedin_url).first()
    assert supp is not None

    # Governor is now ELIGIBLE for unrelated targets
    read_safe, read_reason = is_account_safe_for_provider_read(workspace=ws, linkedin_profile=prof)
    write_safe, write_reason = is_account_safe_for_provider_write(workspace=ws, linkedin_profile=prof)
    assert read_safe is True
    assert write_safe is True
