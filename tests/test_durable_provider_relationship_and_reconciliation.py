# tests/test_durable_provider_relationship_and_reconciliation.py
"""
Comprehensive tests for durable ProviderRelationship state, multi-account provider isolation,
progressive UNKNOWN cooldown/backoff, first-message gating, read governor integration,
and generic future reconciliation.
"""
from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
from django.utils import timezone

from openoutreach.core.content_style import validate_linkedin_outbound_message
from openoutreach.core.models import (
    Campaign,
    OperatorActionApproval,
    User,
    Workspace,
    WorkspaceAutomationSettings,
)
from openoutreach.core.scheduler import plan_check_pending_window
from openoutreach.core.services.external_action_gates import evaluate_external_action_gates
from openoutreach.crm.models import Deal, DealState, Lead
from openoutreach.linkedin.models import (
    LinkedInProfile,
    ProviderOperationType,
    ProviderReadLedger,
    ProviderRelationship,
)
from openoutreach.linkedin.services.evidence_cache import (
    get_cached_evidence,
    record_provider_read,
    set_cached_evidence,
)
from openoutreach.linkedin.services.relationship_reconciliation import (
    CONNECTED_EVIDENCE_TTL_DAYS,
    RelationshipClassification,
    RelationshipReconciliationResult,
    calculate_unknown_cooldown,
    evaluate_relationship_for_first_message,
    get_or_create_provider_relationship,
    reconcile_target_relationship,
    record_relationship_observation,
)
from openoutreach.linkedin.tasks.check_pending import _next_due_pending_deal


@pytest.fixture
def setup_tenancy(db):
    user_a = User.objects.create_user(username="user_a", password="pwd")
    ws_a = Workspace.objects.create(name="Workspace A", slug="ws-a")
    auto_a = WorkspaceAutomationSettings.objects.create(
        workspace=ws_a,
        master_automation_enabled=False,
    )
    prof_a = LinkedInProfile.objects.create(
        user=user_a,
        workspace=ws_a,
        linkedin_username="sender_a1@example.com",
        session_status="valid",
        active=True,
    )
    camp_a = Campaign.objects.create(
        name="Campaign A",
        workspace=ws_a,
        linkedin_profile=prof_a,
        is_active=True,
    )

    # Second provider account in same Workspace A
    user_a2 = User.objects.create_user(username="user_a2", password="pwd")
    prof_a2 = LinkedInProfile.objects.create(
        user=user_a2,
        workspace=ws_a,
        linkedin_username="sender_a2@example.com",
        session_status="valid",
        active=True,
    )

    user_b = User.objects.create_user(username="user_b", password="pwd")
    ws_b = Workspace.objects.create(name="Workspace B", slug="ws-b")
    auto_b = WorkspaceAutomationSettings.objects.create(
        workspace=ws_b,
        master_automation_enabled=False,
    )
    prof_b = LinkedInProfile.objects.create(
        user=user_b,
        workspace=ws_b,
        linkedin_username="sender_b@example.com",
        session_status="valid",
        active=True,
    )
    camp_b = Campaign.objects.create(
        name="Campaign B",
        workspace=ws_b,
        linkedin_profile=prof_b,
        is_active=True,
    )

    lead_a = Lead.objects.create(
        first_name="Zisis",
        last_name="Petrou",
        public_identifier="zisispetrou",
        linkedin_url="https://www.linkedin.com/in/zisispetrou/",
        workspace=ws_a,
    )
    deal_a = Deal.objects.create(
        lead=lead_a,
        campaign=camp_a,
        state=DealState.PENDING,
    )

    return {
        "ws_a": ws_a,
        "prof_a": prof_a,
        "prof_a2": prof_a2,
        "camp_a": camp_a,
        "lead_a": lead_a,
        "deal_a": deal_a,
        "ws_b": ws_b,
        "prof_b": prof_b,
        "camp_b": camp_b,
    }


# ── 1. UNKNOWN Cooldown & Progressive Backoff ───────────────────────────────

def test_unknown_cooldown_calculation():
    """Verify progressive cooldown: 1st >= 72h, 2nd >= 7d, 3rd requires operator review."""
    cd1, review1 = calculate_unknown_cooldown(1)
    assert cd1 >= timedelta(hours=72)
    assert review1 is False

    cd2, review2 = calculate_unknown_cooldown(2)
    assert cd2 >= timedelta(days=7)
    assert review2 is False

    cd3, review3 = calculate_unknown_cooldown(3)
    assert review3 is True

    cd4, review4 = calculate_unknown_cooldown(4)
    assert review4 is True


