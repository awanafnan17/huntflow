import pytest
from unittest.mock import MagicMock, patch
from django.utils import timezone
from openoutreach.core.models import Workspace, WorkspaceAutomationSettings, Campaign
from openoutreach.crm.models import Deal, Lead, DealState
from openoutreach.career.models import CandidateProfile
from openoutreach.linkedin.models import (
    LinkedInProfile,
    ProviderReadLedger,
    ProviderOperationType,
)
from openoutreach.linkedin.session_manager import (
    run_session_health_check,
    validate_linkedin_session,
    CAPTCHA_SELECTORS,
    CHALLENGE_SELECTORS,
    LOGIN_SELECTORS,
    HEALTHY_SELECTORS,
    RESTRICTION_SELECTORS,
)
from openoutreach.linkedin.services.account_governor import (
    is_account_safe_for_provider_read,
    is_account_safe_for_provider_write,
)
from openoutreach.core.scheduler import (
    plan_prospect_discovery_window,
    plan_connect_window,
    plan_follow_up_window,
    plan_reply_poll_window,
    plan_job_window,
)
from tests.factories import UserFactory
from linkedin_cli.page_state import PageState


class MockDOMPage:
    """Mock page that accurately models visible vs hidden DOM elements and URL paths."""

    def __init__(
        self,
        url: str = "https://www.linkedin.com/feed/",
        html: str = "",
        visible_selectors: list[str] | None = None,
        present_selectors: list[str] | None = None,
        title: str = "LinkedIn Feed",
    ):
        self.url = url
        self._html = html
        self._title = title
        self._visible_selectors = set(visible_selectors or [])
        self._present_selectors = set(present_selectors or [])
        if self._visible_selectors:
            self._present_selectors.update(self._visible_selectors)

    def content(self):
        return self._html

    def title(self):
        return self._title

    def locator(self, selector: str):
        is_vis = selector in self._visible_selectors
        is_pres = selector in self._present_selectors or is_vis
        loc = MagicMock()
        loc.count.return_value = 1 if is_pres else 0
        loc.nth.return_value.is_visible.return_value = is_vis
        loc.is_visible.return_value = is_vis
        return loc

    def query_selector(self, selector: str):
        if selector in self._present_selectors or selector in self._visible_selectors:
            el = MagicMock()
            el.is_visible.return_value = selector in self._visible_selectors
            return el
        return None


