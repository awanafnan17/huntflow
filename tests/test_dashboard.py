# tests/test_dashboard.py
import pytest
from django.urls import reverse
from tests.factories import UserFactory
from openoutreach.core.models import Campaign, Task, SiteConfig
from openoutreach.crm.models import Lead, Deal, DealState
from openoutreach.linkedin.models import LinkedInProfile, ActionLog
from openoutreach.emails.models import Mailbox


@pytest.mark.django_db
class TestDashboardSecurity:
    """Verify that all dashboard URLs require authentication and redirect to login."""

    @pytest.mark.parametrize(
        "url_name,kwargs",
        [
            ("dashboard:overview", {}),
            ("dashboard:campaigns", {}),
            ("dashboard:leads", {}),
            ("dashboard:tasks", {}),
            ("dashboard:action_logs", {}),
            ("dashboard:linkedin_accounts", {}),
            ("dashboard:mailboxes", {}),
            ("dashboard:settings_llm", {}),
            ("dashboard:settings_bettercontact", {}),
            ("dashboard:settings_system", {}),
            ("dashboard:daemon", {}),
            ("dashboard:diagnostics", {}),
        ]
    )
    def test_redirects_unauthenticated_user(self, client, url_name, kwargs):
        url = reverse(url_name, kwargs=kwargs)
        response = client.get(url)
        assert response.status_code == 302
        assert "/dashboard/login/" in response.url