@pytest.mark.django_db
def test_progressive_unknown_observation_lifecycle(setup_tenancy):
    """
    Simulate consecutive UNKNOWN observations and verify durable state,
    progressive backoff stamps, and operator review requirement.
    """
    ctx = setup_tenancy
    lead = ctx["lead_a"]
    ws = ctx["ws_a"]
    prof = ctx["prof_a"]
    deal = ctx["deal_a"]

    unknown_res = RelationshipReconciliationResult(
        classification=RelationshipClassification.UNKNOWN,
        evidence="No target-scoped controls",
        evidence_sufficient=False,
    )

    # First UNKNOWN
    t0 = timezone.now()
    rel1 = record_relationship_observation(
        lead=lead,
        result=unknown_res,
        workspace=ws,
        linkedin_profile=prof,
        deal=deal,
    )
    assert rel1.classification == "UNKNOWN"
    assert rel1.consecutive_unknown_count == 1
    assert rel1.operator_review_required is False
    assert rel1.next_eligible_reconciliation_at >= t0 + timedelta(hours=71)
    deal.refresh_from_db()
    assert deal.next_check_pending_at == rel1.next_eligible_reconciliation_at

    # Second UNKNOWN
    rel2 = record_relationship_observation(
        lead=lead,
        result=unknown_res,
        workspace=ws,
        linkedin_profile=prof,
        deal=deal,
    )
    assert rel2.consecutive_unknown_count == 2
    assert rel2.operator_review_required is False
    assert rel2.next_eligible_reconciliation_at >= t0 + timedelta(days=6)

    # Third UNKNOWN -> operator review required
    rel3 = record_relationship_observation(
        lead=lead,
        result=unknown_res,
        workspace=ws,
        linkedin_profile=prof,
        deal=deal,
    )
    assert rel3.consecutive_unknown_count == 3
    assert rel3.operator_review_required is True


# ── 2. Multi-Account Provider Relationship Isolation (Same Workspace, Same Lead) ──

@pytest.mark.django_db
def test_same_workspace_two_provider_profiles_persistence_and_isolation(setup_tenancy):
    """
    REQUIRED Section 11:
    Workspace A, Lead X, Profile A1 (CONNECTED), Profile A2 (UNKNOWN).
    Both rows coexist simultaneously (count == 2).
    A1 passes first-message gate.
    A2 is blocked.
    No cross-account leakage.
    """
    ctx = setup_tenancy
    ws = ctx["ws_a"]
    prof_a1 = ctx["prof_a"]
    prof_a2 = ctx["prof_a2"]
    lead = ctx["lead_a"]
    deal = ctx["deal_a"]
    deal.state = DealState.CONNECTED
    deal.save(update_fields=["state"])

    # Create Profile A1 relationship: CONNECTED, sufficient, fresh
    rel_a1 = record_relationship_observation(
        lead=lead,
        result=RelationshipReconciliationResult(
            classification=RelationshipClassification.CONNECTED,
            observed_degree=1,
            evidence="Target Message control present for Profile A1",
            evidence_sufficient=True,
        ),
        workspace=ws,
        linkedin_profile=prof_a1,
        deal=deal,
    )

    # Create Profile A2 relationship: UNKNOWN, insufficient
    rel_a2 = record_relationship_observation(
        lead=lead,
        result=RelationshipReconciliationResult(
            classification=RelationshipClassification.UNKNOWN,
            evidence="No controls present for Profile A2",
            evidence_sufficient=False,
        ),
        workspace=ws,
        linkedin_profile=prof_a2,
        deal=deal,
    )

    # Assert both rows coexist in the DB
    assert ProviderRelationship.objects.filter(workspace=ws, lead=lead).count() == 2
    assert rel_a1.id != rel_a2.id
    assert rel_a1.classification == "CONNECTED"
    assert rel_a2.classification == "UNKNOWN"

    # Profile A1 first-message check -> PASS
    ok_a1, reason_a1 = evaluate_relationship_for_first_message(
        lead=lead, deal=deal, workspace=ws, linkedin_profile=prof_a1
    )
    assert ok_a1 is True
    assert reason_a1 == "OK"

    # Profile A2 first-message check -> BLOCKED
    ok_a2, reason_a2 = evaluate_relationship_for_first_message(
        lead=lead, deal=deal, workspace=ws, linkedin_profile=prof_a2
    )
    assert ok_a2 is False
    assert "unknown" in reason_a2


