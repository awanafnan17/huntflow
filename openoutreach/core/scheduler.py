# openoutreach/core/scheduler.py
"""Per-type 24h planner with Poisson-spaced lazy task slots.

The daemon's task queue is *lazy*: each row carries only ``task_type``,
``campaign_id``, and ``scheduled_at``. The handler resolves a concrete
target (lead/deal) at execution time via a single eligibility query.

This module is the only place that creates ``Task`` rows. The pipeline
moves forward in three layers:

1. **Per-type planner** — ``plan_connect_window``,
   ``plan_follow_up_window``, ``plan_check_pending_window``. Each one,
   when no PENDING task of its type exists for a campaign, computes the
   right slot count ``n`` for the next 24h, inserts one row that fires
   immediately, and Poisson-spaces the remaining ``n - 1`` rows across
   the working portion of the window. The leading immediate slot kills
   the cold-start ramp (without it the first action would sit ``T/n``
   away on average — ~72 min for a 20/day campaign).

2. **State-transition hook** — ``on_deal_state_entered(deal)`` only
   updates ``deal.next_check_pending_at`` for PENDING transitions. It
   does **not** insert any Task row. CONNECTED and other transitions
   are no-ops.

3. **Reconcile** — ``reconcile(session)``. Recovers stale RUNNING tasks
   and calls each planner per campaign. The daemon invokes it on startup
   and whenever the queue has no ready task.
"""
from __future__ import annotations

import datetime
import logging
import random
from datetime import datetime as Datetime, timedelta
from zoneinfo import ZoneInfo

from django.utils import timezone

from openoutreach.core.conf import (
    ACTIVE_END_HOUR,
    ACTIVE_START_HOUR,
    CAMPAIGN_CONFIG,
    CHECK_PENDING_DAILY_CAP,
    ENABLE_ACTIVE_HOURS,
)
from openoutreach.crm.models import DealState
from openoutreach.core.models import Task

logger = logging.getLogger(__name__)


# ── Working-hours arithmetic ──────────────────────────────────────────


def _working_intervals(start, end, tz_name) -> list[tuple]:
    """Return ``[(s, e), ...]`` UTC datetimes for the working portions of
    ``[start, end]``. The whole window ``[(start, end)]`` is returned —
    i.e. no gating — when ``ENABLE_ACTIVE_HOURS`` is False or ``tz_name`` is
    None (timezone not resolved, e.g. unknown profile country)."""
    if not ENABLE_ACTIVE_HOURS or tz_name is None:
        return [(start, end)]

    tz = ZoneInfo(tz_name)
    local_start = start.astimezone(tz)
    local_end = end.astimezone(tz)

    intervals: list[tuple] = []
    day = local_start.date()
    last_day = local_end.date()
    while day <= last_day:
        day_active_start = Datetime(
            day.year, day.month, day.day, ACTIVE_START_HOUR, tzinfo=tz,
        )
        day_active_end = Datetime(
            day.year, day.month, day.day, ACTIVE_END_HOUR, tzinfo=tz,
        )
        s = max(day_active_start, local_start)
        e = min(day_active_end, local_end)
        if e > s:
            intervals.append((s, e))
        day = day + timedelta(days=1)
    return intervals


def working_seconds_in_window(start, end, tz_name) -> float:
    """Sum of seconds inside ``[ACTIVE_START_HOUR, ACTIVE_END_HOUR]`` between
    ``start`` and ``end``. Returns ``(end - start).total_seconds()`` when
    active hours are disabled or ``tz_name`` is None (no gating)."""
    if not ENABLE_ACTIVE_HOURS or tz_name is None:
        return max(0.0, (end - start).total_seconds())
    return sum((e - s).total_seconds() for s, e in _working_intervals(start, end, tz_name))


def poisson_slot_times(now, n: int, tz_name, horizon_hours: float = 24) -> list:
    """Return ``n`` strictly-increasing timestamps inside the working
    portion of ``[now, now + horizon_hours]``.

    Implementation: sample ``n`` uniform positions in ``[0, T)`` (working
    seconds) and sort. This is the order-statistic representation of a
    conditional Poisson process given ``n`` arrivals in the window —
    same distribution as exponential inter-arrival sampling, but
    guarantees exactly ``n`` slots without overshoot. Mean spacing in
    working time is ``T / (n + 1)``.
    """
    if n <= 0:
        return []

    end = now + timedelta(hours=horizon_hours)
    intervals = _working_intervals(now, end, tz_name)
    total = sum((e - s).total_seconds() for s, e in intervals)
    if total <= 0:
        return []

    positions = sorted(random.uniform(0, total) for _ in range(n))

    times: list = []
    cursor_interval = 0
    cursor_offset = 0.0  # working-seconds consumed before the current interval
    for pos in positions:
        while cursor_interval < len(intervals):
            s, e = intervals[cursor_interval]
            dur = (e - s).total_seconds()
            if pos < cursor_offset + dur:
                times.append(s + timedelta(seconds=pos - cursor_offset))
                break
            cursor_offset += dur
            cursor_interval += 1
    return times


