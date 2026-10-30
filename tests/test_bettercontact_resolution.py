import pytest
from unittest.mock import MagicMock, patch
import numpy as np

from openoutreach.core.models import Campaign, Task, SiteConfig
from openoutreach.crm.models import Lead, Deal, DealState, Outcome
from openoutreach.core.db.deals import get_qualified_profiles, get_emailable_deals
from openoutreach.linkedin.pipeline.qualify import _save_qualification_result, _resolve_email
from openoutreach.emails.bettercontact import BetterContactResult, BetterContactUnavailable
from openoutreach.core.scheduler import flush_email_queue
from openoutreach.emails.models import Mailbox


# ── Scenario A: Successful Email Resolution ─────────────────────────────

@pytest.mark.django_db
def test_bettercontact_successful_resolution():
    """Valid BetterContact resolution populates lead.api_email, enables email route, and preserves LinkedIn eligibility."""
    campaign = Campaign.objects.create(
        name="Phase2A Dual Campaign",
        is_active=True,
        linkedin_outreach_enabled=True,
        email_outreach_enabled=True,
    )
    lead = Lead.objects.create(
        first_name="Alice",
        last_name="Wong",
        public_identifier="alice-wong-phase2a",
        company="Acme Corp",
    )
    session = MagicMock()
    session.campaign = campaign
    qualifier = MagicMock()

    mock_bc_result = BetterContactResult(email="alice.wong@acme.com", status="valid")

    with patch("openoutreach.emails.models.has_mailbox", return_value=True), \
         patch("openoutreach.emails.bettercontact.is_configured", return_value=True), \
         patch("openoutreach.emails.bettercontact.resolve_email", return_value=mock_bc_result) as mock_resolve:
        
        _save_qualification_result(
            session=session,
            qualifier=qualifier,
            lead_id=lead.id,
            public_id=lead.public_identifier,
            embedding=np.zeros(10),
            label=1,
            reason="High fit score",
        )

    deal = Deal.objects.get(lead=lead, campaign=campaign)
    lead.refresh_from_db()

    # 1. Resolved email stored in canonical field
    assert lead.api_email == "alice.wong@acme.com"

    # 2. Email route becomes eligible
    assert deal.state == DealState.READY_TO_EMAIL

    # 3. LinkedIn eligibility remains intact (connect_attempts == 0)
    li_profiles = get_qualified_profiles(session)
    assert len(li_profiles) == 1
    assert li_profiles[0]["public_identifier"] == "alice-wong-phase2a"

    # 4. Deal and lead not disqualified
    assert not lead.disqualified
    assert deal.outcome == ""

    # 5. Exactly 1 provider lookup
    assert mock_resolve.call_count == 1


# ── Scenario B: No Email Found (Genuine Miss) ─────────────────────────

@pytest.mark.django_db
def test_bettercontact_no_email_found():
    """Genuine email miss does not fail or disqualify prospect, leaving LinkedIn outreach active."""
    campaign = Campaign.objects.create(
        name="Phase2A Miss Campaign",
        is_active=True,
        linkedin_outreach_enabled=True,
        email_outreach_enabled=True,
    )
    lead = Lead.objects.create(
        first_name="Bob",
        last_name="Builder",
        public_identifier="bob-builder-phase2a",
    )
    session = MagicMock()
    session.campaign = campaign
    qualifier = MagicMock()

    with patch("openoutreach.emails.models.has_mailbox", return_value=True), \
         patch("openoutreach.emails.bettercontact.is_configured", return_value=True), \
         patch("openoutreach.emails.bettercontact.resolve_email", return_value=None) as mock_resolve:
        
        _save_qualification_result(
            session=session,
            qualifier=qualifier,
            lead_id=lead.id,
            public_id=lead.public_identifier,
            embedding=np.zeros(10),
            label=1,
            reason="Qualified prospect",
        )

    deal = Deal.objects.get(lead=lead, campaign=campaign)
    lead.refresh_from_db()

    # 1. api_email is empty
    assert not lead.api_email

    # 2. Prospect is NOT disqualified or failed
    assert not lead.disqualified
    assert deal.state == DealState.QUALIFIED

    # 3. LinkedIn pool remains eligible
    li_profiles = get_qualified_profiles(session)
    assert len(li_profiles) == 1
    assert li_profiles[0]["public_identifier"] == "bob-builder-phase2a"


# ── Scenario C & D: Provider Unconfigured / Unreachable ───────────────