@pytest.mark.django_db
def test_reverse_state_multi_provider_isolation(setup_tenancy):
    """
    REQUIRED Section 12: Reverse State Test.
    Same Workspace A, Same Lead X:
    Profile A1: UNKNOWN
    Profile A2: CONNECTED (fresh, sufficient).
    A1 -> BLOCKED
    A2 -> PASS.
    Proves no arbitrary .first() fallback picking the wrong profile.
    """
    ctx = setup_tenancy
    ws = ctx["ws_a"]
    prof_a1 = ctx["prof_a"]
    prof_a2 = ctx["prof_a2"]
    lead = ctx["lead_a"]
    deal = ctx["deal_a"]
    deal.state = DealState.CONNECTED
    deal.save(update_fields=["state"])

    # A1: UNKNOWN
    record_relationship_observation(
        lead=lead,
        result=RelationshipReconciliationResult(
            classification=RelationshipClassification.UNKNOWN,
            evidence="No controls for A1",
            evidence_sufficient=False,
        ),
        workspace=ws,
        linkedin_profile=prof_a1,
        deal=deal,
    )

    # A2: CONNECTED
    record_relationship_observation(
        lead=lead,
        result=RelationshipReconciliationResult(
            classification=RelationshipClassification.CONNECTED,
            observed_degree=1,
            evidence="1st degree for A2",
            evidence_sufficient=True,
        ),
        workspace=ws,
        linkedin_profile=prof_a2,
        deal=deal,
    )

    assert ProviderRelationship.objects.filter(workspace=ws, lead=lead).count() == 2

    # A1 must be BLOCKED
    ok_a1, reason_a1 = evaluate_relationship_for_first_message(
        lead=lead, deal=deal, workspace=ws, linkedin_profile=prof_a1
    )
    assert ok_a1 is False
    assert "unknown" in reason_a1

    # A2 must PASS
    ok_a2, reason_a2 = evaluate_relationship_for_first_message(
        lead=lead, deal=deal, workspace=ws, linkedin_profile=prof_a2
    )
    assert ok_a2 is True
    assert reason_a2 == "OK"


# ── 3. Cooldown and Operator Review are Provider-Specific ──────────────────

@pytest.mark.django_db
def test_provider_specific_cooldown_and_operator_review_isolation(setup_tenancy):
    """
    REQUIRED Sections 13 & 14:
    Profile A1 has 3 consecutive UNKNOWNs (cooldown active, operator_review_required=True).
    Profile A2 has fresh CONNECTED state (no operator review required).
    A1's cooldown and review state must NOT affect A2.
    """
    ctx = setup_tenancy
    ws = ctx["ws_a"]
    prof_a1 = ctx["prof_a"]
    prof_a2 = ctx["prof_a2"]
    lead = ctx["lead_a"]
    deal = ctx["deal_a"]

    # Put A1 on 3rd UNKNOWN
    for _ in range(3):
        record_relationship_observation(
            lead=lead,
            result=RelationshipReconciliationResult(
                classification=RelationshipClassification.UNKNOWN,
                evidence="Unknown for A1",
                evidence_sufficient=False,
            ),
            workspace=ws,
            linkedin_profile=prof_a1,
            deal=deal,
        )

    # Observe A2 as CONNECTED
    record_relationship_observation(
        lead=lead,
        result=RelationshipReconciliationResult(
            classification=RelationshipClassification.CONNECTED,
            observed_degree=1,
            evidence="1st degree for A2",
            evidence_sufficient=True,
        ),
        workspace=ws,
        linkedin_profile=prof_a2,
        deal=deal,
    )

    rel_a1 = ProviderRelationship.objects.get(workspace=ws, linkedin_profile=prof_a1, lead=lead)
    rel_a2 = ProviderRelationship.objects.get(workspace=ws, linkedin_profile=prof_a2, lead=lead)

    assert rel_a1.operator_review_required is True
    assert rel_a1.consecutive_unknown_count == 3

    assert rel_a2.operator_review_required is False
    assert rel_a2.consecutive_unknown_count == 0
    assert rel_a2.classification == "CONNECTED"