def _campaign_daily_count(profile, campaign, action_type: str) -> int:
    from openoutreach.linkedin.models import ActionLog
    today_start = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
    return ActionLog.objects.filter(
        linkedin_profile=profile,
        campaign=campaign,
        action_type=action_type,
        created_at__gte=today_start,
    ).count()


UNLIMITED_QUEUE_PLANNING_BATCH = 25


def allocate_slots(total_limit: int | None, daily_count: int, campaigns: list, limit_attr: str, profile, action_type: str) -> dict[int, int]:
    """Allocate remaining daily slots across campaigns using equal share, capped by campaign remaining cap."""
    from openoutreach.core.models import Task
    
    is_unlimited = (total_limit is None)
    
    if is_unlimited:
        existing_planned = Task.objects.filter(
            task_type=action_type,
            status__in=[Task.Status.PENDING, Task.Status.RUNNING],
            payload__campaign_id__in=[c.pk for c in campaigns]
        ).count()
        remaining = max(0, UNLIMITED_QUEUE_PLANNING_BATCH - existing_planned)
    else:
        remaining = max(0, total_limit - daily_count)
        
    allocations = {c.pk: 0 for c in campaigns}
    if remaining <= 0 or not campaigns:
        return allocations

    # Campaign caps: if None/blank, cap is remaining. Otherwise cap is campaign_limit - campaign_daily_count
    caps = {}
    for c in campaigns:
        val = getattr(c, limit_attr)
        if val is not None:
            done = _campaign_daily_count(profile, c, action_type)
            caps[c.pk] = max(0, val - done)
        else:
            caps[c.pk] = remaining

    # Iterative allocation (water-filling/equal share)
    active_pks = [c.pk for c in campaigns]
    while remaining > 0 and active_pks:
        share = remaining // len(active_pks)
        if share == 0:
            for pk in list(active_pks):
                if remaining > 0:
                    allocations[pk] += 1
                    remaining -= 1
                    if allocations[pk] >= caps[pk]:
                        active_pks.remove(pk)
                else:
                    break
            break

        next_active = []
        for pk in active_pks:
            alloc = allocations[pk] + share
            cap = caps[pk]
            if alloc >= cap:
                remaining -= (cap - allocations[pk])
                allocations[pk] = cap
            else:
                remaining -= share
                allocations[pk] = alloc
                next_active.append(pk)
        active_pks = next_active

    return allocations


# ── Per-type planners ─────────────────────────────────────────────────


def _has_pending(task_type: "Task.TaskType", campaign_id: int) -> bool:
    return Task.objects.filter(
        task_type=task_type,
        status=Task.Status.PENDING,
        payload__campaign_id=campaign_id,
    ).exists()


def _create_lazy_slots(task_type: "Task.TaskType", campaign_id: int, times: list) -> int:
    if not times:
        return 0
    profile_id = None
    try:
        from openoutreach.core.models import Campaign
        camp = Campaign.objects.filter(pk=campaign_id).first()
        if camp:
            if camp.linkedin_profile_id:
                profile_id = camp.linkedin_profile_id
            elif camp.sender_identity and getattr(camp.sender_identity, "linkedin_profile_id", None):
                profile_id = camp.sender_identity.linkedin_profile_id
    except Exception:
        pass

    payload = {"campaign_id": campaign_id}
    if profile_id:
        payload["linkedin_profile_id"] = profile_id

    Task.objects.bulk_create([
        Task(
            task_type=task_type,
            scheduled_at=t,
            payload=payload,
        )
        for t in times
    ])
    return len(times)


def _plan_slots(task_type: "Task.TaskType", campaign_id: int, n: int, tz_name) -> int:
    """Schedule *n* lazy slots: one fires immediately, the remaining
    ``n - 1`` are Poisson-spaced across the next 24h working window
    (``tz_name`` defines that window's local hours; None → full 24h).

    The leading immediate slot is intentional — without it the first
    action of a freshly-planned window would sit ``T/n`` away on average
    (the mean of a single ``Exp(n/T) draw). That cold-start ramp made
    `make run` feel dead for ~an hour on a 20/day campaign.
    """
    if n <= 0:
        return 0
    now = timezone.now()
    times = [now] + poisson_slot_times(now, n - 1, tz_name)
    return _create_lazy_slots(task_type, campaign_id, times)


