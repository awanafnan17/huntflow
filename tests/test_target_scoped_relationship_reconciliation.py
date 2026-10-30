# tests/test_target_scoped_relationship_reconciliation.py
"""
Unit and regression tests for target-scoped LinkedIn relationship reconciliation.

Tests that:
1. Unrelated Connect/Pending controls (e.g. for Muhammad Mashhood or other sidebar profiles)
   are strictly ignored and do not corrupt target classification.
2. Positive 1st-degree evidence requires explicit API degree 1 or target-scoped DOM degree badge.
3. Target-scoped Connect produces NOT_CONNECTED.
4. Target-scoped Pending produces PENDING.
5. Message-only buttons and absence of evidence fail closed to UNKNOWN.
6. Relationship gates block first-message actions on non-CONNECTED states.
"""
import pytest
from unittest.mock import MagicMock
from openoutreach.linkedin.services.relationship_reconciliation import (
    RelationshipClassification,
    RelationshipReconciliationResult,
    classify_target_relationship_from_dom_data,
    extract_and_classify_page_relationship,
    is_aria_matching_target,
)
from openoutreach.crm.models import Deal, DealState, Lead
from openoutreach.core.models import Campaign, Workspace, User
from openoutreach.core.services.external_action_gates import evaluate_external_action_gates
from openoutreach.core.content_style import validate_linkedin_outbound_message


# ── 1. Unrelated Connect Button Regression (Section 17) ─────────────────────

def test_unrelated_connect_button_regression_zisis_unknown():
    """
    CRITICAL REGRESSION: Section 17
    Page target: Zisis Petrou (zisispetrou)
    Target profile top card: no degree, no Pending, no Connect
    Elsewhere on page: button aria-label: 'Invite Muhammad Mashhood . to connect'
    API degree: None
    Expected: UNKNOWN (NOT: NOT_CONNECTED)
    """
    target_name = "Zisis Petrou"
    target_id = "zisispetrou"

    top_card_buttons = []
    page_buttons = [
        {"text": "Connect", "aria": "Invite Muhammad Mashhood . to connect"},
        {"text": "More", "aria": "More actions"},
    ]

    result = classify_target_relationship_from_dom_data(
        target_name=target_name,
        target_public_identifier=target_id,
        api_degree=None,
        top_card_degree_text=None,
        top_card_buttons=top_card_buttons,
        page_buttons=page_buttons,
    )

    assert result.classification == RelationshipClassification.UNKNOWN
    assert result.evidence_sufficient is False
    assert any("Muhammad Mashhood" in b.get("aria", "") for b in result.unrelated_controls_ignored)


# ── 2. Target Connect Test (Section 18) ─────────────────────────────────────

def test_target_connect_produces_not_connected():
    """
    Section 18: Target profile top card contains Connect button scoped to Zisis.
    API degree: None.
    Expected: NOT_CONNECTED.
    """
    target_name = "Zisis Petrou"
    target_id = "zisispetrou"

    top_card_buttons = [
        {"text": "Connect", "aria": "Invite Zisis to connect"}
    ]

    result = classify_target_relationship_from_dom_data(
        target_name=target_name,
        target_public_identifier=target_id,
        api_degree=None,
        top_card_degree_text=None,
        top_card_buttons=top_card_buttons,
    )

    assert result.classification == RelationshipClassification.NOT_CONNECTED
    assert result.evidence_sufficient is True
    assert "Connect" in result.evidence


# ── 3. Target Pending Test (Section 19) ─────────────────────────────────────

def test_target_pending_produces_pending():
    """
    Section 19: Target top card contains Pending button for Zisis.
    Expected: PENDING.
    """
    target_name = "Zisis Petrou"
    target_id = "zisispetrou"

    top_card_buttons = [
        {"text": "Pending", "aria": "Pending invitation sent to Zisis Petrou"}
    ]

    result = classify_target_relationship_from_dom_data(
        target_name=target_name,
        target_public_identifier=target_id,
        api_degree=None,
        top_card_degree_text=None,
        top_card_buttons=top_card_buttons,
    )

    assert result.classification == RelationshipClassification.PENDING
    assert result.evidence_sufficient is True
    assert "Pending" in result.evidence


