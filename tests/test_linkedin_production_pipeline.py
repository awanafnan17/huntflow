import pytest
from unittest.mock import MagicMock, patch
from django.utils import timezone
from django.contrib.auth.models import User

from openoutreach.core.models import Campaign, Task, SiteConfig
from openoutreach.crm.models import Lead, Deal, DealState, Outcome
from openoutreach.linkedin.models import LinkedInProfile, ActionLog
from openoutreach.core.scheduler import allocate_slots, plan_connect_window, plan_follow_up_window
from openoutreach.core.task_factory import create_external_task


def _create_test_profile(user, connect_limit=90, follow_up_limit=90):
    return LinkedInProfile.objects.create(
        user=user,
        active=True,
        session_status="valid",
        identity_validation_status="VALID_IDENTITY_MATCH",
        cookie_data={"cookies": [{"name": "li_at", "value": "test"}]},
        last_authenticated_at=timezone.now(),
        connect_daily_limit=connect_limit,
        follow_up_daily_limit=follow_up_limit,
    )


@pytest.mark.django_db
def test_effective_quota_calculation():
    """Verify effective daily connect cap respects min(campaign_cap, profile_cap)."""
    user = User.objects.create(username="user1")
    p = _create_test_profile(user, connect_limit=90, follow_up_limit=90)
    c = Campaign.objects.create(
        name="Production Campaign",
        is_active=True,
        linkedin_outreach_enabled=True,
        daily_connect_limit=80,
        daily_follow_up_limit=90,
        linkedin_profile=p,
    )
    c.users.add(user)

    # 1. Allocate connect slots
    allocs = allocate_slots(
        total_limit=p.connect_daily_limit,
        daily_count=0,
        campaigns=[c],
        limit_attr="daily_connect_limit",
        profile=p,
        action_type="connect",
    )
    # Expected: min(80, 90) = 80
    assert allocs[c.pk] == 80


@pytest.mark.django_db
def test_connect_task_idempotency():
    """Verify duplicate CONNECT tasks are blocked by idempotency key."""
    user = User.objects.create(username="user2")
    p = _create_test_profile(user, connect_limit=90)
    c = Campaign.objects.create(
        name="LinkedIn Campaign",
        is_active=True,
        linkedin_outreach_enabled=True,
        linkedin_profile=p,
    )
    c.users.add(user)
    lead = Lead.objects.create(first_name="Jane", last_name="Doe", public_identifier="jane-doe-123")

    task1 = create_external_task(
        task_type=Task.TaskType.CONNECT,
        campaign_id=c.pk,
        lead_id=lead.id,
        linkedin_profile_id=p.pk,
    )
    assert task1 is not None

    # Second call for same lead and campaign should return existing task (idempotent)
    task2 = create_external_task(
        task_type=Task.TaskType.CONNECT,
        campaign_id=c.pk,
        lead_id=lead.id,
        linkedin_profile_id=p.pk,
    )
    assert task2.id == task1.id


@pytest.mark.django_db
def test_followup_task_idempotency():
    """Verify duplicate FOLLOW_UP tasks are blocked by idempotency key."""
    user = User.objects.create(username="user3")
    p = _create_test_profile(user, follow_up_limit=90)
    c = Campaign.objects.create(
        name="LinkedIn Campaign",
        is_active=True,
        linkedin_outreach_enabled=True,
        linkedin_profile=p,
    )
    c.users.add(user)
    lead = Lead.objects.create(first_name="Bob", last_name="Smith", public_identifier="bob-smith-456")
    deal = Deal.objects.create(lead=lead, campaign=c, state=DealState.CONNECTED)

    task1 = create_external_task(
        task_type=Task.TaskType.FOLLOW_UP,
        campaign_id=c.pk,
        deal_id=deal.id,
        lead_id=lead.id,
        linkedin_profile_id=p.pk,
    )
    assert task1 is not None

    task2 = create_external_task(
        task_type=Task.TaskType.FOLLOW_UP,
        campaign_id=c.pk,
        deal_id=deal.id,
        lead_id=lead.id,
        linkedin_profile_id=p.pk,
    )
    assert task2.id == task1.id


@pytest.mark.django_db
def test_acceptance_transition_and_followup_scheduling():
    """Verify state transition to CONNECTED enables follow-up eligibility."""
    user = User.objects.create(username="user4")
    p = _create_test_profile(user, follow_up_limit=90)
    c = Campaign.objects.create(
        name="LinkedIn Campaign",
        is_active=True,
        linkedin_outreach_enabled=True,
        linkedin_profile=p,
    )
    c.users.add(user)
    lead = Lead.objects.create(first_name="Alice", last_name="Walker", public_identifier="alice-w-789")
    deal = Deal.objects.create(lead=lead, campaign=c, state=DealState.PENDING)

    # Simulate connection acceptance
    deal.state = DealState.CONNECTED
    deal.save()

    session = MagicMock()
    session.campaign = c
    session.campaigns = [c]
    session.linkedin_profile = p
    session.active_timezone = "UTC"

    created = plan_follow_up_window(session, c)
    assert created == 1

    task = Task.objects.filter(task_type=Task.TaskType.FOLLOW_UP, payload__campaign_id=c.pk).first()
    assert task is not None
    assert task.payload.get("deal_id") == deal.id


@pytest.mark.django_db
def test_cross_channel_outcome_suppression():
    """Verify NOT_INTERESTED or CONVERTED deals are excluded from follow-up planning."""
    user = User.objects.create(username="user5")
    p = _create_test_profile(user, follow_up_limit=90)
    c = Campaign.objects.create(
        name="LinkedIn Campaign",
        is_active=True,
        linkedin_outreach_enabled=True,
        linkedin_profile=p,
    )
    c.users.add(user)
    lead = Lead.objects.create(first_name="Charlie", last_name="Brown", public_identifier="charlie-b-000")
    deal = Deal.objects.create(
        lead=lead, campaign=c, state=DealState.CONNECTED, outcome=Outcome.NOT_INTERESTED
    )

    session = MagicMock()
    session.campaign = c
    session.campaigns = [c]
    session.linkedin_profile = p
    session.active_timezone = "UTC"

    created = plan_follow_up_window(session, c)
    assert created == 0


@pytest.mark.django_db
def test_campaign_pause_blocks_planning():
    """Verify paused campaign plans 0 tasks."""
    user = User.objects.create(username="user6")
    p = _create_test_profile(user, connect_limit=90)
    c = Campaign.objects.create(
        name="Paused Campaign",
        is_active=False,
        linkedin_outreach_enabled=True,
        linkedin_profile=p,
    )
    c.users.add(user)

    session = MagicMock()
    session.campaign = c
    session.linkedin_profile = p

    created = plan_connect_window(session, c)
    assert created == 0


@pytest.mark.django_db
def test_campaign1_email_isolation():
    """Verify Campaign #1 with email_outreach_enabled=False skips email flushing."""
    c1 = Campaign.objects.create(
        name="LinkedIn Outreach",
        is_active=True,
        linkedin_outreach_enabled=True,
        email_outreach_enabled=False,
    )

    from openoutreach.core.scheduler import flush_email_queue
    session = MagicMock()
    count = flush_email_queue(session, c1)
    assert count == 0