def _plan_targeted_slots(task_type: "Task.TaskType", campaign, times: list, session) -> int:
    """Schedule Poisson-spaced external write tasks bound to concrete eligible targets."""
    if not times:
        return 0
    from openoutreach.core.task_factory import create_external_task
    from openoutreach.crm.models import Deal, DealState
    
    created = 0
    if task_type == Task.TaskType.CONNECT:
        from openoutreach.core.db.deals import _deals_at_state
        session.campaign = campaign
        ready_deals = _deals_at_state(session, DealState.READY_TO_CONNECT)
        if not ready_deals:
            ready_deals = _deals_at_state(session, DealState.QUALIFIED)
        if not ready_deals:
            try:
                from openoutreach.linkedin.tasks.connect import strategy_for
                strategy = strategy_for(campaign, getattr(session, "qualifiers", {}))
                cand = strategy.find_candidate(session)
                if cand:
                    ready_deals = [cand]
            except Exception:
                pass
        
        for i, t in enumerate(times):
            if i < len(ready_deals):
                cand = ready_deals[i]
                lead_id = cand.get("lead_id") or cand.get("id")
                if not lead_id and "public_identifier" in cand:
                    from openoutreach.crm.models import Lead
                    lead_obj = Lead.objects.filter(public_identifier=cand["public_identifier"]).first()
                    if lead_obj:
                        lead_id = lead_obj.id
                if lead_id:
                    task = create_external_task(
                        task_type=task_type,
                        campaign_id=campaign.pk,
                        lead_id=lead_id,
                        linkedin_profile_id=getattr(session.linkedin_profile, "id", None) if hasattr(session, "linkedin_profile") else None,
                        scheduled_at=t,
                    )
                    if task:
                        created += 1
    elif task_type == Task.TaskType.FOLLOW_UP:
        from openoutreach.linkedin.tasks.follow_up import _connected_deals, _too_soon_to_nudge
        session.campaign = campaign
        eligible_deals = [d for d in _connected_deals(campaign) if not _too_soon_to_nudge(d)]
        for i, t in enumerate(times):
            if i < len(eligible_deals):
                deal = eligible_deals[i]
                task = create_external_task(
                    task_type=task_type,
                    campaign_id=campaign.pk,
                    deal_id=deal.id,
                    lead_id=deal.lead_id,
                    linkedin_profile_id=getattr(session.linkedin_profile, "id", None) if hasattr(session, "linkedin_profile") else None,
                    scheduled_at=t,
                )
                if task:
                    created += 1
    return created


def _is_automation_enabled_for_campaign(campaign, action_name: str) -> bool:
    """Checks whether the workspace owning this campaign has enabled master automation & specific switch."""
    from openoutreach.core.models import WorkspaceAutomationSettings
    ws = getattr(campaign, "workspace", None)
    if not ws and campaign.users.exists():
        from openoutreach.core.models import WorkspaceMembership
        mem = WorkspaceMembership.objects.filter(user=campaign.users.first(), status="active").first()
        if mem:
            ws = mem.workspace
    if not ws:
        return True
    settings = WorkspaceAutomationSettings.objects.filter(workspace=ws).first()
    if not settings:
        return False
    if not settings.master_automation_enabled:
        return False
    if action_name == "connect":
        if not settings.auto_send_connections:
            return False
        if settings.connection_backlog_behavior == "PAUSE_NEW_CONNECTIONS" and not settings.connection_backlog_operator_override:
            from openoutreach.crm.models import Deal
            pending_count = Deal.objects.filter(campaign__workspace=settings.workspace, state="Pending").count()
            provider_pending = getattr(settings, "linkedin_pending_invitations_count", 0) or 0
            effective_backlog = max(pending_count, provider_pending)
            if effective_backlog >= settings.max_pending_invitations:
                return False
    if action_name == "follow_up" and not settings.auto_followup:
        return False
    if action_name == "reply" and not settings.auto_monitor_replies:
        return False
    if action_name == "email" and not settings.auto_send_emails:
        return False
    return True