@pytest.mark.django_db
class TestCaptchaClassifierOfflineAudit:
    """Section 13: Exhaustive offline unit tests for classifier accuracy and false-positive prevention."""

    @pytest.fixture(autouse=True)
    def _setup(self, db):
        self.workspace = Workspace.objects.create(name="Classifier WS", slug="classifier-ws")
        self.user = UserFactory(username="test_classifier_user")
        self.profile = LinkedInProfile.objects.create(
            user=self.user,
            workspace=self.workspace,
            linkedin_username="classifier_test@example.com",
            active=True,
            session_status="unknown",
            cookie_data={"cookies": [{"name": "li_at", "value": "dummy_cookie"}]},
        )

    def test_authenticated_feed_with_hidden_captcha_and_arkose_strings_classifies_healthy(self):
        """Rule: Literal words 'captcha' and 'arkose' inside script/telemetry tags MUST NOT classify as CAPTCHA."""
        html = """
        <!DOCTYPE html>
        <html>
        <head>
            <script>
                window.__ARKOS_CONFIG__ = { arkosePublicKey: "12345", captchaEndpoint: "/captcha/v2" };
                var telemetry = { reason: "arkose_client_init", captcha_version: "2.1" };
            </script>
        </head>
        <body>
            <header class="global-nav">
                <nav id="global-nav">
                    <button class="global-nav__primary-link-me-menu-trigger">Me</button>
                </nav>
            </header>
            <main class="scaffold-layout__main">
                <div class="feed-identity-module">Alex Morgan</div>
            </main>
        </body>
        </html>
        """
        page = MockDOMPage(
            url="https://www.linkedin.com/feed/",
            html=html,
            visible_selectors=["#global-nav", ".global-nav", ".global-nav__primary-link-me-menu-trigger", ".feed-identity-module"],
            present_selectors=["#global-nav", ".global-nav", ".global-nav__primary-link-me-menu-trigger", ".feed-identity-module"],
        )

        cls, err, meta = validate_linkedin_session(self.profile, page_override=page, return_evidence=True)
        assert cls == "HEALTHY"
        assert err is None
        assert meta["healthy_signal_present"] is True
        assert meta["captcha_signal_visible"] is False
        assert meta["classification_rule"] == "authenticated_feed_reached"

    def test_hidden_captcha_node_does_not_classify_as_captcha(self):
        """Rule: A hidden CAPTCHA div (e.g. style='display:none') MUST NOT classify as CAPTCHA."""
        html = """
        <html>
        <body>
            <div id="captcha" style="display:none"></div>
            <div id="captcha-internal" style="visibility:hidden"></div>
            <nav id="global-nav"></nav>
        </body>
        </html>
        """
        page = MockDOMPage(
            url="https://www.linkedin.com/feed/",
            html=html,
            visible_selectors=["#global-nav"],
            present_selectors=["#captcha", "#captcha-internal", "#global-nav"],
        )

        cls, err, meta = validate_linkedin_session(self.profile, page_override=page, return_evidence=True)
        assert cls == "HEALTHY"
        assert meta["captcha_signal_visible"] is False
        assert meta["captcha_signal_present"] is True
        assert meta["healthy_signal_present"] is True

    def test_visible_genuine_captcha_fixture_classifies_captcha(self):
        """Rule: A genuine visible CAPTCHA element/iframe MUST classify as CAPTCHA."""
        html = """
        <html>
        <body>
            <iframe src="https://arkoselabs.com/fc/api/" title="Arkose Challenge" style="display:block"></iframe>
        </body>
        </html>
        """
        page = MockDOMPage(
            url="https://www.linkedin.com/checkpoint/challenge/captcha",
            html=html,
            visible_selectors=['iframe[src*="arkoselabs"]'],
            present_selectors=['iframe[src*="arkoselabs"]'],
        )

        cls, err, meta = validate_linkedin_session(self.profile, page_override=page, return_evidence=True)
        assert cls == "CAPTCHA"
        assert "captcha" in err.lower()
        assert meta["captcha_signal_visible"] is True
        assert meta["classification_rule"] == "visible_captcha_element"

    def test_visible_genuine_challenge_fixture_classifies_challenge(self):
        """Rule: A genuine visible challenge form on checkpoint route MUST classify as CHALLENGE/CHECKPOINT."""
        html = """
        <html>
        <body>
            <form id="checkpoint-challenge-form" style="display:block">
                <input name="pin" id="input__email_verification_pin" />
            </form>
        </body>
        </html>
        """
        page = MockDOMPage(
            url="https://www.linkedin.com/checkpoint/challenge/verify",
            html=html,
            visible_selectors=['form#checkpoint-challenge-form', 'input[name="pin"]'],
            present_selectors=['form#checkpoint-challenge-form', 'input[name="pin"]'],
        )

        cls, err, meta = validate_linkedin_session(self.profile, page_override=page, return_evidence=True)
        assert cls in ["CHALLENGE", "CHECKPOINT"]
        assert "challenge" in err.lower() or "checkpoint" in err.lower()
        assert meta["challenge_signal_present"] is True
        assert meta["classification_rule"] == "security_checkpoint_route_or_form"

    def test_authwall_classifies_auth_required(self):
        """Rule: Redirect to /authwall or /login MUST classify as AUTH_REQUIRED."""
        page = MockDOMPage(
            url="https://www.linkedin.com/authwall?trk=rip",
            html="<html><body>Join LinkedIn</body></html>",
            visible_selectors=['.authwall-join-form'],
            present_selectors=['.authwall-join-form'],
        )

        cls, err, meta = validate_linkedin_session(self.profile, page_override=page, return_evidence=True)
        assert cls == "AUTH_REQUIRED"
        assert meta["auth_required_signal_present"] is True
        assert meta["classification_rule"] == "login_or_authwall_redirect"

    def test_normal_authenticated_feed_classifies_healthy(self):
        """Rule: Normal authenticated feed with visible global-nav MUST classify as HEALTHY."""
        page = MockDOMPage(
            url="https://www.linkedin.com/feed/",
            html="<html><body>Feed</body></html>",
            visible_selectors=["#global-nav", ".feed-identity-module"],
        )

        cls, err, meta = validate_linkedin_session(self.profile, page_override=page, return_evidence=True)
        assert cls == "HEALTHY"
        assert err is None
        assert meta["healthy_signal_present"] is True

    def test_restriction_page_classifies_restriction(self):
        """Rule: Restriction endpoint or visible restriction heading MUST classify as RESTRICTION."""
        page = MockDOMPage(
            url="https://www.linkedin.com/checkpoint/rp/restricted",
            html="<html><body><h1>Your account is restricted</h1></body></html>",
            visible_selectors=['.account-restricted'],
        )

        cls, err, meta = validate_linkedin_session(self.profile, page_override=page, return_evidence=True)
        assert cls == "RESTRICTION"
        assert "restricted" in err.lower()
        assert meta["restriction_signal_present"] is True

    def test_generic_timeout_classifies_unknown(self):
        """Rule: Generic navigation timeout MUST classify as UNKNOWN, never CAPTCHA."""
        with patch("openoutreach.linkedin.session_manager.launch_browser") as mock_launch:
            mock_page = MagicMock()
            mock_page.goto.side_effect = Exception("Page.goto: Timeout 15000ms exceeded at https://www.linkedin.com/feed/")
            mock_launch.return_value = (mock_page, MagicMock(), MagicMock(), MagicMock())

            cls, err, meta = validate_linkedin_session(self.profile, return_evidence=True)
            assert cls == "UNKNOWN"
            assert "timeout" in err.lower()
            assert meta["classification_rule"] == "timeout_exception"


