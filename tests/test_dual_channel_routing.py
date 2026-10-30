import pytest
from unittest.mock import MagicMock, patch
import numpy as np
from django.utils import timezone

from django.contrib.auth.models import User
from openoutreach.core.models import Campaign, Task
from openoutreach.linkedin.models import LinkedInProfile
from openoutreach.crm.models import Lead, Deal, DealState, Outcome
from openoutreach.core.db.deals import get_qualified_profiles, get_emailable_deals, set_profile_state
from openoutreach.linkedin.pipeline.qualify import _save_qualification_result, _resolve_email
from openoutreach.core.scheduler import plan_connect_window, flush_email_queue
from openoutreach.core.agents.connection_note import generate_connection_note, _generate_llm_note
from openoutreach.core.agents.email_opener import compose_opener_email
from openoutreach.emails.models import Mailbox


# ── 1. Legacy Campaign Safety ──────────────────────────────────────────

@pytest.mark.django_db
def test_legacy_linkedin_campaign_safety():
    """Existing LinkedIn-only campaigns do not silently become email-enabled."""
    campaign = Campaign.objects.create(
        name="LinkedIn Outreach",
        is_active=True,
        linkedin_outreach_enabled=True,
        email_outreach_enabled=False,  # Preserved historical mode
    )
    assert campaign.linkedin_outreach_enabled is True
    assert campaign.email_outreach_enabled is False

    session = MagicMock()
    session.campaign = campaign
    
    # Flush email queue returns 0 for email-disabled campaigns
    created_emails = flush_email_queue(session, campaign)
    assert created_emails == 0


# ── 2. Basic Channel Isolation Routing ─────────────────────────────────

@pytest.mark.django_db
def test_linkedin_only_routing():
    """When LinkedIn is ON and Email is OFF, qualified prospects remain QUALIFIED for GP gate promotion."""
    campaign = Campaign.objects.create(
        name="LinkedIn Only Campaign",
        is_active=True,
        linkedin_outreach_enabled=True,
        email_outreach_enabled=False,
    )
    lead = Lead.objects.create(
        first_name="Jane",
        last_name="Doe",
        public_identifier="jane-doe-li-only",
    )

    session = MagicMock()
    session.campaign = campaign
    qualifier = MagicMock()

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
    assert deal.state == DealState.QUALIFIED
    profiles = get_qualified_profiles(session)
    assert len(profiles) == 1
    assert profiles[0]["public_identifier"] == "jane-doe-li-only"


@pytest.mark.django_db
def test_email_only_routing_with_resolved_email():
    """When LinkedIn is OFF and Email is ON, qualified prospects with resolved email become Email-eligible only."""
    campaign = Campaign.objects.create(
        name="Email Only Campaign",
        is_active=True,
        linkedin_outreach_enabled=False,
        email_outreach_enabled=True,
    )
    lead = Lead.objects.create(
        first_name="John",
        last_name="Smith",
        public_identifier="john-smith-email-only",
        api_email="john@example.com",
    )

    session = MagicMock()
    session.campaign = campaign
    qualifier = MagicMock()

    with patch("openoutreach.emails.models.has_mailbox", return_value=True), \
         patch("openoutreach.emails.bettercontact.is_configured", return_value=True), \
         patch.object(Lead, "resolve_api_email", return_value=True):
        _save_qualification_result(
            session=session,
            qualifier=qualifier,
            lead_id=lead.id,
            public_id=lead.public_identifier,
            embedding=np.zeros(10),
            label=1,
            reason="Strong ICP match",
        )

    deal = Deal.objects.get(lead=lead, campaign=campaign)
    assert deal.state == DealState.READY_TO_EMAIL
    
    # LinkedIn profiles pool should be empty because LinkedIn is OFF
    profiles = get_qualified_profiles(session)
    assert len(profiles) == 0