def plan_prospect_discovery_window(session, campaign) -> int:
    """Ensure campaign has active prospect inventory for connection planning."""
    if not campaign.is_active or not getattr(campaign, "linkedin_outreach_enabled", True):
        return 0
    prof = getattr(session, "linkedin_profile", None) or getattr(campaign, "linkedin_profile", None)
    if not prof or not prof.active or prof.session_status != "valid":
        return 0

    from openoutreach.crm.models import Deal, DealState
    from openoutreach.core.models import WorkspaceAutomationSettings

    ws = getattr(campaign, "workspace", None)
    auto_st = WorkspaceAutomationSettings.get_for_workspace(ws) if ws else None
    if not auto_st or not auto_st.master_automation_enabled or not auto_st.auto_send_connections:
        return 0

    if getattr(auto_st, "provider_safety_state", "NORMAL") in ["HIGH_VOLUME_DATA_ACCESS_WARNING", "CHECKPOINT_REQUIRED", "ACCOUNT_RESTRICTED"] or auto_st.soak_status == "SAFETY_STOP" or getattr(auto_st, "manual_provider_use_paused", False):
        return 0

    if Task.objects.filter(
        task_type=Task.TaskType.PROSPECT_DISCOVERY,
        status__in=[Task.Status.PENDING, Task.Status.RUNNING],
        payload__campaign_id=campaign.pk,
    ).exists():
        return 0

    ready_count = Deal.objects.filter(campaign=campaign, state=DealState.READY_TO_CONNECT, lead__disqualified=False).count()
    low_water = getattr(auto_st, "connection_inventory_low_water", 5) or 5
    high_water = getattr(auto_st, "connection_inventory_high_water", 20) or 20

    # If inventory is at or above high_water or low_water, suppress discovery
    if ready_count >= high_water or ready_count >= low_water:
        return 0

    now = timezone.now()
    min_interval = 300 if ready_count == 0 else 1800
    needs_discovery = False
    if ready_count < low_water:
        if not auto_st.last_prospect_discovery_at or (now - auto_st.last_prospect_discovery_at).total_seconds() >= min_interval:
            needs_discovery = True

    if not needs_discovery:
        return 0

    task = Task.objects.create(
        task_type=Task.TaskType.PROSPECT_DISCOVERY,
        scheduled_at=now,
        payload={
            "campaign_id": campaign.pk,
            "workspace_id": ws.id if ws else None,
            "idempotency_key": f"prospect_discovery:{campaign.pk}:{now.strftime('%Y%m%d%H%M')}",
        },
    )
    logger.info("[%s] enqueued autonomous prospect discovery Task #%d (ready_deals=%d, low_water=%d)", campaign, task.id, ready_count, low_water)
    return 1


def plan_connect_window(session, campaign) -> int:
    """Plan the next 24h of connect slots for *campaign*, requiring concrete targets."""
    if not campaign.is_active:
        return 0
    if not getattr(campaign, "linkedin_outreach_enabled", True):
        return 0
    if not _is_automation_enabled_for_campaign(campaign, "connect"):
        return 0
    if not session.linkedin_profile.active or session.linkedin_profile.session_status != "valid":
        return 0

    from openoutreach.core.models import Campaign, WorkspaceAutomationSettings
    ws = getattr(campaign, "workspace", None)
    auto_st = WorkspaceAutomationSettings.get_for_workspace(ws) if ws else None
    if auto_st:
        if getattr(auto_st, "provider_safety_state", "NORMAL") in ["HIGH_VOLUME_DATA_ACCESS_WARNING", "CHECKPOINT_REQUIRED", "ACCOUNT_RESTRICTED"] or auto_st.soak_status == "SAFETY_STOP" or getattr(auto_st, "manual_provider_use_paused", False):
            return 0
        auto_st.last_connection_planner_at = timezone.now()
        auto_st.save(update_fields=["last_connection_planner_at", "updated_at"])

    from openoutreach.core.db.deals import _deals_at_state
    from openoutreach.crm.models import DealState
    session.campaign = campaign
    eligible_connect = _deals_at_state(session, DealState.READY_TO_CONNECT) or _deals_at_state(session, DealState.QUALIFIED)
    if not eligible_connect:
        try:
            from openoutreach.linkedin.tasks.connect import strategy_for
            strategy = strategy_for(campaign, getattr(session, "qualifiers", {}))
            cand = strategy.find_candidate(session)
            if cand:
                eligible_connect = [cand]
        except Exception:
            pass
    if not eligible_connect:
        return 0

    if _has_pending(Task.TaskType.CONNECT, campaign.pk):
        return 0

    profile = session.linkedin_profile
    active_campaigns = list(Campaign.objects.filter(users=profile.user, is_active=True))
    if campaign not in active_campaigns:
        return 0

    conn_limit = profile.connect_daily_limit
    if auto_st and auto_st.max_daily_connections is not None and auto_st.max_daily_connections > 0:
        if conn_limit is not None:
            conn_limit = min(conn_limit, auto_st.max_daily_connections)
        else:
            conn_limit = auto_st.max_daily_connections

    allocations = allocate_slots(
        total_limit=conn_limit,
        daily_count=profile._daily_count("connect"),
        campaigns=active_campaigns,
        limit_attr="daily_connect_limit",
        profile=profile,
        action_type="connect"
    )
    n = min(allocations.get(campaign.pk, 0), len(eligible_connect))
    if n <= 0:
        return 0

    now = timezone.now()
    times = [now] + poisson_slot_times(now, n - 1, session.active_timezone)
    created = _plan_targeted_slots(Task.TaskType.CONNECT, campaign, times, session)

    if created:
        logger.info(
            "[%s] planned %d targeted connect slots over next 24h (daily=%s)",
            campaign, created, str(conn_limit),
        )
    return created


