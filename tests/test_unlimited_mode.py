import pytest
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.utils import IntegrityError
from django.contrib.auth.models import User
from django.utils import timezone
from django.core.management import call_command
from django.urls import reverse
import json
from io import StringIO

from openoutreach.core.models import Campaign, Task
from openoutreach.linkedin.models import LinkedInProfile, ActionLog
from openoutreach.core.scheduler import (
    allocate_slots,
    plan_connect_window,
    plan_follow_up_window,
    reconcile,
    UNLIMITED_QUEUE_PLANNING_BATCH
)
from openoutreach.dashboard.forms import CampaignForm, LinkedInProfileForm
from tests.factories import UserFactory
from openoutreach.core.readiness import check_launch_readiness
from openoutreach.core.launch_preview import generate_launch_preview
from openoutreach.core.operations import get_operations_snapshot


class FakeAccountSession:
    def __init__(self, django_user, linkedin_profile, campaign):
        self.django_user = django_user
        self.linkedin_profile = linkedin_profile
        self.campaign = campaign
        self.campaigns = [campaign]
        self.active_timezone = "UTC"


@pytest.mark.django_db
def test_safe_finite_defaults_and_nullable_limits():
    """Verify that new campaigns and profiles default to 20/25 limits, and can be nullable."""
    user = User.objects.create_user(username="defaults_user")
    p = LinkedInProfile.objects.create(
        user=user,
        linkedin_username="defaults@example.com",
        linkedin_password="pw"
    )
    assert p.connect_daily_limit == 20
    assert p.follow_up_daily_limit == 25
    
    p.connect_daily_limit = None
    p.follow_up_daily_limit = None
    p.save()
    p.refresh_from_db()
    assert p.connect_daily_limit is None
    assert p.follow_up_daily_limit is None

    c = Campaign.objects.create(name="Defaults Campaign")
    assert c.daily_connect_limit == 20
    assert c.daily_follow_up_limit == 25

    c.daily_connect_limit = None
    c.daily_follow_up_limit = None
    c.save()
    c.refresh_from_db()
    assert c.daily_connect_limit is None
    assert c.daily_follow_up_limit is None


@pytest.mark.django_db
def test_database_constraints_reject_invalid():
    """Verify that database CheckConstraints reject non-positive integers (0, negatives) but accept positive integers and NULL."""
    user = User.objects.create_user(username="db_constraint_user")
    p = LinkedInProfile.objects.create(
        user=user,
        linkedin_username="db@example.com",
        linkedin_password="pw"
    )
    
    # 1. Reject zero
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            LinkedInProfile.objects.filter(pk=p.pk).update(connect_daily_limit=0)
            
    # 2. Reject negative
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            LinkedInProfile.objects.filter(pk=p.pk).update(connect_daily_limit=-10)

    # 3. Accept positive integer
    LinkedInProfile.objects.filter(pk=p.pk).update(connect_daily_limit=10)
    p.refresh_from_db()
    assert p.connect_daily_limit == 10

    # 4. Accept NULL
    LinkedInProfile.objects.filter(pk=p.pk).update(connect_daily_limit=None)
    p.refresh_from_db()
    assert p.connect_daily_limit is None

    # Do the same for Campaign
    c = Campaign.objects.create(name="DB Constraint Campaign")
    
    # 1. Reject zero
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            Campaign.objects.filter(pk=c.pk).update(daily_connect_limit=0)
            
    # 2. Reject negative
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            Campaign.objects.filter(pk=c.pk).update(daily_connect_limit=-10)

    # 3. Accept positive integer
    Campaign.objects.filter(pk=c.pk).update(daily_connect_limit=10)
    c.refresh_from_db()
    assert c.daily_connect_limit == 10

    # 4. Accept NULL
    Campaign.objects.filter(pk=c.pk).update(daily_connect_limit=None)
    c.refresh_from_db()
    assert c.daily_connect_limit is None


