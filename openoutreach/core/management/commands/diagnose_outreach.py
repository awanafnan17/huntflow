# openoutreach/core/management/commands/diagnose_outreach.py
"""Read-only diagnostic report for the HuntFlow outreach system.

Prints a structured summary of accounts, campaigns, funnel health,
task queue, recent logs, and blocking reasons to stdout.

This command is strictly read-only:
- No model creates, updates, or deletes
- No LinkedIn API calls, scraping, or sending
- No LLM calls (note preview uses template substitution only)
- No BetterContact lookups
- No task scheduling or execution
- No secrets printed (keys shown as Configured / Not configured)
"""
from __future__ import annotations

import io

from django.core.management.base import BaseCommand
from django.utils import timezone


class Command(BaseCommand):
    help = "Print a read-only diagnostic report of HuntFlow outreach health."

    def handle(self, *args, **options):
        out = self.stdout
        now = timezone.now()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

        from openoutreach.core.models import Campaign, SiteConfig, Task
        from openoutreach.crm.models import Deal, DealState, Lead
        from openoutreach.linkedin.models import ActionLog, LinkedInProfile, SearchKeyword

        cfg = SiteConfig.load()

        # ── Section 1: Accounts ─────────────────────────────────────
        _header(out, "Accounts")

        profiles = LinkedInProfile.objects.select_related("user").all()
        if not profiles.exists():
            out.write("  No LinkedIn accounts configured.\n\n")
        else:
            for p in profiles:
                connect_used = ActionLog.objects.filter(
                    linkedin_profile=p,
                    action_type=ActionLog.ActionType.CONNECT,
                    created_at__gte=today_start,
                ).count()
                followup_used = ActionLog.objects.filter(
                    linkedin_profile=p,
                    action_type=ActionLog.ActionType.FOLLOW_UP,
                    created_at__gte=today_start,
                ).count()
                assigned = list(
                    Campaign.objects.filter(users=p.user).values_list("name", flat=True)
                )
                active_camps = list(Campaign.objects.filter(users=p.user, is_active=True))
                active_assigned = [ac.name for ac in active_camps]
                
                from openoutreach.core.scheduler import allocate_slots
                alloc_conn = allocate_slots(p.connect_daily_limit, 0, active_camps, "daily_connect_limit", p, "connect")
                alloc_follow = allocate_slots(p.follow_up_daily_limit, 0, active_camps, "daily_follow_up_limit", p, "follow_up")
                allocations = [f"{ac.name} (Connect: {alloc_conn.get(ac.pk, 0)}, Follow-up: {alloc_follow.get(ac.pk, 0)})" for ac in active_camps]

                out.write(f"  {p.linkedin_username}\n")
                out.write(f"    Active: {p.active}\n")
                out.write(f"    Connect limit: {p.connect_daily_limit if p.connect_daily_limit is not None else 'Unlimited'}  |  Used today: {connect_used}\n")
                out.write(f"    Follow-up limit: {p.follow_up_daily_limit if p.follow_up_daily_limit is not None else 'Unlimited'}  |  Used today: {followup_used}\n")
                out.write(f"    Campaigns: {', '.join(assigned) if assigned else 'None'}\n")
                out.write(f"    Active Campaigns Sharing Account: {', '.join(active_assigned) if active_assigned else 'None'}\n")
                out.write(f"    Estimated Slot Allocation: {', '.join(allocations) if allocations else 'None'}\n")
            out.write("\n")

        # ── Section 2: Campaigns ────────────────────────────────────
        _header(out, "Campaigns")

        campaigns = Campaign.objects.prefetch_related("users").all()
        if not campaigns.exists():
            out.write("  No campaigns configured.\n\n")
        else:
            for c in campaigns:
                users = list(c.users.values_list("username", flat=True))
                assigned_profiles = list(LinkedInProfile.objects.filter(user__in=c.users.all()).values_list("linkedin_username", flat=True))
                out.write(f"  {c.name}\n")
                out.write(f"    Active: {c.is_active}\n")
                out.write(f"    Connect Daily Cap: {c.daily_connect_limit if c.daily_connect_limit is not None else 'Unlimited'}\n")
                out.write(f"    Follow-up Daily Cap: {c.daily_follow_up_limit if c.daily_follow_up_limit is not None else 'Unlimited'}\n")
                out.write(f"    Shared LinkedIn accounts: {', '.join(assigned_profiles) if assigned_profiles else 'None'}\n")
                out.write(f"    Assigned users: {', '.join(users) if users else 'None'}\n")
                out.write(f"    Connection notes: {'Enabled' if c.connection_note_enabled else 'Disabled'}\n")
                if c.connection_note_template:
                    out.write(f"    Note template: {c.connection_note_template[:80]}{'...' if len(c.connection_note_template) > 80 else ''}\n")
                out.write(f"    Booking link: {c.booking_link or 'Not set'}\n")
                out.write(f"    Action fraction: {c.action_fraction}\n")

                # Latest search keywords
                keywords = SearchKeyword.objects.filter(campaign=c, used=True).order_by("-used_at")[:5]
                if keywords:
                    kw_list = [k.keyword for k in keywords]
                    out.write(f"    Recent keywords: {', '.join(kw_list)}\n")
                else:
                    out.write(f"    Recent keywords: None\n")

                out.write("\n")

        # ── Section 3: Funnel ───────────────────────────────────────
        _header(out, "Funnel")

        for c in campaigns:
            total_leads = Lead.objects.filter(deal__campaign=c).distinct().count()
            disqualified_leads = Lead.objects.filter(disqualified=True, deal__campaign=c).distinct().count()
            funnel = {}
            for state in DealState:
                funnel[state.label] = Deal.objects.filter(campaign=c, state=state).count()

            out.write(f"  {c.name}\n")
            out.write(f"    Discovered: {total_leads}\n")
            for label, count in funnel.items():
                out.write(f"    {label}: {count}\n")
            out.write(f"    Disqualified leads: {disqualified_leads}\n")

            # Recent disqualifications
            recent_dq = (
                Deal.objects.filter(campaign=c, state=DealState.FAILED, outcome="wrong_fit")
                .select_related("lead")
                .order_by("-update_date")[:5]
            )
            if recent_dq:
                out.write(f"    Recent disqualifications:\n")
                for d in recent_dq:
                    out.write(f"      {d.lead.public_identifier}: {d.reason or 'No reason recorded'}\n")

            # Note preview (template only, no LLM, no sending)
            if c.connection_note_enabled:
                preview = _render_note_preview(c)
                if preview:
                    out.write(f"    Sample note preview: {preview}\n")

            out.write("\n")

        # ── Section 4: Tasks ────────────────────────────────────────
        _header(out, "Tasks")

        for status_val, status_label in Task.Status.choices:
            count = Task.objects.filter(status=status_val).count()
            out.write(f"  {status_label}: {count}\n")
        out.write("\n")

        # Per-campaign pending breakdown
        pending_tasks = Task.objects.filter(status=Task.Status.PENDING)
        for c in campaigns:
            connect_pending = sum(
                1 for t in pending_tasks
                if t.payload.get("campaign_id") == c.id and t.task_type == Task.TaskType.CONNECT
            )
            followup_pending = sum(
                1 for t in pending_tasks
                if t.payload.get("campaign_id") == c.id and t.task_type == Task.TaskType.FOLLOW_UP
            )
            check_pending = sum(
                1 for t in pending_tasks
                if t.payload.get("campaign_id") == c.id and t.task_type == Task.TaskType.CHECK_PENDING
            )
            out.write(f"  {c.name}:\n")
            out.write(f"    Pending connect: {connect_pending}\n")
            out.write(f"    Pending follow-up: {followup_pending}\n")
            out.write(f"    Pending check: {check_pending}\n")
        out.write("\n")

        # ── Section 5: Recent Logs ──────────────────────────────────
        _header(out, "Recent Logs")

        recent_logs = (
            ActionLog.objects.select_related("linkedin_profile", "campaign")
            .order_by("-created_at")[:10]
        )
        if not recent_logs:
            out.write("  No action logs recorded.\n")
        else:
            for log in recent_logs:
                ts = log.created_at.strftime("%Y-%m-%d %H:%M:%S")
                note_info = ""
                if log.metadata:
                    if log.metadata.get("note_was_sent") is True:
                        note_info = " [note sent]"
                    elif log.metadata.get("note_was_sent") is False:
                        note_info = " [note NOT sent]"
                out.write(
                    f"  {ts}  {log.action_type:12s}  "
                    f"{log.campaign.name}  "
                    f"{log.linkedin_profile.linkedin_username}{note_info}\n"
                )
        out.write("\n")

        # ── Section 6: Blocking Reasons ─────────────────────────────
        _header(out, "Blocking Reasons")

        llm_configured = bool(cfg.llm_api_key)
        out.write(f"  LLM API key: {'Configured' if llm_configured else 'Not configured'}\n\n")

        any_blocking = False
        for c in campaigns:
            reasons = _get_blocking_reasons(c, cfg, today_start)
            if reasons:
                any_blocking = True
                out.write(f"  {c.name}:\n")
                for r in reasons:
                    out.write(f"    - {r}\n")
                out.write("\n")

        if not any_blocking:
            out.write("  No blocking issues detected.\n\n")

        out.write("Diagnostic report complete.\n")


