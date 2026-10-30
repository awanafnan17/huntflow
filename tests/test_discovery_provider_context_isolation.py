# tests/test_discovery_provider_context_isolation.py
"""
Test Suite for Strict Discovery Provider Context Isolation and Fail-Closed Binding.

Sections 25-30:
1. Missing campaign provider fails closed (0 provider calls, no fallback guessing).
2. Same-workspace multi-profile without explicit binding fails closed.
3. Explicit campaign provider is used exactly and recorded in ProviderReadLedger.
4. Cross-workspace profile mismatch fails closed.
5. Account safety governor evaluates the exact bound account (no substitute fallback).
"""
import pytest
from unittest.mock import MagicMock, patch
from django.contrib.auth.models import User

from openoutreach.core.models import Workspace, Campaign
from openoutreach.crm.models.lead import Lead
from openoutreach.crm.models.deal import Deal
from openoutreach.linkedin.models import (
    LinkedInProfile,
    ProviderReadLedger,
    ProviderOperationType,
    ProviderSafetyEvent,
)
from openoutreach.linkedin.services.discovery_service import discover_campaign_targets


@pytest.fixture
def multi_tenant_discovery_env(db):
    user_a = User.objects.create_user(username="user_a", email="a@example.com")
    user_b = User.objects.create_user(username="user_b", email="b@example.com")

    ws_a = Workspace.objects.create(name="Workspace A", slug="ws-a")
    ws_b = Workspace.objects.create(name="Workspace B", slug="ws-b")

    prof_a1 = LinkedInProfile.objects.create(
        user=user_a,
        workspace=ws_a,
        linkedin_username="prof_a1@example.com",
        session_status="valid",
        active=True,
    )
    prof_a2 = LinkedInProfile.objects.create(
        user=user_a,
        workspace=ws_a,
        linkedin_username="prof_a2@example.com",
        session_status="valid",
        active=True,
    )
    prof_b1 = LinkedInProfile.objects.create(
        user=user_b,
        workspace=ws_b,
        linkedin_username="prof_b1@example.com",
        session_status="valid",
        active=True,
    )

    campaign_a = Campaign.objects.create(
        workspace=ws_a,
        name="Campaign A",
        is_active=True,
        linkedin_outreach_enabled=True,
    )

    return {
        "ws_a": ws_a,
        "ws_b": ws_b,
        "prof_a1": prof_a1,
        "prof_a2": prof_a2,
        "prof_b1": prof_b1,
        "campaign_a": campaign_a,
    }


@pytest.mark.django_db
def test_missing_campaign_provider_fails_closed(multi_tenant_discovery_env):
    """Section 25: Campaign with linkedin_profile=None must block without guessing A1 or B1."""
    env = multi_tenant_discovery_env
    campaign = env["campaign_a"]
    campaign.linkedin_profile = None
    campaign.save()

    mock_search = MagicMock()
    with patch("linkedin_cli.actions.search.search_people", mock_search):
        res = discover_campaign_targets(campaign)

    assert res["status"] == "blocked"
    assert res["reason"] == "missing_campaign_linkedin_profile"
    assert mock_search.call_count == 0
    assert ProviderReadLedger.objects.count() == 0


@pytest.mark.django_db
def test_same_workspace_multi_profile_ambiguity_fails_closed(multi_tenant_discovery_env):
    """Section 26: Multiple valid profiles in the same workspace must NOT be chosen via .first()."""
    env = multi_tenant_discovery_env
    campaign = env["campaign_a"]
    campaign.linkedin_profile = None
    campaign.save()

    # Both A1 and A2 are active/valid in ws_a
    assert env["prof_a1"].session_status == "valid"
    assert env["prof_a2"].session_status == "valid"

    mock_search = MagicMock()
    with patch("linkedin_cli.actions.search.search_people", mock_search):
        res = discover_campaign_targets(campaign)

    assert res["status"] == "blocked"
    assert res["reason"] == "missing_campaign_linkedin_profile"
    assert mock_search.call_count == 0


@pytest.mark.django_db
def test_explicit_campaign_provider_used_exactly(multi_tenant_discovery_env):
    """Section 27: Explicitly bound profile A2 is used, and ledger attributes to A2 (A1 unused)."""
    env = multi_tenant_discovery_env
    campaign = env["campaign_a"]
    campaign.linkedin_profile = env["prof_a2"]
    campaign.save()

    search_cards = [
        {
            "url": "https://www.linkedin.com/in/test-prospect-1",
            "public_identifier": "test-prospect-1",
            "name": "Test Prospect",
            "headline": "Lead Engineer",
            "location": "Toronto, Canada",
        }
    ]

    mock_session = MagicMock()
    mock_session.page = MagicMock()
    mock_session.page.url = "https://www.linkedin.com/feed/"

    with patch("linkedin_cli.actions.search.search_people", return_value={"profiles": search_cards}):
        res = discover_campaign_targets(campaign, limit=10, session=mock_session)

    assert res["status"] == "success"
    ledger = ProviderReadLedger.objects.filter(operation_type=ProviderOperationType.PROSPECT_SEARCH).first()
    assert ledger is not None
    assert ledger.linkedin_profile == env["prof_a2"]
    assert ledger.workspace == env["ws_a"]


@pytest.mark.django_db
def test_cross_workspace_profile_mismatch_fails_closed(multi_tenant_discovery_env):
    """Section 28: Campaign in Workspace A bound to Profile B1 in Workspace B must fail closed."""
    env = multi_tenant_discovery_env
    campaign = env["campaign_a"]
    campaign.linkedin_profile = env["prof_b1"]  # From ws_b
    campaign.save()

    mock_search = MagicMock()
    with patch("linkedin_cli.actions.search.search_people", mock_search):
        res = discover_campaign_targets(campaign)

    assert res["status"] == "blocked"
    assert res["reason"] == "cross_workspace_profile_mismatch"
    assert mock_search.call_count == 0
    assert ProviderReadLedger.objects.count() == 0


@pytest.mark.django_db
def test_governor_evaluates_exact_account_safety(multi_tenant_discovery_env):
    """Section 29: If bound profile A2 has a safety stop, discovery blocks even if A1 is healthy."""
    env = multi_tenant_discovery_env
    campaign = env["campaign_a"]
    prof_a2 = env["prof_a2"]
    prof_a2.session_status = "checkpoint"
    prof_a2.save()
    campaign.linkedin_profile = prof_a2
    campaign.save()

    # A1 remains valid/healthy
    assert env["prof_a1"].session_status == "valid"

    mock_search = MagicMock()
    with patch("linkedin_cli.actions.search.search_people", mock_search):
        res = discover_campaign_targets(campaign)

    assert res["status"] == "blocked"
    assert "checkpoint" in res["reason"].lower() or "invalid_provider_session_status" in res["reason"].lower() or "safety" in res["reason"].lower()
    assert mock_search.call_count == 0
    assert ProviderReadLedger.objects.count() == 0