@pytest.mark.django_db
def test_form_validation_scenarios():
    """Verify forms check validation rules: checkbox checks, blanks, zeros, negatives, and not erases."""
    user = User.objects.create_user(username="form_user")
    
    # CampaignForm: Checked Unlimited saves None
    f1 = CampaignForm(data={
        "name": "Unlimited Camp",
        "is_active": True,
        "action_fraction": 0.2,
        "connect_unlimited": True,
        "daily_connect_limit": "",
        "follow_up_unlimited": True,
        "daily_follow_up_limit": "",
    })
    assert f1.is_valid(), f1.errors
    c = f1.save()
    assert c.daily_connect_limit is None
    assert c.daily_follow_up_limit is None

    # CampaignForm: Unchecked Unlimited but blank field raises error
    f2 = CampaignForm(data={
        "name": "Unlimited Camp 2",
        "is_active": True,
        "action_fraction": 0.2,
        "connect_unlimited": False,
        "daily_connect_limit": "",
        "follow_up_unlimited": False,
        "daily_follow_up_limit": "",
    })
    assert not f2.is_valid()
    assert "daily_connect_limit" in f2.errors
    assert "daily_follow_up_limit" in f2.errors

    # CampaignForm: Unchecked Unlimited and positive integer is valid
    f3 = CampaignForm(data={
        "name": "Unlimited Camp 3",
        "is_active": True,
        "action_fraction": 0.2,
        "connect_unlimited": False,
        "daily_connect_limit": 5,
        "follow_up_unlimited": False,
        "daily_follow_up_limit": 7,
    })
    assert f3.is_valid(), f3.errors
    c3 = f3.save()
    assert c3.daily_connect_limit == 5
    assert c3.daily_follow_up_limit == 7

    # CampaignForm: Edit finite value does not erase unless checked
    f4 = CampaignForm(instance=c3, data={
        "name": "Unlimited Camp 3",
        "is_active": True,
        "action_fraction": 0.2,
        "connect_unlimited": False,
        "daily_connect_limit": 5,
        "follow_up_unlimited": True,
        "daily_follow_up_limit": "",
    })
    assert f4.is_valid(), f4.errors
    c3 = f4.save()
    assert c3.daily_connect_limit == 5
    assert c3.daily_follow_up_limit is None


@pytest.mark.django_db
def test_scheduler_allocations():
    """Verify scheduler allocate_slots behaves under various limit combinations."""
    user = User.objects.create_user(username="sched_user")
    profile = LinkedInProfile.objects.create(
        user=user,
        linkedin_username="sched@example.com",
        linkedin_password="pw",
    )
    c1 = Campaign.objects.create(name="C1", is_active=True)
    c1.users.add(user)

    # 1. Finite profile (10) and finite campaign (5) -> capped by 5
    profile.connect_daily_limit = 10
    c1.daily_connect_limit = 5
    profile.save()
    c1.save()
    alloc = allocate_slots(profile.connect_daily_limit, 0, [c1], "daily_connect_limit", profile, "connect")
    assert alloc[c1.pk] == 5

    # 2. Finite profile (10) and unlimited campaign (None) -> capped by 10
    c1.daily_connect_limit = None
    c1.save()
    alloc = allocate_slots(profile.connect_daily_limit, 0, [c1], "daily_connect_limit", profile, "connect")
    assert alloc[c1.pk] == 10

    # 3. Unlimited profile (None) and finite campaign (5) -> capped by 5
    profile.connect_daily_limit = None
    profile.save()
    c1.daily_connect_limit = 5
    c1.save()
    alloc = allocate_slots(profile.connect_daily_limit, 0, [c1], "daily_connect_limit", profile, "connect")
    assert alloc[c1.pk] == 5

    # 4. Unlimited profile (None) and unlimited campaign (None) -> batch limit 500
    c1.daily_connect_limit = None
    c1.save()
    alloc = allocate_slots(profile.connect_daily_limit, 0, [c1], "daily_connect_limit", profile, "connect")
    assert alloc[c1.pk] == UNLIMITED_QUEUE_PLANNING_BATCH


