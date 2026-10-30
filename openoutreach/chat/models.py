from django.conf import settings
from django.db import models
from django.template.defaultfilters import truncatechars
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.urls import reverse


class ChatMessage(models.Model):
    class Meta:
        verbose_name = _("message")
        verbose_name_plural = _("messages")
        constraints = [
            models.UniqueConstraint(
                fields=["deal", "linkedin_urn"],
                name="uniq_deal_linkedin_urn",
            ),
        ]

    deal = models.ForeignKey(
        "crm.Deal",
        on_delete=models.CASCADE,
        related_name="messages",
        verbose_name=_("Deal"),
    )
    content = models.TextField(blank=True, default='', verbose_name=_("Message"))
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, blank=True, null=True, on_delete=models.CASCADE,
        verbose_name=_("Owner"), related_name="%(app_label)s_%(class)s_owner_related",
    )
    answer_to = models.ForeignKey(
        'self', blank=True, null=True, on_delete=models.CASCADE,
        related_name="%(app_label)s_%(class)s_answer_to_related", verbose_name=_("answer to")
    )
    topic = models.ForeignKey(
        'self', blank=True, null=True, on_delete=models.CASCADE,
        related_name="%(app_label)s_%(class)s_topic_related",
    )
    creation_date = models.DateTimeField(default=timezone.now, verbose_name=_("Creation date"))
    linkedin_urn = models.CharField(
        max_length=300, verbose_name=_("LinkedIn message URN"),
        help_text=_("entityUrn from Voyager API, used for dedup (per deal)"),
    )
    is_outgoing = models.BooleanField(
        default=True, verbose_name=_("Outgoing"),
        help_text=_("True if sent by us, False if received"),
    )
    def __str__(self):
        return f'{truncatechars(self.content, 70)}'

    def get_absolute_url(self):
        return reverse(f'admin:chat_{self._meta.model_name}_change', args=[str(self.id)])


class AgentConversation(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="agent_conversations")
    title = models.CharField(max_length=255, default="New Campaign Brief")
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)
    is_active = models.BooleanField(default=True)

    onboarding_state = models.CharField(max_length=50, default="JOB_ONBOARDING_NOT_STARTED")
    onboarding_data = models.JSONField(default=dict, blank=True)

    def __str__(self):
        return f"Conversation {self.id}: {self.title}"


class AgentMessage(models.Model):
    ROLE_CHOICES = [("user", "User"), ("agent", "Agent"), ("system", "System")]
    conversation = models.ForeignKey(AgentConversation, on_delete=models.CASCADE, related_name="messages")
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default="user")
    content = models.TextField()
    timestamp = models.DateTimeField(default=timezone.now)

    def __str__(self):
        return f"{self.role.upper()} ({self.timestamp.strftime('%H:%M')}): {truncatechars(self.content, 50)}"


class CampaignBrief(models.Model):
    conversation = models.OneToOneField(AgentConversation, on_delete=models.CASCADE, related_name="brief")
    campaign = models.OneToOneField("core.Campaign", on_delete=models.SET_NULL, null=True, blank=True, related_name="brief")
    campaign_name = models.CharField(max_length=255, default="New Campaign")
    campaign_type = models.CharField(max_length=50, default="linkedin_networking") # linkedin_networking, lead_generation, email_outreach, job_application, hybrid
    objective = models.TextField(blank=True)
    offer = models.TextField(blank=True)
    target_regions = models.JSONField(default=list)
    target_cities = models.JSONField(default=list)
    excluded_regions = models.JSONField(default=list)
    audience_titles = models.JSONField(default=list)
    industries = models.JSONField(default=list)
    company_sizes = models.JSONField(default=list)
    keywords = models.JSONField(default=list)
    excluded_keywords = models.JSONField(default=list)
    channels = models.JSONField(default=list)
    daily_limits = models.JSONField(default=dict)
    approval_mode = models.CharField(max_length=50, default="first_action_approval")
    is_confirmed = models.BooleanField(default=False)
    created_at = models.DateTimeField(default=timezone.now)

    def __str__(self):
        return f"Brief: {self.campaign_name} ({self.campaign_type})"


class ConnectedGoogleAccount(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="google_accounts")
    email = models.EmailField(unique=True)
    display_name = models.CharField(max_length=255, blank=True)
    access_token = models.TextField(blank=True)
    refresh_token = models.TextField(blank=True)
    token_uri = models.CharField(max_length=255, default="https://oauth2.googleapis.com/token")
    client_id = models.CharField(max_length=255, blank=True)
    client_secret = models.CharField(max_length=255, blank=True)
    scopes = models.JSONField(default=list)
    is_active = models.BooleanField(default=True)
    connected_at = models.DateTimeField(default=timezone.now)
    last_synced_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"Google Account: {self.email}"


class GmailThread(models.Model):
    google_account = models.ForeignKey(ConnectedGoogleAccount, on_delete=models.CASCADE, related_name="threads")
    thread_id = models.CharField(max_length=255, unique=True)
    subject = models.CharField(max_length=500, blank=True)
    lead = models.ForeignKey("crm.Lead", on_delete=models.SET_NULL, null=True, blank=True, related_name="gmail_threads")
    has_replied = models.BooleanField(default=False)
    last_message_at = models.DateTimeField(default=timezone.now)

    def __str__(self):
        return f"Thread {self.thread_id}: {self.subject}"


class GmailMessage(models.Model):
    thread = models.ForeignKey(GmailThread, on_delete=models.CASCADE, related_name="messages")
    message_id = models.CharField(max_length=255, unique=True)
    sender_email = models.EmailField()
    recipient_email = models.EmailField()
    subject = models.CharField(max_length=500, blank=True)
    body_text = models.TextField(blank=True)
    is_incoming = models.BooleanField(default=False)
    sent_at = models.DateTimeField(default=timezone.now)

    def __str__(self):
        return f"GmailMsg {self.message_id} from {self.sender_email}"


class LeadImport(models.Model):
    workspace = models.ForeignKey("core.Workspace", on_delete=models.CASCADE, related_name="csv_lead_imports")
    file_name = models.CharField(max_length=255)
    uploaded_at = models.DateTimeField(default=timezone.now)
    total_rows = models.IntegerField(default=0)
    successful_rows = models.IntegerField(default=0)
    failed_rows = models.IntegerField(default=0)
    status = models.CharField(max_length=50, default="completed")

    def __str__(self):
        return f"Import {self.file_name} ({self.total_rows} rows)"


class LeadImportRow(models.Model):
    lead_import = models.ForeignKey(LeadImport, on_delete=models.CASCADE, related_name="rows")
    row_number = models.IntegerField()
    email = models.EmailField(blank=True)
    first_name = models.CharField(max_length=100, blank=True)
    last_name = models.CharField(max_length=100, blank=True)
    company = models.CharField(max_length=255, blank=True)
    job_title = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=50, default="imported")
    error_message = models.TextField(blank=True)

    def __str__(self):
        return f"Row {self.row_number}: {self.email or self.company}"