@pytest.mark.django_db
def test_reconciliation_observation_does_not_mutate_other_profile(setup_tenancy):
    """
    REQUIRED Section 15:
    Calling record_relationship_observation for Profile A2 does not mutate Profile A1's
    classification, cooldown, consecutive unknowns, or last ledger.
    """
    ctx = setup_tenancy
    ws = ctx["ws_a"]
    prof_a1 = ctx["prof_a"]
    prof_a2 = ctx["prof_a2"]
    lead = ctx["lead_a"]
    deal = ctx["deal_a"]

    # Initial A1 state
    ledger_a1 = record_provider_read(
        operation_type=ProviderOperationType.PROFILE_DETAIL,
        resource_identifier=lead.public_identifier,
        linkedin_profile=prof_a1,
        workspace=ws,
        success=True,
    )
    rel_a1_initial = record_relationship_observation(
        lead=lead,
        result=RelationshipReconciliationResult(
            classification=RelationshipClassification.UNKNOWN,
            evidence="A1 initial read",
            evidence_sufficient=False,
        ),
        workspace=ws,
        linkedin_profile=prof_a1,
        deal=deal,
        ledger=ledger_a1,
    )
    a1_cooldown = rel_a1_initial.next_eligible_reconciliation_at
    a1_ledger_id = rel_a1_initial.last_read_ledger_id

    # Now mutate A2 multiple times
    for i in range(2):
        record_relationship_observation(
            lead=lead,
            result=RelationshipReconciliationResult(
                classification=RelationshipClassification.PENDING,
                evidence=f"A2 observation {i}",
                evidence_sufficient=True,
            ),
            workspace=ws,
            linkedin_profile=prof_a2,
            deal=deal,
        )

    rel_a1_refreshed = ProviderRelationship.objects.get(workspace=ws, linkedin_profile=prof_a1, lead=lead)
    assert rel_a1_refreshed.classification == "UNKNOWN"
    assert rel_a1_refreshed.consecutive_unknown_count == 1
    assert rel_a1_refreshed.next_eligible_reconciliation_at == a1_cooldown
    assert rel_a1_refreshed.last_read_ledger_id == a1_ledger_id


# ── 4. Provider Read Ledger & Tenancy Mismatches Fail Closed ────────────────

@pytest.mark.django_db
def test_mismatched_read_ledger_rejected(setup_tenancy):
    """
    REQUIRED Section 16:
    If a ProviderReadLedger belonging to Profile A is supplied when recording an observation
    for Profile B, it must fail closed and raise ValueError.
    """
    ctx = setup_tenancy
    ws = ctx["ws_a"]
    prof_a1 = ctx["prof_a"]
    prof_a2 = ctx["prof_a2"]
    lead = ctx["lead_a"]
    deal = ctx["deal_a"]

    # Ledger created for Profile A1
    ledger_a1 = record_provider_read(
        operation_type=ProviderOperationType.PROFILE_DETAIL,
        resource_identifier=lead.public_identifier,
        linkedin_profile=prof_a1,
        workspace=ws,
        success=True,
    )

    # Attempt to record observation for Profile A2 using Profile A1's ledger
    with pytest.raises(ValueError) as exc_info:
        record_relationship_observation(
            lead=lead,
            result=RelationshipReconciliationResult(
                classification=RelationshipClassification.UNKNOWN,
                evidence="Mismatch test",
                evidence_sufficient=False,
            ),
            workspace=ws,
            linkedin_profile=prof_a2,
            deal=deal,
            ledger=ledger_a1,
        )
    assert "mismatch" in str(exc_info.value).lower()


@pytest.mark.django_db
def test_cross_tenant_and_missing_profile_fails_closed(setup_tenancy):
    """
    REQUIRED Section 17 & 6:
    - Cross-tenant profile: Profile B (from Workspace B) passed with Workspace A raises ValueError.
    - Missing profile: None passed raises ValueError.
    """
    ctx = setup_tenancy
    ws_a = ctx["ws_a"]
    prof_b = ctx["prof_b"]  # Belongs to ws_b
    lead_a = ctx["lead_a"]

    # Cross-tenant profile rejected
    with pytest.raises(ValueError) as exc_info:
        get_or_create_provider_relationship(
            workspace=ws_a,
            lead=lead_a,
            linkedin_profile=prof_b,
        )
    assert "does not belong to workspace" in str(exc_info.value).lower()

    # Missing profile rejected
    with pytest.raises(ValueError) as exc_info2:
        get_or_create_provider_relationship(
            workspace=ws_a,
            lead=lead_a,
            linkedin_profile=None,
        )
    assert "explicit linkedinprofile is required" in str(exc_info2.value).lower()