def _header(out, title: str):
    """Print a section header."""
    out.write(f"{'=' * 60}\n")
    out.write(f"  {title}\n")
    out.write(f"{'=' * 60}\n")


def _get_blocking_reasons(campaign, cfg, today_start) -> list[str]:
    """Compute human-readable blocking reasons for a campaign (read-only)."""
    from openoutreach.core.models import Task
    from openoutreach.crm.models import Deal, DealState
    from openoutreach.linkedin.models import ActionLog, LinkedInProfile

    reasons = []

    if not campaign.is_active:
        reasons.append("Campaign is paused")
        return reasons

    # Check assigned users
    user_ids = list(campaign.users.values_list("id", flat=True))
    if not user_ids:
        reasons.append("No assigned users")
        return reasons

    # Check active LinkedIn profiles for assigned users
    active_profiles = LinkedInProfile.objects.filter(user_id__in=user_ids, active=True)
    if not active_profiles.exists():
        reasons.append("No active LinkedIn account")

    # Check daily limits
    all_exhausted = True
    for p in active_profiles:
        connect_used = ActionLog.objects.filter(
            linkedin_profile=p,
            action_type=ActionLog.ActionType.CONNECT,
            created_at__gte=today_start,
        ).count()
        if p.connect_daily_limit is None or connect_used < p.connect_daily_limit:
            all_exhausted = False
            break
    if active_profiles.exists() and all_exhausted:
        reasons.append("Daily connect limit reached")

    # Check pending connect tasks
    pending_connect = sum(
        1 for t in Task.objects.filter(
            status=Task.Status.PENDING, task_type=Task.TaskType.CONNECT,
        )
        if t.payload.get("campaign_id") == campaign.id
    )
    if pending_connect == 0:
        reasons.append("No pending connect tasks")

    # Check qualified leads
    qualified_count = Deal.objects.filter(
        campaign=campaign, state=DealState.QUALIFIED,
    ).count()
    ready_count = Deal.objects.filter(
        campaign=campaign, state=DealState.READY_TO_CONNECT,
    ).count()
    if qualified_count == 0 and ready_count == 0:
        reasons.append("No qualified leads")

    # Check ready leads
    if ready_count == 0 and qualified_count > 0:
        reasons.append("No ready leads available")

    # Check LLM configuration
    if not cfg.llm_api_key:
        reasons.append("LLM not configured")

    return reasons


def _render_note_preview(campaign) -> str | None:
    """Render a sample note using template substitution only (no LLM, no sending).

    Uses the first READY_TO_CONNECT deal's lead public_identifier to build
    a minimal sample profile. If no ready lead exists, uses a placeholder.
    """
    from openoutreach.core.agents.connection_note import _generate_template_note
    from openoutreach.crm.models import Deal, DealState

    deal = (
        Deal.objects.filter(campaign=campaign, state=DealState.READY_TO_CONNECT)
        .select_related("lead")
        .first()
    )

    if deal:
        sample_profile = {
            "first_name": deal.lead.public_identifier.split("-")[0].title(),
            "public_identifier": deal.lead.public_identifier,
        }
    else:
        # Use a generic sample for preview
        sample_profile = {
            "first_name": "Alex",
        }

    try:
        note = _generate_template_note(campaign, sample_profile)
        return note[:120] + "..." if len(note) > 120 else note
    except Exception:
        return None