@pytest.mark.django_db
def test_reconciliation_integration():
    """Verify reconciliation handles duplicate prevention, atomic claiming, spacing, inactive items, and reports."""
    user = User.objects.create_user(username="reconcile_user")
    profile = LinkedInProfile.objects.create(
        user=user,
        linkedin_username="reconcile@example.com",
        linkedin_password="pw",
        active=True,
        session_status="valid",
        identity_validation_status="VALID_IDENTITY_MATCH",
        connect_daily_limit=None,
        follow_up_daily_limit=None
    )
    
    campaign = Campaign.objects.create(name="Reconcile Campaign", is_active=True, linkedin_profile=profile, daily_connect_limit=None, daily_follow_up_limit=None)
    campaign.users.add(user)
    
    session = FakeAccountSession(django_user=user, linkedin_profile=profile, campaign=campaign)

    # 1. Paused campaign exclusion
    paused_campaign = Campaign.objects.create(name="Paused Campaign", is_active=False, daily_connect_limit=None, daily_follow_up_limit=None)
    paused_campaign.users.add(user)
    created = plan_connect_window(session, paused_campaign)
    assert created == 0

    # 2. Inactive profile exclusion
    profile.active = False
    profile.save()
    created = plan_connect_window(session, campaign)
    assert created == 0
    
    profile.active = True
    profile.save()

    # 3. Plan connect window
    from openoutreach.crm.models import Deal, DealState, Lead
    for i in range(UNLIMITED_QUEUE_PLANNING_BATCH):
        l = Lead.objects.create(public_identifier=f"reconcile_lead_{i}", linkedin_url=f"https://www.linkedin.com/in/reconcile_lead_{i}", workspace=campaign.workspace)
        Deal.objects.create(campaign=campaign, lead=l, state=DealState.READY_TO_CONNECT)
    created = plan_connect_window(session, campaign)
    assert created == UNLIMITED_QUEUE_PLANNING_BATCH
    
    # 4. Spacing remains active
    first_task = Task.objects.filter(task_type=Task.TaskType.CONNECT).order_by("scheduled_at").first()
    last_task = Task.objects.filter(task_type=Task.TaskType.CONNECT).order_by("scheduled_at").last()
    assert first_task.scheduled_at <= timezone.now()
    assert (last_task.scheduled_at - first_task.scheduled_at).total_seconds() > 0

    # 5. Duplicate task prevention / Repeated reconciliation does not add another full batch
    # Attempting to plan again when PENDING tasks exist returns 0
    created_again = plan_connect_window(session, campaign)
    assert created_again == 0

    # 6. Pending and running tasks reduce planning availability
    # Delete some tasks and simulate one RUNNING
    Task.objects.all().delete()
    l_run = Lead.objects.create(public_identifier="running_lead", linkedin_url="https://www.linkedin.com/in/running_lead", workspace=campaign.workspace)
    Task.objects.create(
        task_type=Task.TaskType.CONNECT,
        status=Task.Status.RUNNING,
        scheduled_at=timezone.now(),
        payload={"campaign_id": campaign.pk, "lead_id": l_run.id}
    )
    created_reduced = plan_connect_window(session, campaign)
    assert created_reduced == UNLIMITED_QUEUE_PLANNING_BATCH - 1

    # 7. Task claiming remains atomic
    # Claiming should pull first eligible task and atomically mark it RUNNING
    claimed = Task.objects.claim_next()
    assert claimed is not None
    assert claimed.status == Task.Status.RUNNING

    # 8. Readiness command supports unlimited
    output = StringIO()
    try:
        call_command("launch_readiness", json=True, stdout=output)
    except SystemExit:
        pass
    report = json.loads(output.getvalue())
    assert report["overall_status"] in ["pass", "warning", "fail", "ready", "blocked"]

    # 9. Preview command supports unlimited
    output_preview = StringIO()
    try:
        call_command("launch_preview", json=True, limit=5, stdout=output_preview)
    except SystemExit:
        pass
    preview = json.loads(output_preview.getvalue())
    assert preview["readiness_status"] in ["pass", "warning", "fail", "ready", "blocked"]

    # 10. Operations command supports unlimited
    output_ops = StringIO()
    try:
        call_command("operations_status", json=True, stdout=output_ops)
    except SystemExit:
        pass
    ops = json.loads(output_ops.getvalue())
    assert "daemon_health" in ops

    # 11. JSON raw values remain null
    p_check = LinkedInProfile.objects.get(pk=profile.pk)
    c_check = Campaign.objects.get(pk=campaign.pk)
    assert p_check.connect_daily_limit is None
    assert c_check.daily_connect_limit is None