def plan_follow_up_window(session, campaign) -> int:
    """Plan the next 24h of follow-up slots for *campaign*. Requires connected eligible deals."""
    if not campaign.is_active:
        return 0
    if not getattr(campaign, "linkedin_outreach_enabled", True):
        return 0
    if not _is_automation_enabled_for_campaign(campaign, "follow_up"):
        return 0
    if not session.linkedin_profile.active or session.linkedin_profile.session_status != "valid":
        return 0

    from openoutreach.core.models import Campaign, WorkspaceAutomationSettings
    ws = getattr(campaign, "workspace", None)
    auto_st = WorkspaceAutomationSettings.get_for_workspace(ws) if ws else None
    if auto_st:
        if getattr(auto_st, "provider_safety_state", "NORMAL") in ["HIGH_VOLUME_DATA_ACCESS_WARNING", "CHECKPOINT_REQUIRED", "ACCOUNT_RESTRICTED"] or auto_st.soak_status == "SAFETY_STOP" or getattr(auto_st, "manual_provider_use_paused", False):
            return 0
        auto_st.last_followup_planner_at = timezone.now()
        auto_st.save(update_fields=["last_followup_planner_at", "updated_at"])

    from openoutreach.crm.models import Deal, DealState
    from openoutreach.linkedin.tasks.follow_up import _connected_deals, _too_soon_to_nudge
    session.campaign = campaign
    eligible_deals = [d for d in _connected_deals(campaign) if not _too_soon_to_nudge(d)]
    if not eligible_deals:
        return 0
    if _has_pending(Task.TaskType.FOLLOW_UP, campaign.pk):
        return 0

    profile = session.linkedin_profile
    active_campaigns = list(Campaign.objects.filter(users=profile.user, is_active=True))
    if campaign not in active_campaigns:
        return 0

    follow_up_limit = profile.follow_up_daily_limit
    if auto_st and auto_st.max_daily_messages is not None and auto_st.max_daily_messages > 0:
        if follow_up_limit is not None:
            follow_up_limit = min(follow_up_limit, auto_st.max_daily_messages)
        else:
            follow_up_limit = auto_st.max_daily_messages

    allocations = allocate_slots(
        total_limit=follow_up_limit,
        daily_count=profile._daily_count("follow_up"),
        campaigns=active_campaigns,
        limit_attr="daily_follow_up_limit",
        profile=profile,
        action_type="follow_up"
    )
    n = min(allocations.get(campaign.pk, 0), len(eligible_deals))
    if n <= 0:
        return 0

    now = timezone.now()
    times = [now] + poisson_slot_times(now, n - 1, session.active_timezone)
    created = _plan_targeted_slots(Task.TaskType.FOLLOW_UP, campaign, times, session)
    if created:
        logger.info(
            "[%s] planned %d targeted follow_up slots over next 24h (daily=%s)",
            campaign, created, str(follow_up_limit),
        )
    return created


def plan_check_pending_window(session, campaign) -> int:
    """Plan the next 24h of check_pending slots for *campaign*. Slot count
    matches the PENDING deals whose backoff has already expired (due now),
    capped by ``CHECK_PENDING_DAILY_CAP``.

    The "due now" bound must match the handler's own due filter
    (``_next_due_pending_deal``): counting deals due later within the
    horizon would plan an immediate slot the handler then finds nothing
    to do for, leaving the queue empty and re-triggering reconcile in a
    tight no-op spin."""
    if not campaign.is_active:
        return 0
    if not getattr(campaign, "linkedin_outreach_enabled", True):
        return 0
    if not _is_automation_enabled_for_campaign(campaign, "check_pending"):
        return 0
    from openoutreach.crm.models import Deal
    from openoutreach.core.models import WorkspaceAutomationSettings

    ws = getattr(campaign, "workspace", None)
    auto_st = WorkspaceAutomationSettings.get_for_workspace(ws) if ws else None
    if auto_st:
        if getattr(auto_st, "provider_safety_state", "NORMAL") in ["HIGH_VOLUME_DATA_ACCESS_WARNING", "CHECKPOINT_REQUIRED", "ACCOUNT_RESTRICTED"] or auto_st.soak_status == "SAFETY_STOP" or getattr(auto_st, "manual_provider_use_paused", False):
            return 0
        auto_st.last_acceptance_poll_at = timezone.now()
        auto_st.save(update_fields=["last_acceptance_poll_at", "updated_at"])

    if _has_pending(Task.TaskType.CHECK_PENDING, campaign.pk):
        return 0

    now = timezone.now()
    pending_deals = Deal.objects.filter(
        campaign_id=campaign.pk,
        state=DealState.PENDING,
        next_check_pending_at__lte=now,
    ).select_related("lead")

    prof = getattr(session, "linkedin_profile", None) or getattr(campaign, "linkedin_profile", None)
    if not prof or not prof.active or prof.session_status != "valid":
        return 0
    from openoutreach.linkedin.models import ProviderRelationship
    due_deals = []
    for d in pending_deals:
        if prof:
            rel = ProviderRelationship.objects.filter(
                workspace=ws,
                linkedin_profile=prof,
                lead=d.lead,
            ).first()
            if rel:
                if rel.operator_review_required:
                    continue
                if rel.next_eligible_reconciliation_at and now < rel.next_eligible_reconciliation_at:
                    continue
        due_deals.append(d)

    n_due = len(due_deals)
    n = min(n_due, CHECK_PENDING_DAILY_CAP)

    created = _plan_slots(Task.TaskType.CHECK_PENDING, campaign.pk, n, session.active_timezone)
    if created:
        logger.info(
            "[%s] planned %d check_pending slots over next 24h — 1 fires now, "
            "%d Poisson-spaced (due=%d, cap=%d)",
            campaign, created, max(0, created - 1), n_due, CHECK_PENDING_DAILY_CAP,
        )
    return created