# ── 5. Provider-Scoped Evidence Cache ───────────────────────────────────────

@pytest.mark.django_db
def test_evidence_cache_is_strictly_provider_account_scoped(setup_tenancy):
    """
    REQUIRED Section 24:
    ProviderEvidenceCache for connection_state must NOT return cached evidence across profiles.
    """
    ctx = setup_tenancy
    prof_a1 = ctx["prof_a"]
    prof_a2 = ctx["prof_a2"]
    lead = ctx["lead_a"]

    # Set connection_state cache specifically for Profile A1
    set_cached_evidence(
        resource_type="connection_state",
        resource_identifier=lead.public_identifier,
        data={"status": "CONNECTED", "degree": 1, "evidence": "Cached for A1"},
        ttl=timedelta(days=7),
        linkedin_profile=prof_a1,
    )

    # Lookup for Profile A1 -> returns cached data
    cached_a1 = get_cached_evidence(
        resource_type="connection_state",
        resource_identifier=lead.public_identifier,
        linkedin_profile=prof_a1,
    )
    assert cached_a1 is not None
    assert cached_a1["evidence"] == "Cached for A1"

    # Lookup for Profile A2 -> returns None (isolated!)
    cached_a2 = get_cached_evidence(
        resource_type="connection_state",
        resource_identifier=lead.public_identifier,
        linkedin_profile=prof_a2,
    )
    assert cached_a2 is None

    # Lookup without profile -> returns None (fail closed)
    cached_none = get_cached_evidence(
        resource_type="connection_state",
        resource_identifier=lead.public_identifier,
        linkedin_profile=None,
    )
    assert cached_none is None


# ── 6. First-Message Gate & External Action Gates Strict Profile Scope ──────

@pytest.mark.django_db
def test_external_action_gate_checks_exact_sending_profile(setup_tenancy):
    """
    REQUIRED Sections 8, 9, 10:
    - evaluate_external_action_gates with linkedin_profile=A1 passes when A1 is CONNECTED.
    - evaluate_external_action_gates with linkedin_profile=A2 blocks when A2 is UNKNOWN.
    - evaluate_external_action_gates without linkedin_profile blocks with missing_provider_relationship_context.
    - validate_linkedin_outbound_message scopes to exact profile.
    """
    ctx = setup_tenancy
    ws = ctx["ws_a"]
    prof_a1 = ctx["prof_a"]
    prof_a2 = ctx["prof_a2"]
    camp = ctx["camp_a"]
    lead = ctx["lead_a"]
    deal = ctx["deal_a"]
    deal.state = DealState.CONNECTED
    deal.save(update_fields=["state"])

    auto_settings = WorkspaceAutomationSettings.objects.get(workspace=ws)
    auto_settings.master_automation_enabled = True
    auto_settings.auto_followup = True
    auto_settings.save()
    ws.automation_settings = auto_settings

    # Profile A1 CONNECTED
    record_relationship_observation(
        lead=lead,
        result=RelationshipReconciliationResult(
            classification=RelationshipClassification.CONNECTED,
            observed_degree=1,
            evidence="Connected A1",
            evidence_sufficient=True,
        ),
        workspace=ws,
        linkedin_profile=prof_a1,
        deal=deal,
    )

    # Profile A2 UNKNOWN
    record_relationship_observation(
        lead=lead,
        result=RelationshipReconciliationResult(
            classification=RelationshipClassification.UNKNOWN,
            evidence="Unknown A2",
            evidence_sufficient=False,
        ),
        workspace=ws,
        linkedin_profile=prof_a2,
        deal=deal,
    )

    # 1. Gate check for sending Profile A1 -> PASS
    ok_a1, reason_a1 = evaluate_external_action_gates(
        workspace=ws,
        campaign=camp,
        linkedin_profile=prof_a1,
        lead=lead,
        deal=deal,
        action_type="first_message",
    )
    assert ok_a1 is True, f"Failed with reason: {reason_a1}"
    assert reason_a1 == "authorized"

    # 2. Gate check for sending Profile A2 -> BLOCKED
    ok_a2, reason_a2 = evaluate_external_action_gates(
        workspace=ws,
        campaign=camp,
        linkedin_profile=prof_a2,
        lead=lead,
        deal=deal,
        action_type="first_message",
    )
    assert ok_a2 is False
    assert "provider_relationship_unknown" in reason_a2

    # 3. Gate check with no resolvable profile -> BLOCKED missing context
    lead_unbound = Lead.objects.create(first_name="Unbound", last_name="Lead", public_identifier="unboundlead", linkedin_url="https://www.linkedin.com/in/unboundlead/", workspace=ws)
    camp_unbound = Campaign.objects.create(name="Camp Unbound", workspace=ws, is_active=True, linkedin_profile=None)
    deal_unbound = Deal.objects.create(lead=lead_unbound, campaign=camp_unbound, state=DealState.CONNECTED)
    ok_no_prof, reason_no_prof = evaluate_external_action_gates(
        workspace=ws,
        campaign=camp_unbound,
        linkedin_profile=None,
        lead=lead_unbound,
        deal=deal_unbound,
        action_type="first_message",
    )
    assert ok_no_prof is False
    assert "missing_provider_relationship_context" in reason_no_prof

    # 4. validate_linkedin_outbound_message check
    valid_draft = "hey zisis, saw what you're building at Pinterest... curious what you're focused on rn?"
    out_a1 = validate_linkedin_outbound_message(
        message_text=valid_draft,
        content_type="first_message",
        workspace=ws,
        linkedin_profile=prof_a1,
        deal=deal,
    )
    assert out_a1["allowed"] is True

    out_a2 = validate_linkedin_outbound_message(
        message_text=valid_draft,
        content_type="first_message",
        workspace=ws,
        linkedin_profile=prof_a2,
        deal=deal,
    )
    assert out_a2["allowed"] is False
    assert out_a2["reason"] == "MESSAGE_BLOCKED_RELATIONSHIP"