# ── 4. API Degree 1 Test (Section 20) ───────────────────────────────────────

def test_api_degree_1_produces_connected_regardless_of_page_buttons():
    """
    Section 20: Provider API reports connection_degree=1.
    Expected: CONNECTED, even if unrelated recommendation buttons exist on the page.
    """
    target_name = "Zisis Petrou"
    target_id = "zisispetrou"

    page_buttons = [
        {"text": "Connect", "aria": "Invite Muhammad Mashhood . to connect"},
        {"text": "Connect", "aria": "Invite Jane Doe to connect"},
    ]

    result = classify_target_relationship_from_dom_data(
        target_name=target_name,
        target_public_identifier=target_id,
        api_degree=1,
        top_card_degree_text=None,
        top_card_buttons=[],
        page_buttons=page_buttons,
    )

    assert result.classification == RelationshipClassification.CONNECTED
    assert result.evidence_sufficient is True
    assert result.observed_degree == 1


# ── 5. Non-First Degree API Tests (Section 21) ──────────────────────────────

def test_non_first_degree_api_semantics():
    """
    Section 21: Provider API positively reports connection_degree=2 or 3.
    If no pending invitation exists, this definitively establishes NOT_CONNECTED.
    If a target pending button is visible, PENDING is preserved.
    """
    target_name = "Zisis Petrou"
    target_id = "zisispetrou"

    # Case A: API degree 2, no pending -> NOT_CONNECTED
    res_2 = classify_target_relationship_from_dom_data(
        target_name=target_name,
        target_public_identifier=target_id,
        api_degree=2,
        top_card_buttons=[{"text": "Connect", "aria": "Invite Zisis to connect"}],
    )
    assert res_2.classification == RelationshipClassification.NOT_CONNECTED

    # Case B: API degree 3, no buttons -> NOT_CONNECTED
    res_3 = classify_target_relationship_from_dom_data(
        target_name=target_name,
        target_public_identifier=target_id,
        api_degree=3,
        top_card_buttons=[],
    )
    assert res_3.classification == RelationshipClassification.NOT_CONNECTED

    # Case C: API degree 2 + target pending button -> PENDING
    res_pending = classify_target_relationship_from_dom_data(
        target_name=target_name,
        target_public_identifier=target_id,
        api_degree=2,
        top_card_buttons=[{"text": "Pending", "aria": "Withdraw invitation"}],
    )
    assert res_pending.classification == RelationshipClassification.PENDING


# ── 6. Empty Evidence Test (Section 22) ─────────────────────────────────────

def test_empty_evidence_fails_closed_to_unknown():
    """
    Section 22: API degree is None, target top card has no relationship controls.
    Expected: UNKNOWN.
    """
    result = classify_target_relationship_from_dom_data(
        target_name="Zisis Petrou",
        target_public_identifier="zisispetrou",
        api_degree=None,
        top_card_degree_text=None,
        top_card_buttons=[],
        page_buttons=[],
    )
    assert result.classification == RelationshipClassification.UNKNOWN
    assert result.evidence_sufficient is False


# ── 7. Message Button Alone Test (Section 23) ───────────────────────────────

def test_message_button_alone_fails_closed_to_unknown():
    """
    Section 23: Target top card contains Message button, but API degree is None
    and no explicit 1st-degree badge is present.
    Expected: UNKNOWN (Message button alone could indicate InMail or Open Profile).
    """
    result = classify_target_relationship_from_dom_data(
        target_name="Zisis Petrou",
        target_public_identifier="zisispetrou",
        api_degree=None,
        top_card_degree_text=None,
        top_card_buttons=[{"text": "Message", "aria": "Message Zisis"}],
    )
    assert result.classification == RelationshipClassification.UNKNOWN
    assert result.evidence_sufficient is False


# ── 8. Unrelated Pending Test (Section 24) ──────────────────────────────────

def test_unrelated_pending_button_ignored_unknown():
    """
    Section 24: Target Zisis top card has no relationship controls,
    while a sidebar/recommendation element has a Pending button for another user.
    Expected: UNKNOWN (unrelated pending control ignored).
    """
    result = classify_target_relationship_from_dom_data(
        target_name="Zisis Petrou",
        target_public_identifier="zisispetrou",
        api_degree=None,
        top_card_degree_text=None,
        top_card_buttons=[],
        page_buttons=[
            {"text": "Pending", "aria": "Withdraw invitation sent to Sarah Connor"},
        ],
    )
    assert result.classification == RelationshipClassification.UNKNOWN
    assert result.evidence_sufficient is False
    assert len(result.unrelated_controls_ignored) > 0


