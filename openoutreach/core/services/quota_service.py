# openoutreach/core/services/quota_service.py
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional, Dict, Any

from django.utils import timezone
from django.db.models import Q

logger = logging.getLogger(__name__)


def get_calendar_day_window(window_start: Optional[datetime] = None) -> datetime:
    """Returns the start of the current calendar day (midnight boundary)."""
    if window_start is not None:
        return window_start
    return timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)


def get_daily_successful_easy_apply_count(
    workspace=None,
    candidate_profile=None,
    window_start: Optional[datetime] = None,
) -> int:
    """
    Returns the total count of LinkedIn Easy Apply applications successfully executed today.
    Includes: submitted, submission_unverified, externally_confirmed.
    Excludes: submission_uncertain, needs_your_answer, failed before provider, duplicate, non-Easy-Apply.
    """
    from openoutreach.career.models import JobApplication

    day_start = get_calendar_day_window(window_start)
    qs = JobApplication.objects.filter(
        Q(submission_date__gte=day_start) | Q(created_at__gte=day_start)
    )
    if candidate_profile is not None:
        qs = qs.filter(candidate_profile=candidate_profile)
    elif workspace is not None:
        from django.contrib.auth.models import User
        users = User.objects.filter(campaigns__workspace=workspace).distinct()
        qs = qs.filter(candidate_profile__user__in=users)

    successful_statuses = [
        "submitted",
        "submission_unverified",
        "externally_confirmed",
    ]
    return qs.filter(status__in=successful_statuses).distinct().count()


def get_daily_job_provider_attempt_count(
    workspace=None,
    candidate_profile=None,
    window_start: Optional[datetime] = None,
) -> int:
    """
    Returns the total count of job applications that reached or crossed the provider write boundary today.
    Includes: submitted, submission_unverified, submission_uncertain, indeterminate, reconciliation_required, externally_confirmed.
    Excludes: draft, needs_your_answer, failed before provider.
    """
    from openoutreach.career.models import JobApplication

    day_start = get_calendar_day_window(window_start)
    qs = JobApplication.objects.filter(
        Q(submission_date__gte=day_start) | Q(attempted_at__gte=day_start) | Q(created_at__gte=day_start)
    )
    if candidate_profile is not None:
        qs = qs.filter(candidate_profile=candidate_profile)
    elif workspace is not None:
        from django.contrib.auth.models import User
        users = User.objects.filter(campaigns__workspace=workspace).distinct()
        qs = qs.filter(candidate_profile__user__in=users)

    consumed_statuses = [
        "submitted",
        "submission_unverified",
        "submission_uncertain",
        "indeterminate",
        "reconciliation_required",
        "provider_call_started",
        "externally_confirmed",
    ]
    return qs.filter(status__in=consumed_statuses).distinct().count()


def get_daily_connection_provider_attempt_count(
    workspace=None,
    linkedin_profile=None,
    window_start: Optional[datetime] = None,
) -> int:
    """
    Returns the total count of connection requests that reached or crossed the external provider write boundary today.
    Includes: externally_confirmed, uncertain, indeterminate, reconciliation_required, attempting, submitted.
    Excludes: prepared, failed_before_provider.
    """
    from openoutreach.linkedin.models import ConnectionAttempt

    day_start = get_calendar_day_window(window_start)
    qs = ConnectionAttempt.objects.filter(created_at__gte=day_start)

    if linkedin_profile is not None:
        qs = qs.filter(linkedin_profile=linkedin_profile)
    elif workspace is not None:
        qs = qs.filter(
            Q(linkedin_profile__workspace=workspace) |
            Q(linkedin_profile__user__campaigns__workspace=workspace)
        ).distinct()

    consumed_statuses = [
        "externally_confirmed",
        "uncertain",
        "indeterminate",
        "reconciliation_required",
        "attempting",
        "submitted",
    ]
    return qs.filter(status__in=consumed_statuses).count()