@pytest.mark.django_db
class TestDashboardViews:
    """Verify that dashboard views load successfully for authenticated users and render real database data."""

    @pytest.fixture(autouse=True)
    def _setup(self, client, db):
        self.user = UserFactory(username="saasoperator")
        client.force_login(self.user)
        self.client = client

    def test_overview_loads_with_metrics(self):
        # Seed test data
        campaign = Campaign.objects.create(name="Austin Marketing Agency", is_freemium=False)
        lead = Lead.objects.create(public_identifier="jane-doe", linkedin_url="https://linkedin.com/in/jane-doe")
        deal = Deal.objects.create(lead=lead, campaign=campaign, state=DealState.CONNECTED)
        
        response = self.client.get(reverse("dashboard:overview"))
        assert response.status_code == 200
        content = response.content.decode("utf-8")
        
        # Verify branding and layout standards
        assert "HuntFlow" in content
        assert "OpenOutreach" not in content
        import re
        content_no_comments = re.sub(r"<!--.*?-->", "", content, flags=re.DOTALL)
        assert "--" not in content_no_comments  # no decorative separators

        # Verify database metrics are rendered
        assert "Master Automation" in content
        assert "LinkedIn Accounts" in content

    def test_campaigns_page_loads_data(self):
        Campaign.objects.create(name="Austin Marketing Agency", is_freemium=False)
        response = self.client.get(reverse("dashboard:campaigns"))
        assert response.status_code == 200
        content = response.content.decode("utf-8")
        assert "Austin Marketing Agency" in content
        assert "OpenOutreach" not in content

    def test_leads_page_loads_and_searches(self):
        lead1 = Lead.objects.create(public_identifier="jane-doe", linkedin_url="https://linkedin.com/in/jane-doe")
        lead2 = Lead.objects.create(public_identifier="john-smith", linkedin_url="https://linkedin.com/in/john-smith")
        
        # Test basic list
        response = self.client.get(reverse("dashboard:leads"))
        assert response.status_code == 200
        content = response.content.decode("utf-8")
        assert "jane-doe" in content
        assert "john-smith" in content

        # Test search filter
        response = self.client.get(reverse("dashboard:leads") + "?q=smith")
        assert response.status_code == 200
        content = response.content.decode("utf-8")
        assert "john-smith" in content
        assert "jane-doe" not in content

    def test_tasks_page_loads_and_filters(self):
        task1 = Task.objects.create(
            task_type=Task.TaskType.CONNECT,
            status=Task.Status.PENDING,
            scheduled_at=timezone_now(),
            payload={"campaign_id": 99}
        )
        task2 = Task.objects.create(
            task_type=Task.TaskType.FOLLOW_UP,
            status=Task.Status.FAILED,
            scheduled_at=timezone_now(),
            payload={"campaign_id": 100}
        )

        # Test basic listing
        response = self.client.get(reverse("dashboard:tasks"))
        assert response.status_code == 200
        content = response.content.decode("utf-8")
        assert "connect" in content
        assert "follow_up" in content

        # Test status filter
        response = self.client.get(reverse("dashboard:tasks") + "?status=pending")
        assert response.status_code == 200
        # Inspect view context list directly to avoid collision with select dropdown choices in HTML
        tasks_in_context = response.context["tasks"]
        assert len(tasks_in_context) == 1
        assert tasks_in_context[0]["task"].task_type == Task.TaskType.CONNECT


    def test_action_logs_page_loads(self):
        lp = LinkedInProfile.objects.create(user=self.user, linkedin_username="lp_user", linkedin_password="lp_password")
        campaign = Campaign.objects.create(name="Austin Marketing Agency", is_freemium=False)
        ActionLog.objects.create(
            linkedin_profile=lp,
            campaign=campaign,
            action_type=ActionLog.ActionType.CONNECT,
            metadata={"note_source": "template", "note_was_sent": True, "note_length": 120}
        )

        response = self.client.get(reverse("dashboard:action_logs"))
        assert response.status_code == 200
        content = response.content.decode("utf-8")
        assert "connect" in content
        assert "template" in content
        assert "Yes" in content
        assert "120 chars" in content

    def test_linkedin_accounts_page_loads(self):
        LinkedInProfile.objects.create(user=self.user, linkedin_username="candidate-11@example.com", linkedin_password="secret_password")
        response = self.client.get(reverse("dashboard:linkedin_accounts"))
        assert response.status_code == 200
        content = response.content.decode("utf-8")
        assert "candidate-11@example.com" in content
        assert "secret_password" not in content  # must mask credentials

    def test_mailboxes_page_loads(self):
        Mailbox.objects.create(host="smtp.gmail.com", port=587, username="candidate-4@example.com", password="app_password", from_address="candidate-4@example.com")
        response = self.client.get(reverse("dashboard:mailboxes"))
        assert response.status_code == 200
        content = response.content.decode("utf-8")
        assert "candidate-4@example.com" in content
        assert "app_password" not in content  # credentials masked

    def test_settings_pages_load(self):
        # LLM settings
        response = self.client.get(reverse("dashboard:settings_llm"))
        assert response.status_code == 200
        content = response.content.decode("utf-8")
        assert "LLM Configuration" in content

        # BetterContact settings
        response = self.client.get(reverse("dashboard:settings_bettercontact"))
        assert response.status_code == 200
        content = response.content.decode("utf-8")
        assert "BetterContact API" in content

        # System settings
        response = self.client.get(reverse("dashboard:settings_system"))
        assert response.status_code == 200
        content = response.content.decode("utf-8")
        assert "General System Settings" in content

    def test_daemon_page_loads(self):
        response = self.client.get(reverse("dashboard:daemon"))
        assert response.status_code == 200
        content = response.content.decode("utf-8")
        assert "Daemon Status" in content

    def test_non_staff_redirects(self):
        # Log out current staff user
        self.client.logout()
        # Create non-staff user
        non_staff = UserFactory(username="normaluser", is_staff=False)
        self.client.force_login(non_staff)
        
        response = self.client.get(reverse("dashboard:overview"))
        assert response.status_code == 302
        assert "/dashboard/login/" in response.url

    def test_campaign_crud_ops(self):
        # 1. Create
        create_url = reverse("dashboard:campaign_create")
        post_data = {
            "name": "SaaS Launch Campaign",
            "product_docs": "HuntFlow handles SaaS outbound.",
            "campaign_objective": "Get bookings.",
            "booking_link": "https://calendar.com/meet",
            "action_fraction": 0.35,
            "connection_note_enabled": True,
            "connection_note_template": "Hello {first_name}",
            "connection_note_instructions": "Keep it casual.",
            "connect_unlimited": True,
            "follow_up_unlimited": True,
            "users": [self.user.id],
        }
        response = self.client.post(create_url, post_data)
        assert response.status_code == 302
        
        campaign = Campaign.objects.get(name="SaaS Launch Campaign")
        assert campaign.product_docs == "HuntFlow handles SaaS outbound."
        assert campaign.action_fraction == 0.35
        assert campaign.connection_note_enabled is True
        
        # 2. Edit
        edit_url = reverse("dashboard:campaign_edit", kwargs={"pk": campaign.pk})
        post_data["booking_link"] = "https://calendar.com/meet-new"
        response = self.client.post(edit_url, post_data)
        assert response.status_code == 302
        campaign.refresh_from_db()
        assert campaign.booking_link == "https://calendar.com/meet-new"
        
        # Create a task for this campaign to verify cleanup
        task = Task.objects.create(
            task_type=Task.TaskType.CONNECT,
            status=Task.Status.PENDING,
            scheduled_at=timezone_now(),
            payload={"campaign_id": campaign.id}
        )
        
        # 3. Delete Confirmation Load
        delete_url = reverse("dashboard:campaign_delete", kwargs={"pk": campaign.pk})
        response = self.client.get(delete_url)
        assert response.status_code == 200
        
        # 4. Delete POST
        response = self.client.post(delete_url)
        assert response.status_code == 302
        assert not Campaign.objects.filter(pk=campaign.pk).exists()
        assert not Task.objects.filter(pk=task.pk).exists()  # safely deleted task

    def test_settings_llm_save(self):
        cfg = SiteConfig.load()
        cfg.llm_api_key = "original-secret-key-12345"
        cfg.save()
        
        # GET masking status
        url = reverse("dashboard:settings_llm")
        response = self.client.get(url)
        assert response.status_code == 200
        content = response.content.decode("utf-8")
        assert "original-secret-key-12345" not in content
        assert "orig********2345" in content or "Configured:" in content
        
        # POST blank preserves existing key
        post_data = {
            "ai_model": "openai:gpt-4o",
            "llm_api_base": "https://api.openai.com/v1",
            "llm_api_key": "",
        }
        response = self.client.post(url, post_data)
        assert response.status_code == 302
        cfg.refresh_from_db()
        assert cfg.llm_api_key == "original-secret-key-12345"
        assert cfg.ai_model == "openai:gpt-4o"
        
        # POST non-blank updates key
        post_data["llm_api_key"] = "brand-new-secret-key-999"
        response = self.client.post(url, post_data)
        assert response.status_code == 302
        cfg.refresh_from_db()
        assert cfg.llm_api_key == "brand-new-secret-key-999"

    def test_settings_bettercontact_save(self):
        cfg = SiteConfig.load()
        cfg.bettercontact_api_key = "original-bc-key-abc"
        cfg.save()
        
        url = reverse("dashboard:settings_bettercontact")
        
        # POST blank preserves existing key
        response = self.client.post(url, {"bettercontact_api_key": ""})
        assert response.status_code == 302
        cfg.refresh_from_db()
        assert cfg.bettercontact_api_key == "original-bc-key-abc"
        
        # POST non-blank updates key
        response = self.client.post(url, {"bettercontact_api_key": "new-bc-key-xyz"})
        assert response.status_code == 302
        cfg.refresh_from_db()
        assert cfg.bettercontact_api_key == "new-bc-key-xyz"

    def test_linkedin_account_edit(self):
        lp = LinkedInProfile.objects.create(
            user=self.user,
            linkedin_username="johndoe",
            linkedin_password="original-password-123"
        )
        url = reverse("dashboard:linkedin_account_edit", kwargs={"pk": lp.pk})
        
        # POST blank preserves password
        post_data = {
            "linkedin_username": "johndoe",
            "linkedin_password": "",
            "active": True,
            "connect_daily_limit": 15,
            "follow_up_daily_limit": 20,
        }
        response = self.client.post(url, post_data)
        assert response.status_code == 302
        lp.refresh_from_db()
        assert lp.get_decrypted_password() == "original-password-123"
        assert lp.connect_daily_limit == 15
        
        # POST non-blank updates password
        post_data["linkedin_password"] = "new-secure-password"
        response = self.client.post(url, post_data)
        assert response.status_code == 302
        lp.refresh_from_db()
        assert lp.get_decrypted_password() == "new-secure-password"

    def test_mailbox_crud_ops(self):
        # 1. Create
        create_url = reverse("dashboard:mailbox_create")
        post_data = {
            "host": "smtp.mail.com",
            "port": 587,
            "username": "smtpuser",
            "password": "initial-secret-password",
            "from_address": "smtpuser@mail.com",
            "daily_limit": 100,
        }
        response = self.client.post(create_url, post_data)
        assert response.status_code == 302
        
        mailbox = Mailbox.objects.get(username="smtpuser")
        assert mailbox.host == "smtp.mail.com"
        assert mailbox.password == "initial-secret-password"
        
        # 2. Edit blank password preserves it
        edit_url = reverse("dashboard:mailbox_edit", kwargs={"pk": mailbox.pk})
        post_data["password"] = ""
        post_data["daily_limit"] = 120
        response = self.client.post(edit_url, post_data)
        assert response.status_code == 302
        mailbox.refresh_from_db()
        assert mailbox.password == "initial-secret-password"
        assert mailbox.daily_limit == 120
        
        # 3. Edit non-blank password updates it
        post_data["password"] = "changed-secret-password"
        response = self.client.post(edit_url, post_data)
        assert response.status_code == 302
        mailbox.refresh_from_db()
        assert mailbox.password == "changed-secret-password"
        
        # 4. Delete Confirmation
        delete_url = reverse("dashboard:mailbox_delete", kwargs={"pk": mailbox.pk})
        response = self.client.get(delete_url)
        assert response.status_code == 200
        
        # 5. Delete POST
        response = self.client.post(delete_url)
        assert response.status_code == 302
        assert not Mailbox.objects.filter(pk=mailbox.pk).exists()

    def test_lead_detail_view(self):
        campaign = Campaign.objects.create(name="Austin Marketing Agency", is_freemium=False)
        lead = Lead.objects.create(
            public_identifier="jane-doe",
            linkedin_url="https://linkedin.com/in/jane-doe",
            api_email="jane@doe.com"
        )
        deal = Deal.objects.create(
            lead=lead,
            campaign=campaign,
            state=DealState.CONNECTED,
            profile_summary={"facts": ["CEO at Acme Corp", "Role is CEO", "Based in Austin"]}
        )
        
        lp = LinkedInProfile.objects.create(user=self.user, linkedin_username="lp_user", linkedin_password="lp_password")
        
        # Log entry matching this lead
        ActionLog.objects.create(
            linkedin_profile=lp,
            campaign=campaign,
            action_type=ActionLog.ActionType.CONNECT,
            metadata={"lead_public_id": "jane-doe", "note_was_sent": True, "note": "Hi Jane"}
        )
        # Log entry for another lead (should not appear)
        ActionLog.objects.create(
            linkedin_profile=lp,
            campaign=campaign,
            action_type=ActionLog.ActionType.CONNECT,
            metadata={"lead_public_id": "other-lead", "note_was_sent": True, "note": "Hi other"}
        )
        
        url = reverse("dashboard:lead_detail", kwargs={"pk": lead.pk})
        response = self.client.get(url)
        assert response.status_code == 200
        content = response.content.decode("utf-8")
        
        assert "jane@doe.com" in content
        assert "CEO at Acme Corp" in content
        assert "Hi Jane" in content
        assert "Hi other" not in content


