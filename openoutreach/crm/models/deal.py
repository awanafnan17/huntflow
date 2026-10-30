from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError


class DealState(models.TextChoices):
    """OpenOutreach-owned funnel state for a Deal.

    OpenOutreach owns these values, not linkedin_cli. The library's connect/status
    verbs only *observe* three of them off the LinkedIn UI — QUALIFIED, PENDING,
    CONNECTED — and hand them back as plain strings over the CLI boundary; every
    other state is written only here: READY_TO_CONNECT (passed the GP threshold),
    the email fork (READY_TO_EMAIL/EMAILED), and COMPLETED/FAILED (outcome). The
    three UI-observed string values match the library's so lifting a returned
    string into this enum is a plain ``DealState(value)`` lookup at the boundary.

    Email channel: enrichment is a *router*, not a gate, and the route is an
    explicit FSM fork — the state *is* the routing:

        HIT  ─▶ READY_TO_EMAIL ─(EMAIL task)─▶ EMAILED   (Layer-1 quasi-terminal)
        MISS ─▶ stays QUALIFIED ─(GP gate)─▶ READY_TO_CONNECT ─▶ … ─▶ CONNECTED

    On a finder hit the qualify router transitions QUALIFIED → READY_TO_EMAIL (a
    cheap, *ungated* send-queue — any qualified lead with an address, FIFO, paced
    only by the per-box cap; unlike READY_TO_CONNECT it is NOT a GP confidence
    gate). The single Layer-1 send moves it to EMAILED, a quasi-terminal state
    that rests until a human sets an Outcome (no inbound reading yet). A miss,
    finder-off, or couldn't-run leaves the deal QUALIFIED so the GP gate can
    promote it to READY_TO_CONNECT — its only door — and the connection harvests
    contact info on acceptance. The two fork states encode the one-shot guarantee
    in the state column: the email pool holds only READY_TO_EMAIL, so a deal is
    sent exactly once and can never double-send.
    """
    QUALIFIED = "Qualified"
    READY_TO_EMAIL = "Ready to Email"
    EMAILED = "Emailed"
    READY_TO_CONNECT = "Ready to Connect"
    PENDING = "Pending"
    CONNECTED = "Connected"
    COMPLETED = "Completed"
    FAILED = "Failed"


class Outcome(models.TextChoices):
    CONVERTED = "converted"
    NOT_INTERESTED = "not_interested"
    WRONG_FIT = "wrong_fit"
    NO_BUDGET = "no_budget"
    HAS_SOLUTION = "has_solution"
    BAD_TIMING = "bad_timing"
    UNRESPONSIVE = "unresponsive"
    UNKNOWN = "unknown"