def get_daily_linkedin_outbound_message_breakdown(
    workspace=None,
    linkedin_profile=None,
    window_start: Optional[datetime] = None,
) -> Dict[str, int]:
    """
    Returns a breakdown of outbound LinkedIn messages sent/attempted today across:
    - follow_ups
    - ai_replies
    - other_messages
    - total_aggregate
    """
    from openoutreach.linkedin.models import ActionLog

    day_start = get_calendar_day_window(window_start)
    qs = ActionLog.objects.filter(created_at__gte=day_start)

    if linkedin_profile is not None:
        qs = qs.filter(linkedin_profile=linkedin_profile)
    elif workspace is not None:
        qs = qs.filter(
            Q(linkedin_profile__workspace=workspace) |
            Q(campaign__workspace=workspace) |
            Q(linkedin_profile__user__campaigns__workspace=workspace)
        ).distinct()

    followups = qs.filter(action_type=ActionLog.ActionType.FOLLOW_UP).count()
    replies = qs.filter(action_type__in=["reply", "ai_reply"]).count()
    other = qs.filter(action_type="message").count()

    total = followups + replies + other
    return {
        "follow_ups": followups,
        "ai_replies": replies,
        "other_messages": other,
        "total_aggregate": total,
    }


def get_daily_linkedin_outbound_message_count(
    workspace=None,
    linkedin_profile=None,
    window_start: Optional[datetime] = None,
) -> int:
    """Returns aggregate outbound LinkedIn messages today."""
    breakdown = get_daily_linkedin_outbound_message_breakdown(workspace, linkedin_profile, window_start)
    return breakdown["total_aggregate"]


def get_daily_email_send_count(
    workspace=None,
    window_start: Optional[datetime] = None,
) -> int:
    """
    Returns total outbound emails sent/attempted to SMTP today.
    """
    from openoutreach.emails.models import Mailbox

    if workspace is not None:
        mailboxes = Mailbox.objects.for_workspace(workspace)
    else:
        mailboxes = Mailbox.objects.all()

    return sum(box.sent_today() for box in mailboxes)


def get_workspace_quota_summary(workspace, auto_settings=None, window_start: Optional[datetime] = None) -> Dict[str, Any]:
    """Returns a unified, truthful dictionary of all quotas and consumed counters for a workspace."""
    from openoutreach.core.models import WorkspaceAutomationSettings
    from openoutreach.crm.models import Deal

    if auto_settings is None and workspace is not None:
        auto_settings = WorkspaceAutomationSettings.get_for_workspace(workspace)

    successful_easy_apply = get_daily_successful_easy_apply_count(workspace=workspace, window_start=window_start)
    jobs_consumed = get_daily_job_provider_attempt_count(workspace=workspace, window_start=window_start)
    conns_consumed = get_daily_connection_provider_attempt_count(workspace=workspace, window_start=window_start)
    msg_breakdown = get_daily_linkedin_outbound_message_breakdown(workspace=workspace, window_start=window_start)
    emails_consumed = get_daily_email_send_count(workspace=workspace, window_start=window_start)

    crm_pending = Deal.objects.filter(campaign__workspace=workspace, state="Pending").count() if workspace else 0
    provider_backlog = getattr(auto_settings, "linkedin_pending_invitations_count", 0) or 0

    return {
        "window": "UTC / Local Calendar Day (Midnight Boundary)",
        "jobs": {
            "successful_easy_apply": successful_easy_apply,
            "successful_target": getattr(auto_settings, "daily_job_application_target", 50) or 50,
            "provider_attempts": jobs_consumed,
            "provider_ceiling": getattr(auto_settings, "max_daily_job_applications", 60) or 60,
            "consumed": jobs_consumed,
            "ceiling": getattr(auto_settings, "max_daily_job_applications", 60) or 60,
            "remaining": max(0, (getattr(auto_settings, "daily_job_application_target", 50) or 50) - successful_easy_apply),
        },
        "connections": {
            "consumed": conns_consumed,
            "ceiling": getattr(auto_settings, "max_daily_connections", 40),
            "remaining": max(0, getattr(auto_settings, "max_daily_connections", 40) - conns_consumed),
        },
        "messages": {
            "consumed": msg_breakdown["total_aggregate"],
            "ceiling": getattr(auto_settings, "max_daily_messages", 90),
            "remaining": max(0, getattr(auto_settings, "max_daily_messages", 90) - msg_breakdown["total_aggregate"]),
            "follow_ups": msg_breakdown["follow_ups"],
            "ai_replies": msg_breakdown["ai_replies"],
            "other_messages": msg_breakdown["other_messages"],
        },
        "emails": {
            "consumed": emails_consumed,
            "ceiling": getattr(auto_settings, "max_daily_emails", 25),
            "remaining": max(0, getattr(auto_settings, "max_daily_emails", 25) - emails_consumed),
        },
        "backlog": {
            "provider_pending": provider_backlog,
            "crm_pending": crm_pending,
            "limit": getattr(auto_settings, "max_pending_invitations", 100),
            "override_active": getattr(auto_settings, "connection_backlog_operator_override", False),
        },
    }