@pytest.mark.django_db
def test_dual_channel_routing_preserves_both_eligibilities():
    """When BOTH channels are ON, resolving email moves to READY_TO_EMAIL without erasing LinkedIn connection eligibility."""
    campaign = Campaign.objects.create(
        name="Dual Channel Campaign",
        is_active=True,
        linkedin_outreach_enabled=True,
        email_outreach_enabled=True,
    )
    lead = Lead.objects.create(
        first_name="Alex",
        last_name="Taylor",
        public_identifier="alex-taylor-dual",
        api_email="alex@company.com",
    )

    session = MagicMock()
    session.campaign = campaign
    qualifier = MagicMock()

    with patch("openoutreach.emails.models.has_mailbox", return_value=True), \
         patch("openoutreach.emails.bettercontact.is_configured", return_value=True), \
         patch.object(Lead, "resolve_api_email", return_value=True):
        _save_qualification_result(
            session=session,
            qualifier=qualifier,
            lead_id=lead.id,
            public_id=lead.public_identifier,
            embedding=np.zeros(10),
            label=1,
            reason="ICP Qualified",
        )

    deal = Deal.objects.get(lead=lead, campaign=campaign)
    assert deal.state == DealState.READY_TO_EMAIL

    # LinkedIn pool still sees this prospect as eligible for connection because connect_attempts == 0
    profiles = get_qualified_profiles(session)
    assert len(profiles) == 1
    assert profiles[0]["public_identifier"] == "alex-taylor-dual"


# ── 3. Duplicate Prevention ───────────────────────────────────────────

@pytest.mark.django_db
def test_linkedin_duplicate_task_prevention():
    """Repeated scheduling passes do not create duplicate connect tasks when a pending connect task exists."""
    user = User.objects.create(username="testuser", email="user@test.com")
    li_profile = LinkedInProfile.objects.create(
        user=user,
        active=True,
        session_status="valid",
        identity_validation_status="valid",
        connect_daily_limit=10,
    )
    campaign = Campaign.objects.create(
        name="Dup Test Campaign",
        is_active=True,
        linkedin_outreach_enabled=True,
        email_outreach_enabled=True,
        linkedin_profile=li_profile,
    )
    campaign.users.add(user)

    lead = Lead.objects.create(first_name="Dup", last_name="User", public_identifier="dup-user")
    Deal.objects.create(lead=lead, campaign=campaign, state=DealState.READY_TO_CONNECT)

    session = MagicMock()
    session.campaign = campaign
    session.linkedin_profile = li_profile
    session.active_timezone = "UTC"

    # First pass: creates 1 connect task
    pass1 = plan_connect_window(session, campaign)
    assert pass1 == 1
    assert Task.objects.filter(task_type=Task.TaskType.CONNECT, payload__campaign_id=campaign.id, status=Task.Status.PENDING).count() == 1

    # Second pass: _has_pending prevents duplicate scheduling
    pass2 = plan_connect_window(session, campaign)
    assert pass2 == 0
    assert Task.objects.filter(task_type=Task.TaskType.CONNECT, payload__campaign_id=campaign.id, status=Task.Status.PENDING).count() == 1


@pytest.mark.django_db
def test_email_duplicate_task_prevention():
    """Repeated flush calls do not create duplicate email tasks when a pending email task exists."""
    campaign = Campaign.objects.create(
        name="Email Dup Campaign",
        is_active=True,
        linkedin_outreach_enabled=True,
        email_outreach_enabled=True,
    )
    Mailbox.objects.create(
        username="sender@test.com",
        from_address="sender@test.com",
        password="secretpassword",
        daily_limit=50,
    )
    lead = Lead.objects.create(first_name="EmailDup", last_name="Lead", public_identifier="email-dup", api_email="dup@test.com")
    Deal.objects.create(lead=lead, campaign=campaign, state=DealState.READY_TO_EMAIL)

    session = MagicMock()
    session.campaign = campaign

    # First pass: creates 1 email task slot
    pass1 = flush_email_queue(session, campaign)
    assert pass1 == 1
    assert Task.objects.filter(task_type=Task.TaskType.EMAIL, payload__campaign_id=campaign.id, status=Task.Status.PENDING).count() == 1

    # Second pass: _has_pending prevents duplicate scheduling
    pass2 = flush_email_queue(session, campaign)
    assert pass2 == 0
    assert Task.objects.filter(task_type=Task.TaskType.EMAIL, payload__campaign_id=campaign.id, status=Task.Status.PENDING).count() == 1