# ── 7. Approval & Deal State Cannot Bypass Wrong-Profile Relationship ───────

@pytest.mark.django_db
def test_old_approval_cannot_override_wrong_profile_relationship(setup_tenancy):
    """
    Verify that an OperatorActionApproval does NOT authorize first message
    when the sending profile's ProviderRelationship is UNKNOWN.
    """
    ctx = setup_tenancy
    ws = ctx["ws_a"]
    prof_a1 = ctx["prof_a"]
    camp = ctx["camp_a"]
    lead = ctx["lead_a"]
    deal = ctx["deal_a"]
    deal.state = DealState.CONNECTED
    deal.save(update_fields=["state"])

    # ProviderRelationship for Profile A1 is UNKNOWN
    record_relationship_observation(
        lead=lead,
        result=RelationshipReconciliationResult(
            classification=RelationshipClassification.UNKNOWN,
            evidence="No target-scoped controls",
            evidence_sufficient=False,
        ),
        workspace=ws,
        linkedin_profile=prof_a1,
        deal=deal,
    )

    # Operator approves action
    approval = OperatorActionApproval.objects.create(
        workspace=ws,
        operator=prof_a1.user,
        action_type="first_message",
        status="approved",
    )

    ok, reason = evaluate_external_action_gates(
        workspace=ws,
        campaign=camp,
        linkedin_profile=prof_a1,
        lead=lead,
        deal=deal,
        action_type="first_message",
        operator_approval=approval,
    )
    assert ok is False
    assert "provider_relationship_unknown" in reason


# ── 8. PENDING, NOT_CONNECTED, and Freshness ────────────────────────────────

@pytest.mark.django_db
def test_pending_and_not_connected_behavior(setup_tenancy):
    """Verify PENDING and NOT_CONNECTED states block first message."""
    ctx = setup_tenancy
    lead = ctx["lead_a"]
    ws = ctx["ws_a"]
    prof = ctx["prof_a"]
    deal = ctx["deal_a"]
    deal.state = DealState.CONNECTED
    deal.save(update_fields=["state"])

    # 1. PENDING
    record_relationship_observation(
        lead=lead,
        result=RelationshipReconciliationResult(
            classification=RelationshipClassification.PENDING,
            evidence="Pending invitation visible",
            evidence_sufficient=True,
        ),
        workspace=ws,
        linkedin_profile=prof,
        deal=deal,
    )
    ok, reason = evaluate_relationship_for_first_message(
        lead=lead, deal=deal, workspace=ws, linkedin_profile=prof
    )
    assert ok is False
    assert "pending" in reason

    # 2. NOT_CONNECTED
    record_relationship_observation(
        lead=lead,
        result=RelationshipReconciliationResult(
            classification=RelationshipClassification.NOT_CONNECTED,
            evidence="Connect button visible",
            evidence_sufficient=True,
        ),
        workspace=ws,
        linkedin_profile=prof,
        deal=deal,
    )
    ok, reason = evaluate_relationship_for_first_message(
        lead=lead, deal=deal, workspace=ws, linkedin_profile=prof
    )
    assert ok is False
    assert "not_connected" in reason