class Deal(models.Model):
    class Meta:
        verbose_name = _("Deal")
        verbose_name_plural = _("Deals")
        constraints = [
            models.UniqueConstraint(fields=["lead", "campaign"], name="unique_deal_per_campaign"),
        ]

    lead = models.ForeignKey("Lead", on_delete=models.CASCADE)
    campaign = models.ForeignKey(
        "core.Campaign", on_delete=models.CASCADE, related_name="deals",
    )
    state = models.CharField(
        max_length=20,
        choices=DealState.choices,
        default=DealState.QUALIFIED,
    )
    outcome = models.CharField(
        max_length=20,
        choices=Outcome.choices,
        blank=True,
        default="",
    )
    reason = models.TextField(blank=True, default="")
    connect_attempts = models.IntegerField(default=0)
    backoff_hours = models.IntegerField(default=0)
    next_check_pending_at = models.DateTimeField(null=True, blank=True, db_index=True)
    is_synthetic = models.BooleanField(default=False)
    synthetic_run = models.ForeignKey(
        "core.SyntheticDemonstrationRun",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="deals",
    )
    # Email channel (Layer 1 = single outbound touch, no follow-up cadence yet).
    # The mailbox that sent the email, bound to the deal: it's the per-box-cap
    # counting key (ChatMessage.filter(deal__mailbox=box)), the reply anchor, and
    # the sticky thread box once follow-ups land with inbound reply-reading.
    mailbox = models.ForeignKey(
        "emails.Mailbox", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="deals",
    )
    # The email's subject — set when the single email is sent; the reuse source
    # ("Re: …") once follow-ups land. The agent generates it once.
    email_subject = models.CharField(max_length=300, blank=True, default="")
    # When the single Layer-1 email was sent. The per-box daily cap counts deals
    # sent since local midnight; also the audit timestamp. Null until sent.
    email_sent_at = models.DateTimeField(null=True, blank=True, db_index=True)
    # RFC-5322 Message-ID of the sent email. The Layer-2 correlation key: a reply's
    # In-Reply-To/References matches it back to this exact campaign/deal (the
    # disambiguator when one lead is emailed across two campaigns). Null until sent.
    email_message_id = models.CharField(max_length=300, blank=True, default="")
    profile_summary = models.JSONField(null=True, blank=True, default=None)
    chat_summary = models.JSONField(null=True, blank=True, default=None)
    creation_date = models.DateTimeField(default=timezone.now)
    update_date = models.DateTimeField(auto_now=True)

    target_rationale = models.TextField(blank=True)
    verified_personalization_facts = models.JSONField(default=list, blank=True)
    generated_connection_note = models.TextField(blank=True)
    generated_follow_up = models.TextField(blank=True)
    validation_results = models.JSONField(default=dict, blank=True)
    manual_review_status = models.CharField(
        max_length=20, default="pending",
        choices=[("pending", "Pending"), ("approved", "Approved"), ("rejected", "Rejected")]
    )
    reviewer = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    rejection_reason = models.TextField(blank=True)
    send_eligible = models.BooleanField(default=False)
    draft_mode = models.BooleanField(default=True)
    sendable = models.BooleanField(default=False)
    external_action_authorized = models.BooleanField(default=False)

    def clean(self):
        super().clean()
        if self.campaign and self.lead:
            if self.campaign.workspace and self.lead.workspace:
                if self.campaign.workspace != self.lead.workspace:
                    raise ValidationError("Lead and Campaign must belong to the same Workspace.")
            elif self.campaign.workspace and not self.lead.workspace:
                self.lead.workspace = self.campaign.workspace
                if self.lead.pk:
                    self.lead.save(update_fields=["workspace"])
        if self.campaign and self.campaign.is_active:
            conflicting_deals = Deal.objects.filter(
                lead=self.lead,
                campaign__is_active=True
            ).exclude(state__in=[DealState.COMPLETED, DealState.FAILED])
            if self.pk:
                conflicting_deals = conflicting_deals.exclude(pk=self.pk)
            if conflicting_deals.exists():
                raise ValidationError("Lead is already active in another campaign.")
        
        # Check suppression record
        from openoutreach.crm.models.lead import SuppressionRecord
        if self.lead and SuppressionRecord.objects.filter(linkedin_url=self.lead.linkedin_url).exists():
            if (self.manual_review_status != "rejected" and self.state not in [DealState.COMPLETED, DealState.FAILED]) or self.sendable or self.external_action_authorized:
                raise ValidationError("Lead is suppressed.")

        # Enforce failsafe draft mode sandboxing
        if self.draft_mode:
            if self.sendable or self.external_action_authorized:
                raise ValidationError("Draft mode targets cannot be sendable or have external actions authorized.")

        # Validate content style
        from openoutreach.core.content_style import validate_linkedin_outbound_message
        if self.generated_connection_note:
            res = validate_linkedin_outbound_message(
                self.generated_connection_note,
                content_type="connection_note",
                workspace=getattr(self.campaign, "workspace", None),
                prospect_facts=self.lead.to_profile_dict() if self.lead else None,
                deal=self,
            )
            if not res["allowed"]:
                raise ValidationError(f"Style validation failed for connection note: {', '.join(res['errors'])}")
        if self.generated_follow_up:
            res = validate_linkedin_outbound_message(
                self.generated_follow_up,
                content_type="follow_up",
                workspace=getattr(self.campaign, "workspace", None),
                prospect_facts=self.lead.to_profile_dict() if self.lead else None,
                deal=self,
            )
            if not res["allowed"]:
                raise ValidationError(f"Style validation failed for follow-up: {', '.join(res['errors'])}")

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        lead_str = str(self.lead) if self.lead_id else "?"
        return f"{lead_str} [{self.state}]"


class LeadPipelineEvent(models.Model):
    lead = models.ForeignKey("Lead", on_delete=models.CASCADE, related_name="pipeline_events")
    deal = models.ForeignKey("Deal", null=True, blank=True, on_delete=models.SET_NULL, related_name="pipeline_events")
    campaign = models.ForeignKey("core.Campaign", null=True, blank=True, on_delete=models.SET_NULL, related_name="pipeline_events")
    operator = models.ForeignKey("auth.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="pipeline_events")
    action = models.CharField(max_length=100)
    previous_state = models.CharField(max_length=50, blank=True)
    new_state = models.CharField(max_length=50, blank=True)
    note = models.TextField(blank=True)
    metadata = models.JSONField(null=True, blank=True, default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Lead Pipeline Event"
        verbose_name_plural = "Lead Pipeline Events"
        ordering = ["-created_at"]

    def __str__(self):
        return f"Event: {self.action} on {self.lead} at {self.created_at}"