# ── Eager drain (no window) ───────────────────────────────────────────


def flush_email_queue(session, campaign) -> int:
    """Drain the READY_TO_EMAIL pool for *campaign* into immediate task slots."""
    if not campaign.is_active:
        return 0
    if not getattr(campaign, "email_outreach_enabled", True):
        return 0
    if not _is_automation_enabled_for_campaign(campaign, "email"):
        return 0
    from openoutreach.crm.models import Deal
    from openoutreach.emails.models import Mailbox
    from openoutreach.core.models import WorkspaceAutomationSettings

    ws = getattr(campaign, "workspace", None)
    auto_st = WorkspaceAutomationSettings.get_for_workspace(ws) if ws else None
    if auto_st:
        auto_st.last_email_cycle_at = timezone.now()
        auto_st.save(update_fields=["last_email_cycle_at", "updated_at"])

    if _has_pending(Task.TaskType.EMAIL, campaign.pk):
        return 0

    remaining = Mailbox.objects.remaining_today(workspace=ws)
    if remaining <= 0:
        return 0

    queued = Deal.objects.filter(
        campaign_id=campaign.pk,
        state=DealState.READY_TO_EMAIL,
        lead__disqualified=False,
        outcome="",
    ).count()
    n = min(queued, remaining)
    if n <= 0:
        return 0

    now = timezone.now()
    created = _create_lazy_slots(Task.TaskType.EMAIL, campaign.pk, [now] * n)
    logger.info(
        "[%s] flushed %d email slots to send now (queued=%d, cap_remaining=%d)",
        campaign, created, queued, remaining,
    )
    return created


# ── Delay helpers ─────────────────────────────────────────────────────


def seconds_until_tomorrow() -> float:
    """Seconds until 00:00 local time — used for daily rate-limit waits."""
    now = timezone.now()
    tomorrow = (now + datetime.timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0,
    )
    return (tomorrow - now).total_seconds()


# ── State-transition hook ─────────────────────────────────────────────


def on_deal_state_entered(deal) -> None:
    """PENDING: stamp ``deal.next_check_pending_at = now + backoff_hours``.
    All other transitions are no-ops (CONNECTED tasks are created lazily
    by the planner, never by state changes)."""
    state = DealState(deal.state)
    if state != DealState.PENDING:
        return

    backoff = deal.backoff_hours or CAMPAIGN_CONFIG["check_pending_recheck_after_hours"]
    deal.next_check_pending_at = timezone.now() + timedelta(hours=backoff)
    deal.save(update_fields=["next_check_pending_at"])


# ── Reconciliation ────────────────────────────────────────────────────


def _recover_stale_running_tasks() -> int:
    """Reset RUNNING tasks to PENDING. RUNNING rows can only linger if the
    daemon crashed mid-task, so they are always stale at reconcile time."""
    count = Task.objects.filter(status=Task.Status.RUNNING).update(
        status=Task.Status.PENDING,
    )
    if count:
        logger.info("Recovered %d stale running tasks", count)
    return count


