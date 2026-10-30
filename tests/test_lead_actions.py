# tests/test_lead_actions.py
"""Tests for individual Lead pipeline controls, safety guards, manual audit logs, and status CLI command (Batch 9)."""
from __future__ import annotations

import pytest
from unittest.mock import patch
from django.urls import reverse
from django.utils import timezone
from django.core.management import call_command
from io import StringIO

from openoutreach.core.models import Campaign, Task
from openoutreach.crm.models import Lead, Deal, DealState, Outcome, LeadPipelineEvent
from openoutreach.linkedin.models import LinkedInProfile, ActionLog
from tests.factories import UserFactory

@pytest.mark.django_db
class TestLeadPipelineControls:

    @pytest.fixture(autouse=True)
    def setup_data(self):
        self.staff_user = UserFactory(is_staff=True, is_superuser=False)
        self.non_staff_user = UserFactory(is_staff=False, is_superuser=False)
        
        self.campaign = Campaign.objects.create(name="Lead Action Campaign", is_active=True)
        self.campaign.users.add(self.staff_user)

        self.lead = Lead.objects.create(
            public_identifier="john-doe-123",
            linkedin_url="https://www.linkedin.com/in/john-doe-123",
            first_name="John",
            last_name="Doe",
            company="Acme Corp"
        )
        self.deal = Deal.objects.create(
            lead=self.lead,
            campaign=self.campaign,
            state=DealState.QUALIFIED
        )

    def test_unauthenticated_redirects(self, client):
        """Verify actions require login."""
        url = reverse("dashboard:lead_detail", kwargs={"pk": self.lead.pk})
        resp = client.post(url, {"action": "qualify"})
        assert resp.status_code == 302
        assert "login" in resp.url

    def test_no_get_mutation(self, client):
        """Verify GET requests never mutate deal or lead state."""
        url = reverse("dashboard:lead_detail", kwargs={"pk": self.lead.pk})
        client.force_login(self.staff_user)

        assert self.deal.state == DealState.QUALIFIED
        resp = client.get(url)
        assert resp.status_code == 200

        self.deal.refresh_from_db()
        assert self.deal.state == DealState.QUALIFIED
        assert LeadPipelineEvent.objects.filter(lead=self.lead).count() == 0

    def test_qualify_action(self, client):
        """Verify Mark Qualified transitions state to QUALIFIED and clears outcomes/reasons."""
        self.deal.state = DealState.FAILED
        self.deal.outcome = Outcome.WRONG_FIT
        self.deal.reason = "Unresponsive"
        self.deal.save()

        url = reverse("dashboard:lead_detail", kwargs={"pk": self.lead.pk})
        client.force_login(self.staff_user)

        resp = client.post(url, {"action": "qualify"})
        assert resp.status_code == 302

        self.deal.refresh_from_db()
        assert self.deal.state == DealState.QUALIFIED
        assert self.deal.outcome == ""
        assert self.deal.reason == ""

        # Verify LeadPipelineEvent log
        event = LeadPipelineEvent.objects.filter(lead=self.lead, action="qualify").first()
        assert event is not None
        assert event.previous_state == DealState.FAILED.value
        assert event.new_state == DealState.QUALIFIED.value
        assert event.operator == self.staff_user

    def test_disqualify_action(self, client):
        """Verify Mark Disqualified sets lead.disqualified=True and deal state=FAILED."""
        url = reverse("dashboard:lead_detail", kwargs={"pk": self.lead.pk})
        client.force_login(self.staff_user)

        resp = client.post(url, {"action": "disqualify"})
        assert resp.status_code == 302

        self.lead.refresh_from_db()
        assert self.lead.disqualified is True

        self.deal.refresh_from_db()
        assert self.deal.state == DealState.FAILED
        assert self.deal.outcome == Outcome.WRONG_FIT
        assert self.deal.reason == "Manual operator disqualification"

        event = LeadPipelineEvent.objects.filter(lead=self.lead, action="disqualify").first()
        assert event is not None
        assert event.new_state == DealState.FAILED.value

    def test_ready_to_connect_guard(self, client):
        """Verify Mark Ready to Connect blocks PENDING/CONNECTED states without confirmation."""
        # 1. Transition allowed from QUALIFIED or READY_TO_CONNECT
        url = reverse("dashboard:lead_detail", kwargs={"pk": self.lead.pk})
        client.force_login(self.staff_user)

        self.deal.state = DealState.QUALIFIED
        self.deal.save()

        resp = client.post(url, {"action": "mark_ready_connect"})
        assert resp.status_code == 302
        self.deal.refresh_from_db()
        assert self.deal.state == DealState.READY_TO_CONNECT

        # 2. Block transition if state is PENDING/CONNECTED unless confirmed
        self.deal.state = DealState.PENDING
        self.deal.save()

        resp = client.post(url, {"action": "mark_ready_connect"})
        assert resp.status_code == 302
        self.deal.refresh_from_db()
        assert self.deal.state == DealState.PENDING  # Blocked

        # 3. Confirm with confirm_reset=yes
        resp = client.post(url, {"action": "mark_ready_connect", "confirm_reset": "yes"})
        assert resp.status_code == 302
        self.deal.refresh_from_db()
        assert self.deal.state == DealState.READY_TO_CONNECT  # Allowed

    def test_ready_to_email_guard(self, client):
        """Verify Mark Ready to Email blocks if lead.api_email is empty unless override=yes."""
        url = reverse("dashboard:lead_detail", kwargs={"pk": self.lead.pk})
        client.force_login(self.staff_user)

        # 1. Blocks if email is empty and no override
        self.lead.api_email = ""
        self.lead.save()

        resp = client.post(url, {"action": "mark_ready_email"})
        assert resp.status_code == 302
        self.deal.refresh_from_db()
        assert self.deal.state == DealState.QUALIFIED  # Blocked

        # 2. Succeeds with override=yes
        resp = client.post(url, {"action": "mark_ready_email", "override": "yes"})
        assert resp.status_code == 302
        self.deal.refresh_from_db()
        assert self.deal.state == DealState.READY_TO_EMAIL

        # 3. Succeeds normally if email exists
        self.deal.state = DealState.QUALIFIED
        self.deal.save()
        self.lead.api_email = "test@acme.com"
        self.lead.save()

        resp = client.post(url, {"action": "mark_ready_email"})
        assert resp.status_code == 302
        self.deal.refresh_from_db()
        assert self.deal.state == DealState.READY_TO_EMAIL

    def test_retry_failed_clears_outcomes(self, client):
        """Verify retry_failed clears outcomes/reasons and sets correct state."""
        self.deal.state = DealState.FAILED
        self.deal.outcome = Outcome.WRONG_FIT
        self.deal.reason = "Unresponsive"
        self.deal.save()

        url = reverse("dashboard:lead_detail", kwargs={"pk": self.lead.pk})
        client.force_login(self.staff_user)

        # 1. Email present -> READY_TO_EMAIL
        self.lead.api_email = "resolved@acme.com"
        self.lead.save()

        resp = client.post(url, {"action": "retry_failed"})
        assert resp.status_code == 302
        self.deal.refresh_from_db()
        assert self.deal.state == DealState.READY_TO_EMAIL
        assert self.deal.outcome == ""
        assert self.deal.reason == ""

        # 2. Email absent -> QUALIFIED
        self.deal.state = DealState.FAILED
        self.deal.outcome = Outcome.WRONG_FIT
        self.deal.reason = "Unresponsive"
        self.deal.save()
        self.lead.api_email = ""
        self.lead.save()

        resp = client.post(url, {"action": "retry_failed"})
        assert resp.status_code == 302
        self.deal.refresh_from_db()
        assert self.deal.state == DealState.QUALIFIED
        assert self.deal.outcome == ""
        assert self.deal.reason == ""

    def test_reset_qualified_retains_deal_no_deletion(self, client):
        """Verify reset_qualified sets state to QUALIFIED, resets stats, and does not delete Deal row."""
        self.deal.state = DealState.FAILED
        self.deal.outcome = Outcome.WRONG_FIT
        self.deal.reason = "Bounced"
        self.deal.connect_attempts = 3
        self.deal.backoff_hours = 24
        self.deal.save()

        url = reverse("dashboard:lead_detail", kwargs={"pk": self.lead.pk})
        client.force_login(self.staff_user)

        deal_count_before = Deal.objects.count()

        resp = client.post(url, {"action": "reset_qualified"})
        assert resp.status_code == 302

        # Verify Deal counts and row still exist
        assert Deal.objects.count() == deal_count_before
        self.deal.refresh_from_db()
        assert self.deal.state == DealState.QUALIFIED
        assert self.deal.outcome == ""
        assert self.deal.reason == ""
        assert self.deal.connect_attempts == 0
        assert self.deal.backoff_hours == 0

        event = LeadPipelineEvent.objects.filter(lead=self.lead, action="reset_qualified").first()
        assert event is not None

    def test_self_profile_lockout(self, client):
        """Verify operator/system self-profile leads are locked from manual mutation."""
        profile = LinkedInProfile.objects.create(
            user=self.staff_user,
            linkedin_username="john-doe-123",
            active=True,
            self_lead=self.lead
        )

        url = reverse("dashboard:lead_detail", kwargs={"pk": self.lead.pk})
        client.force_login(self.staff_user)

        self.deal.state = DealState.QUALIFIED
        self.deal.save()

        # Try to mark disqualified
        resp = client.post(url, {"action": "disqualify"})
        assert resp.status_code == 302

        # Verify lead state did not change
        self.lead.refresh_from_db()
        assert self.lead.disqualified is False
        self.deal.refresh_from_db()
        assert self.deal.state == DealState.QUALIFIED

    def test_no_outreach_execution(self, client):
        """Verify manual actions NEVER launch browser sessions or send messages/emails."""
        url = reverse("dashboard:lead_detail", kwargs={"pk": self.lead.pk})
        client.force_login(self.staff_user)

        with patch("openoutreach.linkedin.browser.session.AccountSession.ensure_browser") as mock_pw, \
             patch("openoutreach.emails.sender.send_email") as mock_email_sender:
            
            # Run qualify action
            client.post(url, {"action": "qualify"})
            # Run disqualify action
            client.post(url, {"action": "disqualify"})
            # Run reset action
            client.post(url, {"action": "reset_qualified"})

            # Verify no browser or outreach executions were triggered
            mock_pw.assert_not_called()
            mock_email_sender.assert_not_called()

    def test_leads_list_filtering(self, client):
        """Verify leads list filters function correctly."""
        # Create second lead/deal for comparison
        other_lead = Lead.objects.create(
            public_identifier="jane-doe-456",
            linkedin_url="https://www.linkedin.com/in/jane-doe-456",
            api_email="jane@acme.com"
        )
        other_deal = Deal.objects.create(
            lead=other_lead,
            campaign=self.campaign,
            state=DealState.FAILED
        )

        list_url = reverse("dashboard:leads")
        client.force_login(self.staff_user)

        # 1. Email filter
        resp = client.get(list_url, {"email_filter": "has_email"})
        assert resp.status_code == 200
        # only jane has email
        assert b"jane-doe-456" in resp.content
        assert b"john-doe-123" not in resp.content

        # 2. Deal filter (discovered_only vs has_deal)
        discovered_lead = Lead.objects.create(
            public_identifier="discovered-lead",
            linkedin_url="https://www.linkedin.com/in/discovered"
        )
        resp = client.get(list_url, {"deal_filter": "discovered_only"})
        assert resp.status_code == 200
        assert b"discovered-lead" in resp.content
        assert b"john-doe-123" not in resp.content

        # 3. Failed only
        resp = client.get(list_url, {"failed_only": "yes"})
        assert resp.status_code == 200
        assert b"jane-doe-456" in resp.content
        assert b"john-doe-123" not in resp.content

    def test_lead_pipeline_status_command(self):
        """Verify lead_pipeline_status CLI command runs, filters, and prints no secrets."""
        out = StringIO()
        # Add a pipeline event
        LeadPipelineEvent.objects.create(
            lead=self.lead,
            deal=self.deal,
            campaign=self.campaign,
            operator=self.staff_user,
            action="qualify",
            previous_state="Discovered",
            new_state=DealState.QUALIFIED.value,
            note="Test event"
        )

        # Run command
        call_command("lead_pipeline_status", stdout=out)
        output = out.getvalue()

        # Verify output
        assert "CRITICAL CRM FUNNEL COUNT SUMMARY" in output
        assert "john-doe-123" in output
        assert "Lead Action Campaign" in output
        assert f"qualify by {self.staff_user.username}" in output
        
        # Verify no credentials or API keys exist in output
        assert "sk-" not in output
        assert "password" not in output
