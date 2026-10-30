# openoutreach/emails/models.py
"""Mailbox: one SMTP sending inbox, imported from the provider's creds export."""
from __future__ import annotations

from django.db import models
from django.utils import timezone
from django.contrib.auth.models import User

from openoutreach.core.conf import DEFAULT_EMAIL_DAILY_LIMIT


class MailboxManager(models.Manager):
    """Pool-level send pacing — the daily-cap accounting the task and planner share."""

    def for_workspace(self, workspace):
        if not workspace:
            return self.none()
        return self.filter(
            models.Q(workspace=workspace) |
            models.Q(sender_identity__workspace=workspace) |
            models.Q(user__workspace_memberships__workspace=workspace, user__workspace_memberships__status="active")
        ).distinct()

    def remaining_today(self, workspace=None) -> int:
        """Total sends left across the pool today (Σ per-box headroom).

        0 when no boxes exist or every box is at its cap.
        """
        qs = self.for_workspace(workspace) if workspace else self.all()
        return sum(box.headroom_today() for box in qs)

    def least_loaded_under_cap(self, workspace=None):
        """The under-cap box with the most headroom today, or None if all are capped."""
        qs = self.for_workspace(workspace) if workspace else self.all()
        ranked = [(box, sent) for box in qs
                  if (sent := box.sent_today()) < box.daily_limit]
        if not ranked:
            return None
        return min(ranked, key=lambda pair: pair[1])[0]


class Mailbox(models.Model):
    """One SMTP inbox. host/port default to IceMail's Google Workspace boxes.

    A row exists only once its credentials pass the import auth-check — the
    provider has no health API, so the import is the gate. Send-time failures
    are not swallowed: a bad send fails its task and is retried, the box is
    left untouched (re-import with fixed credentials to repair it).
    """

    workspace = models.ForeignKey(
        "core.Workspace",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="mailboxes",
    )
    user = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="mailboxes",
    )
    sender_identity = models.ForeignKey(
        "core.SenderIdentity",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="mailboxes",
    )
    host = models.CharField(max_length=255, default="smtp.gmail.com")
    port = models.PositiveIntegerField(default=587)
    username = models.CharField(max_length=320, unique=True)
    password = models.CharField(max_length=255)
    from_address = models.EmailField(max_length=320)
    # Warm-safe sends/day for this box, set at email onboarding (mirrors the
    # LinkedIn connect_daily_limit). Enforced at send time, per box.
    daily_limit = models.PositiveIntegerField(default=DEFAULT_EMAIL_DAILY_LIMIT)

    objects = MailboxManager()

    class Meta:
        verbose_name_plural = "Mailboxes"

    def __str__(self):
        return self.from_address or self.username

    def sent_today(self) -> int:
        """Emails this box has sent since local midnight (the per-box cap ledger).

        Keyed on ``email_sent_at`` (write-once at send, never cleared), so a deal
        dispositioned past EMAILED later the same day still counts against the cap.
        """
        midnight = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
        return self.deals.filter(email_sent_at__gte=midnight).count()

    def headroom_today(self) -> int:
        """Sends this box has left today before hitting ``daily_limit``."""
        return max(0, self.daily_limit - self.sent_today())


def has_mailbox(workspace=None) -> bool:
    """True when ≥1 mailbox is configured — i.e. email is a viable channel to
    send from. Gates email enrichment: with no mailbox there's nothing to send,
    so resolving an address is pointless and the deal should take the connect leg."""
    if workspace:
        return Mailbox.objects.for_workspace(workspace).exists()
    return Mailbox.objects.exists()