def plan_job_window(session, workspace=None, candidate=None) -> int:
    """Ensure job discovery and applications are actively scheduled to hit the 50/day target.
    
    Autonomous feedback loop:
    - If daily successful Easy Apply submissions < daily_target (50) and daily attempts < daily_ceiling (60):
      - If no pending JOB_DISCOVERY or JOB_APPLICATION tasks exist:
        - Enqueue a JOB_DISCOVERY task scheduled now.
    """
    from openoutreach.core.models import Workspace, WorkspaceAutomationSettings, Task
    from openoutreach.career.models import CandidateProfile
    from openoutreach.core.services.quota_service import (
        get_daily_successful_easy_apply_count,
        get_daily_job_provider_attempt_count,
    )

    if not workspace:
        if session and getattr(session, "campaigns", None):
            for c in session.campaigns:
                if getattr(c, "workspace", None):
                    workspace = c.workspace
                    break
        if not workspace:
            workspace = Workspace.objects.filter(slug="default").first() or Workspace.objects.first()
    if not workspace:
        return 0

    if not candidate:
        from django.contrib.auth.models import User
        django_user = getattr(session, "django_user", None)
        if django_user and isinstance(django_user, User):
            candidate = CandidateProfile.objects.filter(user=django_user).first()
        if not candidate and workspace:
            candidate = CandidateProfile.objects.filter(user__workspace_memberships__workspace=workspace).first()
            if not candidate:
                candidate = CandidateProfile.objects.filter(user__campaigns__workspace=workspace).first()

    if not candidate:
        logger.warning("No candidate profile found for workspace %s. Skipping job planning.", getattr(workspace, "slug", workspace))
        return 0

    auto_settings = WorkspaceAutomationSettings.get_for_workspace(workspace)
    if not auto_settings or not auto_settings.master_automation_enabled or not auto_settings.auto_apply_jobs:
        return 0

    from openoutreach.linkedin.models import LinkedInProfile
    active_prof = LinkedInProfile.objects.filter(workspace=workspace, active=True, session_status="valid").first()
    if not active_prof:
        return 0

    if getattr(auto_settings, "provider_safety_state", "NORMAL") in ["HIGH_VOLUME_DATA_ACCESS_WARNING", "CHECKPOINT_REQUIRED", "ACCOUNT_RESTRICTED"] or auto_settings.soak_status == "SAFETY_STOP" or getattr(auto_settings, "manual_provider_use_paused", False):
        return 0

    successful_today = get_daily_successful_easy_apply_count(workspace=workspace, candidate_profile=candidate)
    attempts_today = get_daily_job_provider_attempt_count(workspace=workspace, candidate_profile=candidate)
    daily_target = getattr(auto_settings, "daily_job_application_target", 50) or 50
    daily_ceiling = auto_settings.max_daily_job_applications or 60

    if successful_today >= daily_target or attempts_today >= daily_ceiling:
        return 0

    # 1. Coalescing: Check for any existing active (PENDING or RUNNING) job discovery or application task for this workspace/candidate
    active_job_tasks = Task.objects.filter(
        task_type__in=[Task.TaskType.JOB_APPLICATION, Task.TaskType.JOB_DISCOVERY],
        status__in=[Task.Status.PENDING, Task.Status.RUNNING],
    )
    if workspace:
        active_job_tasks = active_job_tasks.filter(payload__workspace_id=workspace.id)
    if candidate:
        active_job_tasks = active_job_tasks.filter(payload__candidate_id=candidate.id)

    if active_job_tasks.exists():
        return 0

    # 2. Cooldown: Do not spam discovery cycles if one was started recently (<15 min)
    now = timezone.now()
    if auto_settings.last_job_discovery_started_at:
        elapsed = (now - auto_settings.last_job_discovery_started_at).total_seconds()
        if elapsed < 900:  # 15 minutes cooldown
            return 0

    task = Task.objects.create(
        task_type=Task.TaskType.JOB_DISCOVERY,
        status=Task.Status.PENDING,
        scheduled_at=now,
        payload={
            "workspace_id": workspace.id,
            "candidate_id": candidate.id if candidate else None,
            "idempotency_key": f"job_discovery:{workspace.id}:{candidate.id if candidate else 0}:{now.strftime('%Y%m%d%H%M')}",
        },
    )
    auto_settings.last_job_discovery_started_at = now
    auto_settings.save(update_fields=["last_job_discovery_started_at", "updated_at"])
    logger.info("Scheduled immediate Job Discovery Task #%s (Progress: %d/%d target)", task.id, successful_today, daily_target)
    return 1