@pytest.mark.django_db
class TestAutomationPreferencePreservation:
    """Section 25: Verify all 8 user automation preferences remain ON across safety stops and recovery."""

    @pytest.fixture(autouse=True)
    def _setup(self, db):
        self.workspace = Workspace.objects.create(name="Candidate Agency", slug="default")
        self.user = UserFactory(username="candidate")
        self.auto_st = WorkspaceAutomationSettings.get_for_workspace(self.workspace)
        
        # Arm all 8 user automation preferences ON
        self.auto_st.master_automation_enabled = True
        self.auto_st.auto_find_prospects = True
        self.auto_st.auto_send_connections = True
        self.auto_st.auto_followup = True
        self.auto_st.auto_find_jobs = True
        self.auto_st.auto_apply_jobs = True
        self.auto_st.auto_send_emails = True
        self.auto_st.auto_monitor_replies = True
        self.auto_st.soak_status = "ACTIVE"
        self.auto_st.provider_safety_state = "NORMAL"
        self.auto_st.save()

        self.profile = LinkedInProfile.objects.create(
            user=self.user,
            workspace=self.workspace,
            linkedin_username="candidate@huntflow.ai",
            active=True,
            session_status="valid",
            identity_validation_status="MATCH_CONFIRMED",
            cookie_data={"cookies": [{"name": "li_at", "value": "test_li_at"}]},
        )

        self.campaign = Campaign.objects.create(
            name="Candidate Outreach Campaign #107",
            workspace=self.workspace,
            linkedin_profile=self.profile,
            is_active=True,
            linkedin_outreach_enabled=True,
        )

        self.candidate = CandidateProfile.objects.create(
            user=self.user,
            full_name="Alex Morgan",
            email="candidate@huntflow.ai",
        )

    def test_session_health_captcha_preserves_all_8_switches_and_halts_execution(self):
        """When CAPTCHA is detected, all 8 switches MUST remain True, profile=checkpoint, provider execution blocked."""
        captcha_page = MockDOMPage(
            url="https://www.linkedin.com/checkpoint/challenge/captcha",
            html='<iframe src="https://arkoselabs.com/fc/api/"></iframe>',
            visible_selectors=['iframe[src*="arkoselabs"]'],
        )

        # Run health check encountering genuine visible CAPTCHA
        run_session_health_check(self.profile, page_override=captcha_page)

        # 1. Profile status becomes checkpoint
        self.profile.refresh_from_db()
        assert self.profile.session_status == "checkpoint"
        assert "captcha" in (self.profile.session_error_details or "").lower()

        # 2. Workspace safety state is engaged, but ALL 8 USER PREFERENCES REMAIN ON
        self.auto_st.refresh_from_db()
        assert self.auto_st.soak_status == "SAFETY_STOP"
        assert self.auto_st.provider_safety_state == "CHECKPOINT_REQUIRED"

        # Verify all 8 switches remain strictly TRUE
        assert self.auto_st.master_automation_enabled is True
        assert self.auto_st.auto_find_prospects is True
        assert self.auto_st.auto_send_connections is True
        assert self.auto_st.auto_followup is True
        assert self.auto_st.auto_find_jobs is True
        assert self.auto_st.auto_apply_jobs is True
        assert self.auto_st.auto_send_emails is True
        assert self.auto_st.auto_monitor_replies is True

        # 3. Provider read is blocked by governor
        can_read, read_reason = is_account_safe_for_provider_read(
            workspace=self.workspace,
            linkedin_profile=self.profile,
        )
        assert can_read is False
        assert "checkpoint" in read_reason.lower() or "safety_stop" in read_reason.lower()

        # 4. Provider write is blocked by governor
        can_write, write_reason = is_account_safe_for_provider_write(
            workspace=self.workspace,
            linkedin_profile=self.profile,
            action_type="connect",
        )
        assert can_write is False
        assert "checkpoint" in write_reason.lower() or "safety_stop" in write_reason.lower()

        # 5. Schedulers produce 0 provider work items
        assert plan_prospect_discovery_window(session=None, campaign=self.campaign) == 0
        assert plan_connect_window(session=MagicMock(linkedin_profile=self.profile), campaign=self.campaign) == 0
        assert plan_job_window(session=None, workspace=self.workspace, candidate=self.candidate) == 0

        # 6. Structured metadata was recorded in ProviderReadLedger
        ledger = ProviderReadLedger.objects.filter(
            linkedin_profile=self.profile,
            operation_type=ProviderOperationType.SESSION_HEALTH,
        ).order_by("-id").first()
        assert ledger is not None
        assert ledger.success is False
        assert ledger.metadata["classification"] == "CAPTCHA"
        assert ledger.metadata["status"] == "checkpoint"
        assert ledger.metadata["captcha_signal_visible"] is True
        assert ledger.metadata["classification_rule"] == "visible_captcha_element"

    def test_healthy_recovery_preserves_switches_and_restores_eligibility(self):
        """When healthy session is recovered, all 8 switches remain ON and provider execution becomes eligible."""
        # Start in safety-stopped checkpoint state
        self.profile.session_status = "checkpoint"
        self.profile.save()
        self.auto_st.trigger_safety_stop("provider_captcha")
        self.auto_st.save()

        healthy_page = MockDOMPage(
            url="https://www.linkedin.com/feed/",
            html='<header class="global-nav"><nav id="global-nav"></nav></header>',
            visible_selectors=["#global-nav", ".global-nav"],
        )

        run_session_health_check(self.profile, page_override=healthy_page)

        self.profile.refresh_from_db()
        assert self.profile.session_status == "valid"

        self.auto_st.refresh_from_db()
        assert self.auto_st.provider_safety_state == "NORMAL"
        assert self.auto_st.soak_status == "ACTIVE"

        # All 8 user switches remain firmly ON
        assert self.auto_st.master_automation_enabled is True
        assert self.auto_st.auto_find_prospects is True
        assert self.auto_st.auto_send_connections is True
        assert self.auto_st.auto_followup is True
        assert self.auto_st.auto_find_jobs is True
        assert self.auto_st.auto_apply_jobs is True
        assert self.auto_st.auto_send_emails is True
        assert self.auto_st.auto_monitor_replies is True

        # Governor verifies read and write eligibility
        can_read, _ = is_account_safe_for_provider_read(
            workspace=self.workspace,
            linkedin_profile=self.profile,
        )
        assert can_read is True

        can_write, _ = is_account_safe_for_provider_write(
            workspace=self.workspace,
            linkedin_profile=self.profile,
            action_type="connect",
        )
        assert can_write is True

    def test_consecutive_captcha_to_healthy_recovery_without_manual_db_mutation(self):
        """Rule 8: Exactly tests CAPTCHA -> HEALTHY sequence on the same account with ZERO intermediate manual DB updates."""
        # 1. Start with all 8 switches ON and healthy account
        assert self.auto_st.master_automation_enabled is True
        assert self.auto_st.soak_status == "ACTIVE"

        # 2. Encounter genuine visible CAPTCHA
        captcha_page = MockDOMPage(
            url="https://www.linkedin.com/checkpoint/challenge/captcha",
            html='<iframe src="https://arkoselabs.com/fc/api/"></iframe>',
            visible_selectors=['iframe[src*="arkoselabs"]'],
        )
        run_session_health_check(self.profile, page_override=captcha_page)

        # Verify fail-closed safety state after CAPTCHA
        self.profile.refresh_from_db()
        self.auto_st.refresh_from_db()
        assert self.profile.session_status == "checkpoint"
        assert self.auto_st.soak_status == "SAFETY_STOP"
        assert self.auto_st.soak_stop_reason == "provider_captcha"
        assert self.auto_st.provider_safety_state == "CHECKPOINT_REQUIRED"

        # All 8 switches still ON
        assert self.auto_st.master_automation_enabled is True
        assert self.auto_st.auto_send_connections is True
        assert self.auto_st.auto_apply_jobs is True

        # Governor blocks execution
        can_read, _ = is_account_safe_for_provider_read(self.workspace, self.profile)
        can_write, _ = is_account_safe_for_provider_write(self.workspace, self.profile, "connect")
        assert can_read is False
        assert can_write is False

        # 3. DIRECTLY invoke HEALTHY session check with ZERO intermediate database mutations
        healthy_page = MockDOMPage(
            url="https://www.linkedin.com/feed/",
            html='<header class="global-nav"><nav id="global-nav"></nav></header>',
            visible_selectors=["#global-nav", ".global-nav"],
        )
        run_session_health_check(self.profile, page_override=healthy_page)

        # 4. Verify canonical recovery: all 8 ON, valid, NORMAL, ACTIVE, empty reason, governor eligible
        self.profile.refresh_from_db()
        self.auto_st.refresh_from_db()
        assert self.profile.session_status == "valid"
        assert self.auto_st.provider_safety_state == "NORMAL"
        assert self.auto_st.soak_status == "ACTIVE"
        assert self.auto_st.soak_stop_reason == ""

        # All 8 switches remain ON
        assert self.auto_st.master_automation_enabled is True
        assert self.auto_st.auto_find_prospects is True
        assert self.auto_st.auto_send_connections is True
        assert self.auto_st.auto_followup is True
        assert self.auto_st.auto_find_jobs is True
        assert self.auto_st.auto_apply_jobs is True
        assert self.auto_st.auto_send_emails is True
        assert self.auto_st.auto_monitor_replies is True

        # Governor now allows execution
        can_read_after, _ = is_account_safe_for_provider_read(self.workspace, self.profile)
        can_write_after, _ = is_account_safe_for_provider_write(self.workspace, self.profile, "connect")
        assert can_read_after is True
        assert can_write_after is True

    def test_healthy_session_does_not_clear_account_restriction(self):
        """Rule 9: HEALTHY session authentication must NOT clear an ACCOUNT_RESTRICTED safety stop."""
        self.auto_st.trigger_safety_stop("provider_restriction")
        self.auto_st.refresh_from_db()
        assert self.auto_st.provider_safety_state == "ACCOUNT_RESTRICTED"
        assert self.auto_st.soak_status == "SAFETY_STOP"

        healthy_page = MockDOMPage(
            url="https://www.linkedin.com/feed/",
            html='<header class="global-nav"><nav id="global-nav"></nav></header>',
            visible_selectors=["#global-nav", ".global-nav"],
        )
        run_session_health_check(self.profile, page_override=healthy_page)

        self.auto_st.refresh_from_db()
        # MUST remain fail-closed
        assert self.auto_st.provider_safety_state == "ACCOUNT_RESTRICTED"
        assert self.auto_st.soak_status == "SAFETY_STOP"
        assert self.auto_st.soak_stop_reason == "provider_restriction"

        can_read, _ = is_account_safe_for_provider_read(self.workspace, self.profile)
        can_write, _ = is_account_safe_for_provider_write(self.workspace, self.profile, "connect")
        assert can_read is False
        assert can_write is False

    def test_healthy_session_does_not_clear_high_volume_warning(self):
        """Rule 9: HEALTHY session authentication must NOT clear HIGH_VOLUME_DATA_ACCESS_WARNING."""
        self.auto_st.trigger_safety_stop("high_volume_data_access_warning")
        self.auto_st.refresh_from_db()
        assert self.auto_st.provider_safety_state == "HIGH_VOLUME_DATA_ACCESS_WARNING"
        assert self.auto_st.soak_status == "SAFETY_STOP"

        healthy_page = MockDOMPage(
            url="https://www.linkedin.com/feed/",
            html='<header class="global-nav"><nav id="global-nav"></nav></header>',
            visible_selectors=["#global-nav", ".global-nav"],
        )
        run_session_health_check(self.profile, page_override=healthy_page)

        self.auto_st.refresh_from_db()
        # MUST remain fail-closed
        assert self.auto_st.provider_safety_state == "HIGH_VOLUME_DATA_ACCESS_WARNING"
        assert self.auto_st.soak_status == "SAFETY_STOP"
        assert self.auto_st.soak_stop_reason == "high_volume_data_access_warning"

        can_read, _ = is_account_safe_for_provider_read(self.workspace, self.profile)
        can_write, _ = is_account_safe_for_provider_write(self.workspace, self.profile, "connect")
        assert can_read is False
        assert can_write is False

    def test_healthy_session_does_not_clear_manual_operator_hold(self):
        """Rule 9: HEALTHY session authentication must NOT clear a manual operator incident hold."""
        self.auto_st.trigger_safety_stop("manual_operator_incident_hold")
        self.auto_st.refresh_from_db()
        assert self.auto_st.soak_status == "SAFETY_STOP"
        assert self.auto_st.soak_stop_reason == "manual_operator_incident_hold"

        healthy_page = MockDOMPage(
            url="https://www.linkedin.com/feed/",
            html='<header class="global-nav"><nav id="global-nav"></nav></header>',
            visible_selectors=["#global-nav", ".global-nav"],
        )
        run_session_health_check(self.profile, page_override=healthy_page)

        self.auto_st.refresh_from_db()
        # MUST remain fail-closed
        assert self.auto_st.soak_status == "SAFETY_STOP"
        assert self.auto_st.soak_stop_reason == "manual_operator_incident_hold"

        can_read, _ = is_account_safe_for_provider_read(self.workspace, self.profile)
        can_write, _ = is_account_safe_for_provider_write(self.workspace, self.profile, "connect")
        assert can_read is False
        assert can_write is False

    def test_reconcile_safety_invariants_fixes_inconsistent_state(self):
        """Rule 7: State invariant prevents provider_safety_state=NORMAL while soak_status=SAFETY_STOP with recoverable reason."""
        self.auto_st.provider_safety_state = "NORMAL"
        self.auto_st.soak_status = "SAFETY_STOP"
        self.auto_st.soak_stop_reason = "provider_captcha"
        self.auto_st.save()

        changed = self.auto_st.reconcile_safety_invariants()
        assert changed is True
        assert self.auto_st.soak_status == "ACTIVE"
        assert self.auto_st.soak_stop_reason == ""