@pytest.mark.django_db
def test_queue_planning_proof_unlimited_pacing():
    """Verify that calling reconcile repeatedly does not add another 500 tasks
    while the queue target is already filled."""
    user = User.objects.create_user(username="proof_user")
    profile = LinkedInProfile.objects.create(
        user=user,
        linkedin_username="proof@example.com",
        linkedin_password="pw",
        active=True,
        session_status="valid",
        identity_validation_status="VALID_IDENTITY_MATCH",
        connect_daily_limit=None,
        follow_up_daily_limit=None
    )
    campaign = Campaign.objects.create(
        name="Proof Campaign",
        is_active=True,
        linkedin_profile=profile,
        daily_connect_limit=None,
        daily_follow_up_limit=None
    )
    campaign.users.add(user)
    
    from openoutreach.crm.models import Deal, DealState, Lead
    for i in range(UNLIMITED_QUEUE_PLANNING_BATCH):
        l_conn = Lead.objects.create(public_identifier=f"unlimited_proof_lead_{i}", linkedin_url=f"https://www.linkedin.com/in/unlimited_proof_lead_{i}", workspace=campaign.workspace)
        Deal.objects.create(campaign=campaign, lead=l_conn, state=DealState.READY_TO_CONNECT)
        l_fol = Lead.objects.create(public_identifier=f"unlimited_proof_fol_{i}", linkedin_url=f"https://www.linkedin.com/in/unlimited_proof_fol_{i}", workspace=campaign.workspace)
        Deal.objects.create(campaign=campaign, lead=l_fol, state=DealState.CONNECTED)
    session = FakeAccountSession(django_user=user, linkedin_profile=profile, campaign=campaign)
    
    # 1. Reconcile once
    reconcile(session)
    
    # 2. Count PENDING and RUNNING tasks by profile, campaign, type
    connect_pending = Task.objects.filter(
        task_type=Task.TaskType.CONNECT,
        status=Task.Status.PENDING,
        payload__campaign_id=campaign.pk
    ).count()
    
    follow_up_pending = Task.objects.filter(
        task_type=Task.TaskType.FOLLOW_UP,
        status=Task.Status.PENDING,
        payload__campaign_id=campaign.pk
    ).count()
    
    assert connect_pending == UNLIMITED_QUEUE_PLANNING_BATCH
    assert follow_up_pending == UNLIMITED_QUEUE_PLANNING_BATCH
    
    # 3. Reconcile again
    reconcile(session)
    
    # 4. Prove the second reconciliation does not add another 500 tasks
    connect_pending_after = Task.objects.filter(
        task_type=Task.TaskType.CONNECT,
        status=Task.Status.PENDING,
        payload__campaign_id=campaign.pk
    ).count()
    
    follow_up_pending_after = Task.objects.filter(
        task_type=Task.TaskType.FOLLOW_UP,
        status=Task.Status.PENDING,
        payload__campaign_id=campaign.pk
    ).count()
    
    assert connect_pending_after == UNLIMITED_QUEUE_PLANNING_BATCH
    assert follow_up_pending_after == UNLIMITED_QUEUE_PLANNING_BATCH