def plan_reply_poll_window(session, campaign) -> int:
    """Ensure inbound replies are polled periodically for active campaigns."""
    if not campaign.is_active or not getattr(campaign, "linkedin_outreach_enabled", True):
        return 0
    prof = getattr(session, "linkedin_profile", None) or getattr(campaign, "linkedin_profile", None)
    if not prof or not prof.active or prof.session_status != "valid":
        return 0

    from openoutreach.core.models import WorkspaceAutomationSettings
    ws = getattr(campaign, "workspace", None)
    auto_st = WorkspaceAutomationSettings.get_for_workspace(ws) if ws else None
    if not auto_st or not auto_st.master_automation_enabled or not auto_st.auto_monitor_replies:
        return 0

    if getattr(auto_st, "provider_safety_state", "NORMAL") in ["HIGH_VOLUME_DATA_ACCESS_WARNING", "CHECKPOINT_REQUIRED", "ACCOUNT_RESTRICTED"] or auto_st.soak_status == "SAFETY_STOP" or getattr(auto_st, "manual_provider_use_paused", False):
        return 0

    now = timezone.now()
    if auto_st:
        auto_st.last_reply_poll_at = now
        auto_st.save(update_fields=["last_reply_poll_at", "updated_at"])

    if Task.objects.filter(
        task_type=Task.TaskType.REPLY_POLL,
        status__in=[Task.Status.PENDING, Task.Status.RUNNING],
        payload__campaign_id=campaign.pk,
    ).exists():
        return 0

    if auto_st.last_reply_poll_at and (now - auto_st.last_reply_poll_at).total_seconds() < 900:
        return 0

    task = Task.objects.create(
        task_type=Task.TaskType.REPLY_POLL,
        scheduled_at=now,
        payload={
            "campaign_id": campaign.pk,
            "workspace_id": ws.id if ws else None,
            "idempotency_key": f"reply_poll:{campaign.pk}:{now.strftime('%Y%m%d%H%M')}",
        },
    )
    logger.info("[%s] enqueued autonomous reply poll Task #%d", campaign, task.id)
    return 1


_PLANNERS = (
    plan_prospect_discovery_window,
    plan_connect_window,
    plan_follow_up_window,
    plan_reply_poll_window,
    plan_check_pending_window,
)


def reconcile(session) -> None:
    """Recover stale RUNNING tasks, then ensure every (campaign, task_type)
    whose pending queue is empty gets a fresh 24h plan. Runs on daemon
    startup and whenever the queue has no ready task."""
    _recover_stale_running_tasks()

    if session:
        ws = None
        lp = getattr(session, "linkedin_profile", None)
        if lp and getattr(lp, "workspace", None):
            ws = lp.workspace
        elif getattr(session, "campaign", None) and session.campaign.workspace:
            ws = session.campaign.workspace
        elif getattr(session, "campaigns", None):
            for c in session.campaigns:
                if getattr(c, "workspace", None):
                    ws = c.workspace
                    break

        from openoutreach.core.models import WorkspaceAutomationSettings
        auto_st = WorkspaceAutomationSettings.get_for_workspace(ws) if ws else None
        if auto_st:
            if not auto_st.master_automation_enabled:
                logger.info("Master automation disabled for workspace %s — skipping queue reconcile", getattr(ws, "slug", ws))
                return
            if auto_st.soak_status == "SAFETY_STOP":
                logger.info(
                    "Automation safety hold active (soak_status=SAFETY_STOP, reason=%s) for workspace %s — skipping queue reconcile",
                    auto_st.soak_stop_reason, getattr(ws, "slug", ws)
                )
                return
            if getattr(auto_st, "manual_provider_use_paused", False):
                logger.info(
                    "Manual provider use paused by operator for workspace %s — skipping queue reconcile",
                    getattr(ws, "slug", ws)
                )
                return
            if getattr(auto_st, "provider_safety_state", "NORMAL") in [
                "HIGH_VOLUME_DATA_ACCESS_WARNING", "CHECKPOINT_REQUIRED", "ACCOUNT_RESTRICTED"
            ]:
                logger.warning(
                    "Provider safety state '%s' active for workspace %s — skipping queue reconcile",
                    auto_st.provider_safety_state, getattr(ws, "slug", ws)
                )
                return

        if lp:
            try:
                lp.refresh_from_db(fields=["session_status", "active"])
            except Exception:
                pass
            if not getattr(lp, "active", True) or getattr(lp, "session_status", "valid") in [
                "needs_reauth", "expired", "invalid", "checkpoint", "restricted", "challenge_required"
            ]:
                logger.info(
                    "LinkedInProfile #%s in fail-closed state (active=%s, status=%s) — skipping provider queue reconcile",
                    getattr(lp, "pk", None), getattr(lp, "active", None), getattr(lp, "session_status", None)
                )
                return

    for campaign in getattr(session, "campaigns", []):
        if not campaign.is_active:
            continue
        for planner in _PLANNERS:
            planner(session, campaign)
        # Eager-drain counterpart to the window planners — queue every
        # ready email to send now (paced only by the per-box daily cap).
        flush_email_queue(session, campaign)

    # Reconcile autonomous job pipeline independently of outreach campaigns
    plan_job_window(session)

    pending_count = Task.objects.pending().count()
    logger.info("Task queue reconciled: %d pending tasks", pending_count)