@pytest.mark.django_db
def test_connected_relationship_freshness_and_sufficiency(setup_tenancy):
    """Verify CONNECTED sufficiency and 7-day TTL freshness."""
    ctx = setup_tenancy
    lead = ctx["lead_a"]
    ws = ctx["ws_a"]
    prof = ctx["prof_a"]
    deal = ctx["deal_a"]
    deal.state = DealState.CONNECTED
    deal.save(update_fields=["state"])

    # A. Insufficient evidence CONNECTED
    rel = get_or_create_provider_relationship(workspace=ws, lead=lead, linkedin_profile=prof, deal=deal)
    rel.classification = "CONNECTED"
    rel.evidence_sufficient = False
    rel.last_observed_at = timezone.now()
    rel.save()

    ok, reason = evaluate_relationship_for_first_message(
        lead=lead, deal=deal, workspace=ws, linkedin_profile=prof
    )
    assert ok is False
    assert reason == "insufficient_relationship_evidence"

    # B. Stale evidence CONNECTED (> 7 days)
    rel.evidence_sufficient = True
    rel.last_observed_at = timezone.now() - timedelta(days=8)
    rel.save()

    ok, reason = evaluate_relationship_for_first_message(
        lead=lead, deal=deal, workspace=ws, linkedin_profile=prof
    )
    assert ok is False
    assert reason == "stale_relationship_evidence"

    # C. Fresh sufficient evidence CONNECTED (<= 7 days)
    rel.last_observed_at = timezone.now() - timedelta(hours=2)
    rel.save()

    ok, reason = evaluate_relationship_for_first_message(
        lead=lead, deal=deal, workspace=ws, linkedin_profile=prof
    )
    assert ok is True
    assert reason == "OK"


# ── 9. Daemon Scheduler Hold & Multi-Tenant Isolation ───────────────────────

@pytest.mark.django_db
def test_daemon_does_not_schedule_during_unknown_cooldown(setup_tenancy):
    """Prove that a Deal in Pending with ProviderRelationship UNKNOWN does NOT produce slots."""
    ctx = setup_tenancy
    lead = ctx["lead_a"]
    ws = ctx["ws_a"]
    prof = ctx["prof_a"]
    camp = ctx["camp_a"]
    deal = ctx["deal_a"]

    record_relationship_observation(
        lead=lead,
        result=RelationshipReconciliationResult(
            classification=RelationshipClassification.UNKNOWN,
            evidence="No target-scoped controls",
            evidence_sufficient=False,
        ),
        workspace=ws,
        linkedin_profile=prof,
        deal=deal,
    )

    mock_session = MagicMock()
    mock_session.active_timezone = "UTC"
    mock_session.linkedin_profile = prof
    slots_planned = plan_check_pending_window(mock_session, camp)
    assert slots_planned == 0

    due_deal = _next_due_pending_deal(camp, profile=prof)
    assert due_deal is None


@pytest.mark.django_db
def test_reconciliation_tenant_and_account_isolation(setup_tenancy):
    """Verify that reconcile_target_relationship strictly isolates workspaces and provider accounts."""
    ctx = setup_tenancy
    ws_a = ctx["ws_a"]
    prof_a = ctx["prof_a"]
    lead_a = ctx["lead_a"]
    ws_b = ctx["ws_b"]
    prof_b = ctx["prof_b"]

    res = reconcile_target_relationship(
        workspace_id=ws_b.id,
        linkedin_profile_id=prof_b.id,
        lead_id=lead_a.id,
    )
    assert res["success"] is False
    assert res["error"] == "lead_workspace_mismatch"

    res2 = reconcile_target_relationship(
        workspace_id=ws_a.id,
        linkedin_profile_id=prof_b.id,
        lead_id=lead_a.id,
    )
    assert res2["success"] is False
    assert res2["error"] == "unauthorized_or_inactive_provider_profile"