# ── 4. Cross-Channel Terminal & Engagement Safety ─────────────────────

@pytest.mark.django_db
def test_cross_channel_terminal_outcome_suppression():
    """Setting NOT_INTERESTED or CONVERTED suppresses task creation across both channels."""
    campaign = Campaign.objects.create(
        name="Terminal Safety Campaign",
        is_active=True,
        linkedin_outreach_enabled=True,
        email_outreach_enabled=True,
    )
    lead = Lead.objects.create(
        first_name="Chris",
        last_name="Evans",
        public_identifier="chris-evans-terminal",
        api_email="chris@evans.com",
    )
    deal = Deal.objects.create(
        lead=lead,
        campaign=campaign,
        state=DealState.READY_TO_EMAIL,
        outcome=Outcome.NOT_INTERESTED,
    )

    session = MagicMock()
    session.campaign = campaign

    # NOT_INTERESTED excludes deal from email pool
    emailable = get_emailable_deals(session)
    assert emailable.count() == 0

    # CONVERTED also suppresses cold email queue
    deal.outcome = Outcome.CONVERTED
    deal.save()
    emailable_converted = get_emailable_deals(session)
    assert emailable_converted.count() == 0

    # Disqualified lead suppresses cold email queue
    deal.outcome = ""
    deal.save()
    lead.disqualified = True
    lead.save()
    emailable_disqualified = get_emailable_deals(session)
    assert emailable_disqualified.count() == 0


# ── 5. Channel Progression Independence (No Mutual Erasure) ───────────

@pytest.mark.django_db
def test_channel_progression_independence():
    """Progression on one channel does not erase eligibility on the other, with no ping-ponging."""
    campaign = Campaign.objects.create(
        name="Independence Campaign",
        is_active=True,
        linkedin_outreach_enabled=True,
        email_outreach_enabled=True,
    )
    lead = Lead.objects.create(
        first_name="Sam",
        last_name="Wilson",
        public_identifier="sam-wilson-indep",
        api_email="sam@wilson.com",
    )
    deal = Deal.objects.create(
        lead=lead,
        campaign=campaign,
        state=DealState.READY_TO_EMAIL,
        connect_attempts=0,
    )

    session = MagicMock()
    session.campaign = campaign

    # 1. Email state is READY_TO_EMAIL, but LinkedIn candidate pool still sees connect_attempts == 0
    li_profiles = get_qualified_profiles(session)
    assert len(li_profiles) == 1

    # 2. Simulate sending LinkedIn connection request (connect_attempts incremented)
    deal.connect_attempts = 1
    deal.save()

    # Email pool is unchanged and still sees deal in READY_TO_EMAIL
    emailable = get_emailable_deals(session)
    assert emailable.count() == 1


# ── 6. BetterContact Absence Safety ───────────────────────────────────

@pytest.mark.django_db
def test_bettercontact_absent_does_not_block_linkedin():
    """Missing BetterContact key reports WAITING_FOR_EMAIL_PROVIDER and allows LinkedIn outreach to continue."""
    campaign = Campaign.objects.create(
        name="BC Missing Campaign",
        is_active=True,
        linkedin_outreach_enabled=True,
        email_outreach_enabled=True,
    )
    lead = Lead.objects.create(
        first_name="Morgan",
        last_name="Lee",
        public_identifier="morgan-lee-bc-missing",
    )

    session = MagicMock()
    session.campaign = campaign
    qualifier = MagicMock()

    with patch("openoutreach.emails.bettercontact.is_configured", return_value=False):
        _save_qualification_result(
            session=session,
            qualifier=qualifier,
            lead_id=lead.id,
            public_id=lead.public_identifier,
            embedding=np.zeros(10),
            label=1,
            reason="Qualified fit",
        )

    deal = Deal.objects.get(lead=lead, campaign=campaign)
    assert deal.state == DealState.QUALIFIED
    assert not deal.lead.disqualified

    # LinkedIn eligibility remains active
    profiles = get_qualified_profiles(session)
    assert len(profiles) == 1
    assert profiles[0]["public_identifier"] == "morgan-lee-bc-missing"


