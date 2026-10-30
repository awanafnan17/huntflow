# openoutreach/dashboard/forms.py
from django import forms
from django.contrib.auth.models import User
from openoutreach.core.models import Campaign, SiteConfig
from openoutreach.linkedin.models import LinkedInProfile
from openoutreach.emails.models import Mailbox


class CampaignForm(forms.ModelForm):
    connect_unlimited = forms.BooleanField(
        required=False,
        label="Unlimited connections",
        help_text="Removes the daily connection cap while preserving pacing, scheduling, duplicate checks, and safety controls."
    )
    follow_up_unlimited = forms.BooleanField(
        required=False,
        label="Unlimited follow-ups",
        help_text="Removes the daily follow-up cap while preserving pacing, scheduling, duplicate checks, and safety controls."
    )

    class Meta:
        model = Campaign
        fields = [
            "name",
            "is_active",
            "campaign_type",
            "owner",
            "linkedin_profile",
            "target_audience_definition",
            "target_job_titles",
            "target_seniority",
            "target_industries",
            "target_locations",
            "target_company_size",
            "target_keywords",
            "require_title_match",
            "require_seniority_match",
            "require_location_match",
            "require_industry_match",
            "require_company_size_match",
            "approved_offer",
            "target_data_source",
            "message_template_family",
            "follow_up_template_family",
            "minimum_delay",
            "max_follow_ups",
            "active_time_window",
            "manual_approval_required",
            "users",
            "product_docs",
            "campaign_objective",
            "booking_link",
            "action_fraction",
            "daily_connect_limit",
            "daily_follow_up_limit",
            "connection_note_enabled",
            "connection_note_template",
            "connection_note_instructions",
        ]
        widgets = {
            "name": forms.TextInput(attrs={"class": "input-text"}),
            "campaign_type": forms.Select(attrs={"class": "input-text"}),
            "owner": forms.Select(attrs={"class": "input-text"}),
            "target_audience_definition": forms.Textarea(attrs={"class": "input-text", "rows": 3}),
            "target_job_titles": forms.TextInput(attrs={"class": "input-text", "placeholder": "e.g. CEO, Founder, VP Sales"}),
            "target_seniority": forms.TextInput(attrs={"class": "input-text", "placeholder": "e.g. Executive, Director"}),
            "target_industries": forms.TextInput(attrs={"class": "input-text", "placeholder": "e.g. B2B SaaS, Software"}),
            "target_locations": forms.TextInput(attrs={"class": "input-text", "placeholder": "e.g. United States, Remote"}),
            "target_company_size": forms.TextInput(attrs={"class": "input-text", "placeholder": "e.g. 11-50, 51-200"}),
            "target_keywords": forms.TextInput(attrs={"class": "input-text", "placeholder": "e.g. AI outreach, B2B sales"}),
            "approved_offer": forms.Textarea(attrs={"class": "input-text", "rows": 3}),
            "target_data_source": forms.TextInput(attrs={"class": "input-text"}),
            "message_template_family": forms.Textarea(attrs={"class": "input-text", "rows": 3}),
            "follow_up_template_family": forms.Textarea(attrs={"class": "input-text", "rows": 3}),
            "minimum_delay": forms.NumberInput(attrs={"class": "input-text", "min": "0"}),
            "max_follow_ups": forms.NumberInput(attrs={"class": "input-text", "min": "0"}),
            "active_time_window": forms.TextInput(attrs={"class": "input-text"}),
            "manual_approval_required": forms.CheckboxInput(),
            "booking_link": forms.URLInput(attrs={"class": "input-text"}),
            "action_fraction": forms.NumberInput(attrs={"class": "input-text", "step": "0.01", "min": "0", "max": "1"}),
            "daily_connect_limit": forms.NumberInput(attrs={"class": "input-text", "min": "1"}),
            "daily_follow_up_limit": forms.NumberInput(attrs={"class": "input-text", "min": "1"}),
            "product_docs": forms.Textarea(attrs={"class": "input-text", "rows": 4}),
            "campaign_objective": forms.Textarea(attrs={"class": "input-text", "rows": 4}),
            "connection_note_template": forms.Textarea(attrs={"class": "input-text", "rows": 3}),
            "connection_note_instructions": forms.Textarea(attrs={"class": "input-text", "rows": 3}),
            "users": forms.CheckboxSelectMultiple(),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["daily_connect_limit"].required = False
        self.fields["daily_follow_up_limit"].required = False
        self.fields["campaign_type"].required = False
        self.fields["minimum_delay"].required = False
        self.fields["max_follow_ups"].required = False
        self.fields["active_time_window"].required = False
        
        # When editing, default checkbox state to True if stored limit is None
        if self.instance and self.instance.pk:
            self.fields["connect_unlimited"].initial = (self.instance.daily_connect_limit is None)
            self.fields["follow_up_unlimited"].initial = (self.instance.daily_follow_up_limit is None)

    def clean(self):
        cleaned_data = super().clean()
        connect_unlimited = cleaned_data.get("connect_unlimited")
        connect_limit = cleaned_data.get("daily_connect_limit")
        follow_up_unlimited = cleaned_data.get("follow_up_unlimited")
        follow_up_limit = cleaned_data.get("daily_follow_up_limit")

        # Connection Limit Validation
        if connect_unlimited:
            cleaned_data["daily_connect_limit"] = None
        else:
            if connect_limit is None:
                self.add_error("daily_connect_limit", "A daily limit value is required unless Unlimited is checked.")
            elif connect_limit < 1:
                self.add_error("daily_connect_limit", "Daily limit must be 1 or greater.")

        # Follow-up Limit Validation
        if follow_up_unlimited:
            cleaned_data["daily_follow_up_limit"] = None
        else:
            if follow_up_limit is None:
                self.add_error("daily_follow_up_limit", "A daily limit value is required unless Unlimited is checked.")
            elif follow_up_limit < 1:
                self.add_error("daily_follow_up_limit", "Daily limit must be 1 or greater.")

        return cleaned_data


class LinkedInProfileForm(forms.ModelForm):
    linkedin_password = forms.CharField(
        widget=forms.PasswordInput(render_value=False, attrs={"class": "input-text"}),
        required=False,
        help_text="Leave blank to keep existing secret. Enter a new value to replace it."
    )
    connect_unlimited = forms.BooleanField(
        required=False,
        label="Unlimited connections",
        help_text="Removes the daily connection cap while preserving pacing, scheduling, duplicate checks, and safety controls."
    )
    follow_up_unlimited = forms.BooleanField(
        required=False,
        label="Unlimited follow-ups",
        help_text="Removes the daily follow-up cap while preserving pacing, scheduling, duplicate checks, and safety controls."
    )

    class Meta:
        model = LinkedInProfile
        fields = [
            "linkedin_username",
            "linkedin_password",
            "active",
            "connect_daily_limit",
            "follow_up_daily_limit",
        ]
        widgets = {
            "linkedin_username": forms.TextInput(attrs={"class": "input-text"}),
            "connect_daily_limit": forms.NumberInput(attrs={"class": "input-text", "min": "1"}),
            "follow_up_daily_limit": forms.NumberInput(attrs={"class": "input-text", "min": "1"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["connect_daily_limit"].required = False
        self.fields["follow_up_daily_limit"].required = False

        # When editing, default checkbox state to True if stored limit is None
        if self.instance and self.instance.pk:
            self.fields["connect_unlimited"].initial = (self.instance.connect_daily_limit is None)
            self.fields["follow_up_unlimited"].initial = (self.instance.follow_up_daily_limit is None)

    def clean(self):
        cleaned_data = super().clean()
        connect_unlimited = cleaned_data.get("connect_unlimited")
        connect_limit = cleaned_data.get("connect_daily_limit")
        follow_up_unlimited = cleaned_data.get("follow_up_unlimited")
        follow_up_limit = cleaned_data.get("follow_up_daily_limit")

        # Connection Limit Validation
        if connect_unlimited:
            cleaned_data["connect_daily_limit"] = None
        else:
            if connect_limit is None:
                self.add_error("connect_daily_limit", "A daily limit value is required unless Unlimited is checked.")
            elif connect_limit < 1:
                self.add_error("connect_daily_limit", "Daily limit must be 1 or greater.")

        # Follow-up Limit Validation
        if follow_up_unlimited:
            cleaned_data["follow_up_daily_limit"] = None
        else:
            if follow_up_limit is None:
                self.add_error("follow_up_daily_limit", "A daily limit value is required unless Unlimited is checked.")
            elif follow_up_limit < 1:
                self.add_error("follow_up_daily_limit", "Daily limit must be 1 or greater.")

        return cleaned_data


class MailboxForm(forms.ModelForm):
    password = forms.CharField(
        widget=forms.PasswordInput(render_value=False, attrs={"class": "input-text"}),
        required=False,
        help_text="Leave blank to keep existing secret. Enter a new value to replace it."
    )

    class Meta:
        model = Mailbox
        fields = [
            "host",
            "port",
            "username",
            "password",
            "from_address",
            "daily_limit",
        ]
        widgets = {
            "host": forms.TextInput(attrs={"class": "input-text"}),
            "port": forms.NumberInput(attrs={"class": "input-text", "min": "1"}),
            "username": forms.TextInput(attrs={"class": "input-text"}),
            "from_address": forms.EmailInput(attrs={"class": "input-text"}),
            "daily_limit": forms.NumberInput(attrs={"class": "input-text", "min": "0"}),
        }


class LLMConfigForm(forms.ModelForm):
    llm_api_key = forms.CharField(
        widget=forms.PasswordInput(render_value=False, attrs={"class": "input-text"}),
        required=False,
        help_text="Leave blank to keep existing secret. Enter a new value to replace it."
    )

    class Meta:
        model = SiteConfig
        fields = [
            "ai_model",
            "llm_api_base",
            "llm_api_key",
        ]
        widgets = {
            "ai_model": forms.TextInput(attrs={"class": "input-text"}),
            "llm_api_base": forms.TextInput(attrs={"class": "input-text"}),
        }


class BetterContactConfigForm(forms.ModelForm):
    bettercontact_api_key = forms.CharField(
        widget=forms.PasswordInput(render_value=False, attrs={"class": "input-text"}),
        required=False,
        help_text="Leave blank to keep existing secret. Enter a new value to replace it."
    )

    class Meta:
        model = SiteConfig
        fields = [
            "bettercontact_api_key",
        ]
