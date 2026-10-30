# tests/test_session_health.py
import pytest
from unittest.mock import MagicMock, patch
from django.urls import reverse
from django.utils import timezone
from tests.factories import UserFactory
from openoutreach.core.models import Workspace
from openoutreach.linkedin.models import (
    LinkedInProfile,
    ProviderReadLedger,
    ProviderOperationType,
    ProviderSafetyEvent,
)
from openoutreach.linkedin.session_manager import run_session_health_check, validate_linkedin_session, manual_login_linkedin
from openoutreach.linkedin.services.efficiency_metrics import get_provider_efficiency_summary
from linkedin_cli.page_state import PageState

@pytest.mark.django_db
class TestSessionHealth:
    
    @pytest.fixture(autouse=True)
    def _setup(self, client, db):
        self.workspace = Workspace.objects.create(name="Health Test WS", slug="health-test-ws")
        self.user = UserFactory(username="sessionoperator")
        self.profile = LinkedInProfile.objects.create(
            user=self.user,
            workspace=self.workspace,
            linkedin_username="testuser@example.com",
            linkedin_password="testpassword",
            cookie_data={"cookies": [{"name": "li_at", "value": "dummy"}]}
        )
        client.force_login(self.user)
        self.client = client

    def test_profile_has_session_health_fields(self):
        profile = LinkedInProfile.objects.get(id=self.profile.id)
        assert profile.session_status == "unknown"
        assert profile.last_validated_at is None
        assert profile.last_authenticated_at is None
        assert profile.session_error_details is None

    @patch("openoutreach.linkedin.session_manager.launch_browser")
    @patch("openoutreach.linkedin.session_manager.classify_page")
    def test_validate_linkedin_session_valid(self, mock_classify, mock_launch_browser):
        mock_page = MagicMock()
        mock_page.url = "https://www.linkedin.com/feed/"
        mock_page.content.return_value = "<html><body>Welcome to your feed</body></html>"
        
        mock_context = MagicMock()
        mock_browser = MagicMock()
        mock_playwright = MagicMock()
        
        mock_launch_browser.return_value = (mock_page, mock_context, mock_browser, mock_playwright)
        mock_classify.return_value = PageState.FEED
        
        status, details = validate_linkedin_session(self.profile)
        assert status == "HEALTHY"
        assert details is None

    @patch("openoutreach.linkedin.session_manager.launch_browser")
    @patch("openoutreach.linkedin.session_manager.classify_page")
    def test_validate_linkedin_session_restricted(self, mock_classify, mock_launch_browser):
        mock_page = MagicMock()
        mock_page.url = "https://www.linkedin.com/checkpoint/lg/restricted-login"
        mock_page.content.return_value = "<html><body>Your account has been temporarily restricted.</body></html>"
        
        mock_context = MagicMock()
        mock_browser = MagicMock()
        mock_playwright = MagicMock()
        
        mock_launch_browser.return_value = (mock_page, mock_context, mock_browser, mock_playwright)
        mock_classify.return_value = PageState.UNKNOWN
        
        status, details = validate_linkedin_session(self.profile)
        assert status == "RESTRICTION"
        assert "Account restricted detected" in details

    @patch("openoutreach.linkedin.session_manager.launch_browser")
    @patch("openoutreach.linkedin.session_manager.classify_page")
    def test_validate_linkedin_session_checkpoint(self, mock_classify, mock_launch_browser):
        mock_page = MagicMock()
        mock_page.url = "https://www.linkedin.com/checkpoint/challenge/verify"
        mock_page.content.return_value = "<html><body>Please verify your identity</body></html>"
        
        mock_context = MagicMock()
        mock_browser = MagicMock()
        mock_playwright = MagicMock()
        
        mock_launch_browser.return_value = (mock_page, mock_context, mock_browser, mock_playwright)
        mock_classify.return_value = PageState.CHECKPOINT
        
        status, details = validate_linkedin_session(self.profile)
        assert status in ["CHALLENGE", "CHECKPOINT"]
        assert "checkpoint challenge" in details.lower()

    @patch("openoutreach.linkedin.session_manager.launch_browser")
    def test_validate_linkedin_session_redirect_loop_sets_needs_reauth(self, mock_launch_browser):
        mock_page = MagicMock()
        mock_page.goto.side_effect = Exception("Page.goto: net::ERR_TOO_MANY_REDIRECTS at https://www.linkedin.com/feed/")
        
        mock_context = MagicMock()
        mock_browser = MagicMock()
        mock_playwright = MagicMock()
        
        mock_launch_browser.return_value = (mock_page, mock_context, mock_browser, mock_playwright)
        
        status, details = validate_linkedin_session(self.profile)
        assert status == "REDIRECT_LOOP"
        assert "Redirect loop detected" in details
        assert "manual re-authentication required" in details

    @patch("openoutreach.linkedin.session_manager.validate_linkedin_session")
    def test_failed_health_check_never_leaves_session_valid(self, mock_validate):
        self.profile.session_status = "valid"
        self.profile.save()
        
        mock_validate.return_value = ("needs_reauth", "Redirect loop detected")
        
        run_session_health_check(self.profile)
        
        self.profile.refresh_from_db()
        assert self.profile.session_status == "needs_reauth"
        assert self.profile.session_error_details == "Redirect loop detected"
        assert self.profile.can_execute() is False

    @patch("openoutreach.linkedin.session_manager.validate_linkedin_session")
    def test_health_check_read_is_durably_counted_in_provider_read_ledger(self, mock_validate):
        mock_validate.return_value = ("needs_reauth", "Redirect loop detected")
        
        initial_count = ProviderReadLedger.objects.filter(workspace=self.workspace).count()
        
        run_session_health_check(self.profile)
        
        new_entries = ProviderReadLedger.objects.filter(workspace=self.workspace)
        assert new_entries.count() == initial_count + 1
        
        entry = new_entries.last()
        assert entry.operation_type == ProviderOperationType.SESSION_HEALTH
        assert entry.resource_identifier == "feed_session_check"
        assert entry.success is False
        assert entry.read_cost == 1
        assert entry.cache_hit is False
        assert entry.linkedin_profile == self.profile
        assert entry.workspace == self.workspace

    @patch("openoutreach.linkedin.session_manager.validate_linkedin_session")
    def test_health_check_telemetry_does_not_count_as_profile_or_job_read(self, mock_validate):
        mock_validate.return_value = ("needs_reauth", "Redirect loop detected")
        
        run_session_health_check(self.profile)
        
        eff = get_provider_efficiency_summary(workspace=self.workspace)
        assert eff["provider_reads_today"] == 1
        assert eff["session_health_reads"] == 1
        assert eff["job_search_reads"] == 0
        assert eff["job_detail_reads"] == 0
        assert eff["prospect_searches"] == 0
        assert eff["full_profile_views"] == 0
        assert eff["inbox_pending_reads"] == 0

    def test_retrospective_safety_event_does_not_count_as_live_read(self):
        # Create retrospective safety event
        ProviderSafetyEvent.objects.create(
            workspace=self.workspace,
            linkedin_profile=self.profile,
            event_type="session_redirect_loop",
            url_category="https://www.linkedin.com/feed/",
            action_taken="NEEDS_REAUTH",
            metadata={
                "source": "recovered_prior_health_check_log",
                "retrospective": True,
                "provider_read_ledger_entry": False,
            }
        )
        
        # Verify ProviderReadLedger is untouched
        assert ProviderReadLedger.objects.filter(workspace=self.workspace).count() == 0
        
        # Verify telemetry shows 0 live reads
        eff = get_provider_efficiency_summary(workspace=self.workspace)
        assert eff["provider_reads_today"] == 0
        assert eff["session_health_reads"] == 0

    @patch("openoutreach.linkedin.session_manager.validate_linkedin_session")
    def test_healthy_session_check_sets_valid_and_authenticated_at(self, mock_validate):
        mock_validate.return_value = ("valid", None)
        
        before = timezone.now()
        run_session_health_check(self.profile)
        
        self.profile.refresh_from_db()
        assert self.profile.session_status == "valid"
        assert self.profile.last_validated_at >= before
        assert self.profile.last_authenticated_at >= before
        assert self.profile.session_error_details is None

    def test_huntflow_browser_adapter_does_not_invoke_stealth(self):
        import inspect
        from openoutreach.linkedin.browser import adapter
        source = inspect.getsource(adapter)
        assert "apply_stealth_sync" not in source
        assert "Stealth(" not in source
        assert "playwright_stealth" not in source

    def test_all_huntflow_browser_entrypoints_avoid_third_party_stealth(self):
        import inspect
        from openoutreach.linkedin.browser import launch
        from openoutreach.linkedin import identity_service, session_manager
        from scripts import verify_real_external_account, diagnose_nav_triggers
        
        for mod in [launch, identity_service, session_manager, verify_real_external_account, diagnose_nav_triggers]:
            src = inspect.getsource(mod)
            assert "from linkedin_cli.browser.login import launch_browser" not in src
            assert "playwright_stealth" not in src
            assert "apply_stealth_sync" not in src

    def test_session_view_get(self):
        url = reverse("dashboard:linkedin_account_session", kwargs={"pk": self.profile.pk})
        resp = self.client.get(url)
        assert resp.status_code == 200
        content = resp.content.decode("utf-8")
        assert "LinkedIn Session Health" in content
        assert "testuser" in content

    @patch("openoutreach.linkedin.session_manager.run_session_health_check")
    def test_session_view_post_validate(self, mock_health_check):
        def side_effect(p):
            p.session_status = "valid"
            p.save()
            return p
        mock_health_check.side_effect = side_effect
        
        url = reverse("dashboard:linkedin_account_session", kwargs={"pk": self.profile.pk})
        resp = self.client.post(url, {"action": "validate"})
        assert resp.status_code == 302
        
        self.profile.refresh_from_db()
        assert self.profile.session_status == "valid"

    def test_session_view_post_clear(self):
        url = reverse("dashboard:linkedin_account_session", kwargs={"pk": self.profile.pk})
        resp = self.client.post(url, {"action": "clear"})
        assert resp.status_code == 302
        
        self.profile.refresh_from_db()
        assert self.profile.cookie_data is None
        assert self.profile.session_status == "expired"