# ── 9. Identity Validation Tests (Section 25) ───────────────────────────────

def test_is_aria_matching_target_unit():
    """
    Section 25: Verify aria-label entity extraction and target matching.
    """
    # Exact / partial target match
    is_valid, is_unrelated = is_aria_matching_target("Invite Zisis to connect", "Zisis Petrou", "zisispetrou")
    assert is_valid is True
    assert is_unrelated is False

    is_valid, is_unrelated = is_aria_matching_target("Invite Zisis Petrou to connect", "Zisis Petrou", "zisispetrou")
    assert is_valid is True
    assert is_unrelated is False

    # Unrelated individual
    is_valid, is_unrelated = is_aria_matching_target("Invite Muhammad Mashhood . to connect", "Zisis Petrou", "zisispetrou")
    assert is_valid is False
    assert is_unrelated is True

    is_valid, is_unrelated = is_aria_matching_target("Connect with Jane Doe", "Zisis Petrou", "zisispetrou")
    assert is_valid is False
    assert is_unrelated is True

    # Generic button without name
    is_valid, is_unrelated = is_aria_matching_target("Connect", "Zisis Petrou", "zisispetrou")
    assert is_valid is True
    assert is_unrelated is False


# ── 10. Security Signals Fail Closed ────────────────────────────────────────

def test_security_signals_classification():
    """
    Section 14 & 27: Verify provider security signals immediately yield SAFETY_BLOCKED or AUTH_REQUIRED.
    """
    for sig in ("CHECKPOINT", "CHALLENGE", "CAPTCHA", "RESTRICTION", "HIGH_VOLUME_WARNING (commercial use limit)"):
        res = classify_target_relationship_from_dom_data(
            target_name="Zisis Petrou",
            target_public_identifier="zisispetrou",
            security_signal=sig,
        )
        assert res.classification == RelationshipClassification.SAFETY_BLOCKED

    for auth_sig in ("AUTH_REQUIRED", "LOGIN", "AUTHWALL", "SESSION_EXPIRED"):
        res = classify_target_relationship_from_dom_data(
            target_name="Zisis Petrou",
            target_public_identifier="zisispetrou",
            security_signal=auth_sig,
        )
        assert res.classification == RelationshipClassification.AUTH_REQUIRED


# ── 11. Relationship Gate Blocks on Non-Connected / UNKNOWN ─────────────────

@pytest.mark.django_db
def test_first_message_blocked_when_relationship_unknown_and_pending():
    """
    Verify that when local deal is Pending and provider relationship is UNKNOWN,
    first message is strictly blocked by external action gates and outbound message validator.
    """
    user = User.objects.create_user(username="test_rel_user", password="pwd")
    ws = Workspace.objects.create(name="Test Rel Workspace", slug="test-rel-ws")
    campaign = Campaign.objects.create(name="Job Outreach", workspace=ws)
    lead = Lead.objects.create(
        first_name="Zisis",
        last_name="Petrou",
        public_identifier="zisispetrou",
        linkedin_url="https://www.linkedin.com/in/zisispetrou/",
    )
    deal = Deal.objects.create(
        lead=lead,
        campaign=campaign,
        state=DealState.PENDING,
    )

    valid_draft = "hey zisis, saw the ml + engineering role at pinterest... curious what you're focused on rn?"

    gate_ok, gate_reason = evaluate_external_action_gates(
        workspace=ws,
        campaign=campaign,
        lead=lead,
        deal=deal,
        action_type="first_message",
    )
    assert gate_ok is False
    assert gate_reason == "deal_not_connected_cannot_first_message"

    out_res = validate_linkedin_outbound_message(
        message_text=valid_draft,
        content_type="first_message",
        workspace=ws,
        deal=deal,
    )
    assert out_res["allowed"] is False
    assert out_res["reason"] == "MESSAGE_BLOCKED_RELATIONSHIP"