def timezone_now():
    from django.utils import timezone
    return timezone.now()


@pytest.mark.django_db
class TestDiagnosticsView:
    """Verify the diagnostics page renders properly and does not expose secrets."""

    @pytest.fixture(autouse=True)
    def _setup(self, client, db):
        self.user = UserFactory(username="diaguser")
        client.force_login(self.user)
        self.client = client

    def test_page_loads(self):
        response = self.client.get(reverse("dashboard:diagnostics"))
        assert response.status_code == 200

    def test_shows_campaign_data(self):
        campaign = Campaign.objects.create(name="Diag Campaign", connection_note_enabled=True)
        lead = Lead.objects.create(
            public_identifier="diag-lead",
            linkedin_url="https://linkedin.com/in/diag-lead",
        )
        Deal.objects.create(lead=lead, campaign=campaign, state=DealState.QUALIFIED)

        response = self.client.get(reverse("dashboard:diagnostics"))
        content = response.content.decode("utf-8")
        assert "Diag Campaign" in content
        assert "Qualified" in content

    def test_shows_account_info(self):
        user = UserFactory(username="linkedinop")
        LinkedInProfile.objects.create(
            user=user,
            linkedin_username="linkedinop@mail.com",
            linkedin_password="topsecretpassword",
            active=True,
        )

        response = self.client.get(reverse("dashboard:diagnostics"))
        content = response.content.decode("utf-8")
        assert "linkedinop@mail.com" in content
        assert "topsecretpassword" not in content

    def test_no_secrets_in_page(self):
        user = UserFactory(username="secop")
        LinkedInProfile.objects.create(
            user=user,
            linkedin_username="secop@example.com",
            linkedin_password="linkedin_pass_secret",
            active=True,
        )
        cfg = SiteConfig.load()
        cfg.llm_api_key = "sk-llm-secret-key-999"
        cfg.bettercontact_api_key = "bc-secret-999"
        cfg.save()

        mb = Mailbox.objects.create(
            host="smtp.test.com", port=587, username="mail@test.com",
            password="mailbox_secret_pw", from_address="mail@test.com",
        )

        response = self.client.get(reverse("dashboard:diagnostics"))
        content = response.content.decode("utf-8")

        assert "linkedin_pass_secret" not in content
        assert "sk-llm-secret-key-999" not in content
        assert "bc-secret-999" not in content
        assert "mailbox_secret_pw" not in content

    def test_blocking_reasons_displayed(self):
        campaign = Campaign.objects.create(name="Blocked Diag")
        # No users assigned

        response = self.client.get(reverse("dashboard:diagnostics"))
        content = response.content.decode("utf-8")
        assert "No assigned users" in content

    def test_note_preview_does_not_send(self):
        """Note preview must render via template only, never calling the sender."""
        from unittest.mock import patch

        user = UserFactory(username="previewop")
        campaign = Campaign.objects.create(
            name="Preview Campaign",
            connection_note_enabled=True,
            connection_note_template="Hi {first_name}, let us connect.",
        )
        campaign.users.add(user)

        with patch(
            "openoutreach.linkedin.browser.connect_with_note.send_connection_request_with_note"
        ) as mock_send:
            response = self.client.get(reverse("dashboard:diagnostics"))
            mock_send.assert_not_called()

        content = response.content.decode("utf-8")
        assert response.status_code == 200
        # Should show note preview text
        assert "let us connect" in content.lower() or "Sample Note Preview" in content

    def test_shows_deal_funnel_counts(self):
        campaign = Campaign.objects.create(name="Funnel Campaign")
        lead1 = Lead.objects.create(public_identifier="f1", linkedin_url="https://linkedin.com/in/f1")
        lead2 = Lead.objects.create(public_identifier="f2", linkedin_url="https://linkedin.com/in/f2")
        Deal.objects.create(lead=lead1, campaign=campaign, state=DealState.CONNECTED)
        Deal.objects.create(lead=lead2, campaign=campaign, state=DealState.FAILED)

        response = self.client.get(reverse("dashboard:diagnostics"))
        content = response.content.decode("utf-8")
        assert "Funnel Campaign" in content
        assert "Connected" in content
        assert "Failed" in content

    def test_read_only_banner(self):
        response = self.client.get(reverse("dashboard:diagnostics"))
        content = response.content.decode("utf-8")
        assert "read-only" in content.lower()