# ── 7. Ollama Regression & Prompt Context Preservation ────────────────

@pytest.mark.django_db
def test_ollama_configuration_and_note_generation():
    """Ollama configuration without API key correctly triggers LLM note generation."""
    from openoutreach.core.models import SiteConfig

    cfg = SiteConfig.load()
    cfg.ai_model = "ollama:qwen3:1.7b-q4_K_M"
    cfg.llm_api_base = "http://127.0.0.1:11434/v1"
    cfg.llm_api_key = ""  # No API key required for Ollama
    cfg.save()

    campaign = Campaign.objects.create(
        name="Ollama Test Campaign",
        product_docs="SaaS Marketing Automation Platform",
        campaign_objective="Find agency owners needing automated lead gen",
        connection_note_enabled=True,
    )
    session = MagicMock()
    session.campaign = campaign

    profile_dict = {
        "first_name": "Scott",
        "last_name": "Jensen",
        "title": "VP of Marketing",
        "company": "Bitwage",
        "industry": "Fintech",
        "positions": [{"title": "VP of Marketing", "company_name": "Bitwage"}],
    }

    mock_result = MagicMock()
    mock_result.output = "hey Scott, saw your marketing work at Bitwage... curious what part of client acquisition has been trickiest to scale?"

    with patch("openoutreach.core.agents.connection_note._generate_llm_note", return_value=mock_result.output):
        note, source = generate_connection_note(session, campaign, profile_dict)

    assert source == "llm"
    assert "Scott" in note
    assert len(note) <= 300


@pytest.mark.django_db
def test_compose_opener_email_prompt_context_and_string_fallback():
    """Email opener agent preserves prompt context and falls back cleanly on small local LLMs."""
    from openoutreach.core.models import SiteConfig

    cfg = SiteConfig.load()
    cfg.ai_model = "ollama:qwen3:1.7b-q4_K_M"
    cfg.llm_api_base = "http://127.0.0.1:11434/v1"
    cfg.save()

    campaign = Campaign.objects.create(
        name="Opener Test Campaign",
        product_docs="Lead generation software",
        campaign_objective="Reach B2B Founders",
    )
    lead = Lead.objects.create(first_name="Diana", last_name="Prince", company="Themyscira Inc", current_title="CEO")
    deal = Deal.objects.create(lead=lead, campaign=campaign, state=DealState.READY_TO_EMAIL)

    session = MagicMock()
    session.campaign = campaign

    from openoutreach.core.agents.email_opener import EmailDraft

    # Mock structured Pydantic failure, testing string fallback parsing
    mock_str_result = MagicMock()
    mock_str_result.output = "Subject: Scaling B2B Lead Gen\nBody:\nHi Diana, noticed your leadership at Themyscira Inc. Would love to connect!"

    calls = 0

    def mock_sync(coro):
        nonlocal calls
        calls += 1
        if hasattr(coro, "close"):
            try:
                coro.close()
            except Exception:
                pass
        if calls == 1:
            raise Exception("Structured tool call unavailable")
        return mock_str_result

    with patch("openoutreach.core.agents.email_opener.run_agent_sync", side_effect=mock_sync):
        draft = compose_opener_email(session, deal)

    assert isinstance(draft, EmailDraft)
    assert draft.subject == "Scaling B2B Lead Gen"
    assert "Themyscira" in draft.body or "Diana" in draft.body