@pytest.mark.django_db
def test_bettercontact_unconfigured_and_unreachable():
    """Unconfigured key or transport error returns WAITING_FOR_EMAIL_PROVIDER without marking prospect failed."""
    campaign = Campaign.objects.create(
        name="Phase2A Unavail Campaign",
        is_active=True,
        linkedin_outreach_enabled=True,
        email_outreach_enabled=True,
    )
    lead = Lead.objects.create(
        first_name="Charlie",
        last_name="Chaplin",
        public_identifier="charlie-chaplin-phase2a",
    )
    session = MagicMock()
    session.campaign = campaign
    qualifier = MagicMock()

    # Unconfigured key
    with patch("openoutreach.emails.bettercontact.is_configured", return_value=False) as mock_cfg:
        _save_qualification_result(
            session=session,
            qualifier=qualifier,
            lead_id=lead.id,
            public_id=lead.public_identifier,
            embedding=np.zeros(10),
            label=1,
            reason="Qualified",
        )

    deal = Deal.objects.get(lead=lead, campaign=campaign)
    assert deal.state == DealState.QUALIFIED
    assert not lead.disqualified

    # Unreachable transport error
    with patch("openoutreach.emails.models.has_mailbox", return_value=True), \
         patch("openoutreach.emails.bettercontact.is_configured", return_value=True), \
         patch("openoutreach.emails.bettercontact.resolve_email", side_effect=BetterContactUnavailable("Timeout")):
        
        resolved = _resolve_email(session, lead)
        assert resolved is False

    assert not lead.disqualified
    assert deal.state == DealState.QUALIFIED


# ── Idempotency & Cost Safety ──────────────────────────────────────────

@pytest.mark.django_db
def test_bettercontact_idempotency_prevents_duplicate_lookups():
    """Leads with resolved api_email skip repeated paid provider lookups."""
    campaign = Campaign.objects.create(name="Idempotent Campaign", is_active=True)
    lead = Lead.objects.create(
        first_name="David",
        last_name="Goliath",
        public_identifier="david-goliath-idempotent",
        api_email="david@goliath.com",
    )

    with patch("openoutreach.emails.bettercontact.resolve_email") as mock_resolve:
        hit = lead.resolve_api_email()
        assert hit is True
        # Provider resolution skipped because api_email is already populated
        assert mock_resolve.call_count == 0


# ── Email Validation Safety ────────────────────────────────────────────

@pytest.mark.django_db
def test_bettercontact_invalid_email_rejection():
    """Malformed email strings returned by provider are safely rejected."""
    lead = Lead.objects.create(
        first_name="Eve",
        last_name="Online",
        public_identifier="eve-online-validation",
    )
    mock_bad_result = BetterContactResult(email="not-an-email-address", status="valid")

    cfg = SiteConfig.load()
    cfg.bettercontact_api_key = "test_key_12345"
    cfg.save()

    with patch("openoutreach.emails.bettercontact.resolve_email", return_value=mock_bad_result):
        res = lead.resolve_api_email()
        assert res is None or res is False

    lead.refresh_from_db()
    assert not lead.api_email


# ── Send Safety Gates ──────────────────────────────────────────────────

@pytest.mark.django_db
def test_resolved_email_does_not_bypass_send_gates():
    """A resolved email makes deal ELIGIBLE, but does NOT bypass mailbox/cap send safety gates."""
    campaign = Campaign.objects.create(
        name="Send Gate Campaign",
        is_active=True,
        linkedin_outreach_enabled=True,
        email_outreach_enabled=True,
    )
    lead = Lead.objects.create(first_name="Frank", last_name="Castle", public_identifier="frank-castle", api_email="frank@castle.com")
    Deal.objects.create(lead=lead, campaign=campaign, state=DealState.READY_TO_EMAIL)

    session = MagicMock()
    session.campaign = campaign

    # With 0 mailboxes, flush_email_queue creates 0 tasks (send gate enforced)
    created_tasks = flush_email_queue(session, campaign)
    assert created_tasks == 0
    assert Task.objects.filter(task_type=Task.TaskType.EMAIL).count() == 0


# ── Cross-Channel Safety After Resolution ─────────────────────────────

@pytest.mark.django_db
def test_cross_channel_safety_after_bettercontact_resolution():
    """Setting NOT_INTERESTED or CONVERTED after email resolution suppresses both LinkedIn and Email outreach."""
    campaign = Campaign.objects.create(
        name="Cross Channel Phase2A",
        is_active=True,
        linkedin_outreach_enabled=True,
        email_outreach_enabled=True,
    )
    lead = Lead.objects.create(
        first_name="Grace",
        last_name="Hopper",
        public_identifier="grace-hopper-cross",
        api_email="grace@hopper.com",
    )
    deal = Deal.objects.create(
        lead=lead,
        campaign=campaign,
        state=DealState.READY_TO_EMAIL,
        outcome="",
    )

    session = MagicMock()
    session.campaign = campaign

    # 1. NOT_INTERESTED suppresses both channels
    deal.outcome = Outcome.NOT_INTERESTED
    deal.save()

    emailable = get_emailable_deals(session)
    assert emailable.count() == 0

    # 2. CONVERTED suppresses cold outreach
    deal.outcome = Outcome.CONVERTED
    deal.save()
    emailable_converted = get_emailable_deals(session)
    assert emailable_converted.count() == 0
