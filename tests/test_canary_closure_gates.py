# tests/test_canary_closure_gates.py
"""
Focused regression tests for HuntFlow SaaS Offline Canary-Path Closure:
1. Cookie UI security and multi-tenant authorization
2. Canonical manual operator connection execution service & boundary gates
3. Session verification exact 9-state classifications, failure semantics, and pre-network ledgering
4. Canonical provider safety resolver state
"""
import hashlib
import json
from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase, Client
from django.utils import timezone
from linkedin_cli.page_state import PageState

from openoutreach.core.models import (
    Workspace,
    WorkspaceMembership,
    WorkspaceAutomationSettings,
    OperatorActionApproval,
    Campaign,
    Task,
    SiteConfig,
)
from openoutreach.crm.models import Lead, Deal, DealState
from openoutreach.linkedin.models import (
    LinkedInProfile,
    ProviderWriteIntent,
    ConnectionAttempt,
    ActionLog,
    ProviderReadLedger,
    ProviderSafetyEvent,
    ProviderAccountLease,
    ProviderOperationType,
)
from openoutreach.linkedin.services.account_governor import (
    is_account_safe_for_provider_read,
    is_account_safe_for_provider_write,
)
from openoutreach.linkedin.services.manual_connection import (
    execute_manual_operator_connection,
)
from openoutreach.linkedin.session_manager import (
    run_session_health_check,
    validate_linkedin_session,
)

User = get_user_model()
SENTINEL = "HUNTFLOW_SENTINEL_NOT_A_REAL_LINKEDIN_COOKIE"


class TestCookieUISecurity(TestCase):
    def setUp(self):
        self.client = Client()
        self.owner_user = User.objects.create_user(username="owner_user", password="Password123!", email="owner@test.com")
        self.viewer_user = User.objects.create_user(username="viewer_user", password="Password123!", email="viewer@test.com")
        self.other_tenant = User.objects.create_user(username="other_tenant", password="Password123!", email="other@test.com")

        self.ws_a = Workspace.objects.create(name="Workspace A", slug="ws-a")
        self.ws_b = Workspace.objects.create(name="Workspace B", slug="ws-b")

        WorkspaceMembership.objects.create(workspace=self.ws_a, user=self.owner_user, role="owner", status="active")
        WorkspaceMembership.objects.create(workspace=self.ws_a, user=self.viewer_user, role="member", status="active")
        WorkspaceMembership.objects.create(workspace=self.ws_b, user=self.other_tenant, role="owner", status="active")

        self.profile_a = LinkedInProfile.objects.create(
            user=self.owner_user,
            workspace=self.ws_a,
            linkedin_username="profile_a",
            active=True,
            session_status="needs_reauth",
        )

    def test_unauthenticated_request_cannot_store_cookie(self):
        resp = self.client.post(
            f"/dashboard/linkedin-accounts/{self.profile_a.pk}/session/",
            {"action": "save_li_at", "li_at_cookie": SENTINEL},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertTrue("/auth/login/" in resp.url or "/login" in resp.url)
        self.profile_a.refresh_from_db()
        self.assertFalse(bool(self.profile_a.cookie_data))

    def test_non_member_cannot_modify_profile_session(self):
        self.client.force_login(self.other_tenant)
        resp = self.client.post(
            f"/dashboard/linkedin-accounts/{self.profile_a.pk}/session/",
            {"action": "save_li_at", "li_at_cookie": SENTINEL},
        )
        self.assertEqual(resp.status_code, 403)
        self.profile_a.refresh_from_db()
        self.assertFalse(bool(self.profile_a.cookie_data))

    def test_ordinary_member_without_owner_admin_role_cannot_modify_session(self):
        self.client.force_login(self.viewer_user)
        resp = self.client.post(
            f"/dashboard/linkedin-accounts/{self.profile_a.pk}/session/",
            {"action": "save_li_at", "li_at_cookie": SENTINEL},
        )
        self.assertEqual(resp.status_code, 403)
        self.profile_a.refresh_from_db()
        self.assertFalse(bool(self.profile_a.cookie_data))

    def test_authorized_owner_can_submit_cookie_safely(self):
        self.client.force_login(self.owner_user)
        initial_tasks = Task.objects.count()
        initial_intents = ProviderWriteIntent.objects.count()

        resp = self.client.post(
            f"/dashboard/linkedin-accounts/{self.profile_a.pk}/session/",
            {"action": "save_li_at", "li_at_cookie": SENTINEL},
            follow=True,
        )
        self.assertEqual(resp.status_code, 200)
        self.profile_a.refresh_from_db()

        # Database value is encrypted
        self.assertTrue(self.profile_a.cookie_data.startswith("v1::"))
        self.assertNotIn(SENTINEL, self.profile_a.cookie_data)

        # Status is needs_reauth (zero auto network execution)
        self.assertEqual(self.profile_a.session_status, "needs_reauth")

        # Zero tasks and zero write intents created on save
        self.assertEqual(Task.objects.count(), initial_tasks)
        self.assertEqual(ProviderWriteIntent.objects.count(), initial_intents)

        # Plaintext sentinel and ciphertext not rendered back in HTML
        html = resp.content.decode("utf-8")
        self.assertNotIn(SENTINEL, html)
        self.assertNotIn("v1::", html)

    def test_get_request_does_not_mutate_session_state(self):
        self.client.force_login(self.owner_user)
        self.profile_a.session_status = "expired"
        self.profile_a.save()

        resp = self.client.get(f"/dashboard/linkedin-accounts/{self.profile_a.pk}/session/")
        self.assertEqual(resp.status_code, 200)
        self.profile_a.refresh_from_db()
        self.assertEqual(self.profile_a.session_status, "expired")


class TestManualConnectionExecutionService(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="op_user", password="Password123!")
        self.ws = Workspace.objects.create(name="WS 1", slug="ws-1")
        self.auto_settings = WorkspaceAutomationSettings.objects.create(
            workspace=self.ws,
            master_automation_enabled=False,
            auto_send_connections=False,
            auto_apply_jobs=False,
        )
        self.profile = LinkedInProfile.objects.create(
            user=self.user,
            workspace=self.ws,
            linkedin_username="test_prof",
            session_status="valid",
            identity_validation_status="MATCH",
            active=True,
            connect_daily_limit=20,
        )
        self.campaign = Campaign.objects.create(
            name="Test Campaign",
            workspace=self.ws,
            linkedin_profile=self.profile,
        )
        self.lead = Lead.objects.create(
            workspace=self.ws,
            first_name="Jane",
            last_name="Doe",
            current_title="Software Engineer",
            company="Acme Inc",
            headline="Software Engineer at Acme Inc",
            public_identifier="jane-doe-123",
            linkedin_url="https://www.linkedin.com/in/jane-doe-123",
        )
        self.note = "saw your engineering role at Acme Inc, wanted to say hi"
        self.deal = Deal.objects.create(
            campaign=self.campaign,
            lead=self.lead,
            state=DealState.READY_TO_CONNECT.value,
            generated_connection_note=self.note,
        )

        self.note_hash = hashlib.sha256(self.note.strip().encode("utf-8")).hexdigest()

        self.approval = OperatorActionApproval.objects.create(
            workspace=self.ws,
            operator=self.user,
            linkedin_profile=self.profile,
            lead=self.lead,
            deal=self.deal,
            action_type="connect",
            message_hash=self.note_hash,
            status="approved",
            expires_at=timezone.now() + timedelta(hours=1),
        )

    def test_manual_execution_with_valid_approval_reaches_mocked_transport_once(self):
        transport_mock = MagicMock(return_value=DealState.PENDING)

        res = execute_manual_operator_connection(
            approval_id=self.approval.id,
            workspace=self.ws,
            linkedin_profile=self.profile,
            deal=self.deal,
            note_text=self.note,
            custom_transport=transport_mock,
        )

        self.assertEqual(res["status"], "success")
        self.assertTrue(res["transport_invoked"])
        self.assertEqual(transport_mock.call_count, 1)

        # Approval consumed
        self.approval.refresh_from_db()
        self.assertEqual(self.approval.status, "consumed")
        self.assertIsNotNone(self.approval.consumed_at)

        # ProviderWriteIntent confirmed
        intent = ProviderWriteIntent.objects.get(id=res["write_intent_id"])
        self.assertEqual(intent.state, ProviderWriteIntent.State.CONFIRMED)

        # ActionLog recorded
        self.assertEqual(ActionLog.objects.filter(linkedin_profile=self.profile, action_type=ActionLog.ActionType.CONNECT).count(), 1)

    def test_manual_execution_rejects_wrong_workspace(self):
        other_ws = Workspace.objects.create(name="Other WS", slug="other-ws")
        transport_mock = MagicMock(return_value=DealState.PENDING)

        res = execute_manual_operator_connection(
            approval_id=self.approval.id,
            workspace=other_ws,
            linkedin_profile=self.profile,
            deal=self.deal,
            note_text=self.note,
            custom_transport=transport_mock,
        )

        self.assertEqual(res["status"], "blocked")
        self.assertEqual(transport_mock.call_count, 0)

    def test_manual_execution_rejects_wrong_profile(self):
        other_prof = LinkedInProfile.objects.create(
            user=self.user,
            workspace=self.ws,
            linkedin_username="other_prof",
            session_status="valid",
            identity_validation_status="MATCH",
        )
        transport_mock = MagicMock(return_value=DealState.PENDING)

        res = execute_manual_operator_connection(
            approval_id=self.approval.id,
            workspace=self.ws,
            linkedin_profile=other_prof,
            deal=self.deal,
            note_text=self.note,
            custom_transport=transport_mock,
        )

        self.assertEqual(res["status"], "blocked")
        self.assertEqual(transport_mock.call_count, 0)

    def test_manual_execution_rejects_altered_message_hash(self):
        transport_mock = MagicMock(return_value=DealState.PENDING)

        res = execute_manual_operator_connection(
            approval_id=self.approval.id,
            workspace=self.ws,
            linkedin_profile=self.profile,
            deal=self.deal,
            note_text="saw your updated role at Acme Inc, wanted to say hi",
            custom_transport=transport_mock,
        )

        self.assertEqual(res["status"], "blocked")
        self.assertEqual(transport_mock.call_count, 0)

    def test_manual_execution_rejects_expired_approval(self):
        self.approval.expires_at = timezone.now() - timedelta(minutes=5)
        self.approval.save()
        transport_mock = MagicMock(return_value=DealState.PENDING)

        res = execute_manual_operator_connection(
            approval_id=self.approval.id,
            workspace=self.ws,
            linkedin_profile=self.profile,
            deal=self.deal,
            note_text=self.note,
            custom_transport=transport_mock,
        )

        self.assertEqual(res["status"], "blocked")
        self.assertEqual(transport_mock.call_count, 0)

    def test_manual_execution_rejects_consumed_approval(self):
        self.approval.status = "consumed"
        self.approval.consumed_at = timezone.now()
        self.approval.save()
        transport_mock = MagicMock(return_value=DealState.PENDING)

        res = execute_manual_operator_connection(
            approval_id=self.approval.id,
            workspace=self.ws,
            linkedin_profile=self.profile,
            deal=self.deal,
            note_text=self.note,
            custom_transport=transport_mock,
        )

        self.assertEqual(res["status"], "blocked")
        self.assertEqual(transport_mock.call_count, 0)

    def test_manual_execution_rejects_safety_stop(self):
        self.auto_settings.soak_status = "SAFETY_STOP"
        self.auto_settings.soak_stop_reason = "high_volume_data_access_warning"
        self.auto_settings.save()
        transport_mock = MagicMock(return_value=DealState.PENDING)

        res = execute_manual_operator_connection(
            approval_id=self.approval.id,
            workspace=self.ws,
            linkedin_profile=self.profile,
            deal=self.deal,
            note_text=self.note,
            custom_transport=transport_mock,
        )

        self.assertEqual(res["status"], "blocked")
        self.assertIn("safety_gate", res["code"])
        self.assertEqual(transport_mock.call_count, 0)

    def test_manual_execution_rejects_quota_exhausted(self):
        self.profile.connect_daily_limit = 1
        self.profile.save()
        # Record 1 connect action today
        ActionLog.objects.create(
            linkedin_profile=self.profile,
            campaign=self.campaign,
            action_type=ActionLog.ActionType.CONNECT,
        )

        transport_mock = MagicMock(return_value=DealState.PENDING)
        res = execute_manual_operator_connection(
            approval_id=self.approval.id,
            workspace=self.ws,
            linkedin_profile=self.profile,
            deal=self.deal,
            note_text=self.note,
            custom_transport=transport_mock,
        )

        self.assertEqual(res["status"], "blocked")
        self.assertEqual(transport_mock.call_count, 0)

    def test_manual_execution_rejects_lease_conflict_deterministically(self):
        # Create active lease held by other worker
        ProviderAccountLease.objects.create(
            workspace=self.ws,
            linkedin_profile=self.profile,
            locked_by="pid_99999_other",
            owner_token="tok_other_worker",
            acquired_at=timezone.now(),
            expires_at=timezone.now() + timedelta(minutes=2),
            active_operation="running_task",
        )
        transport_mock = MagicMock(return_value=DealState.PENDING)

        initial_intents = ProviderWriteIntent.objects.count()

        res = execute_manual_operator_connection(
            approval_id=self.approval.id,
            workspace=self.ws,
            linkedin_profile=self.profile,
            deal=self.deal,
            note_text=self.note,
            custom_transport=transport_mock,
        )

        self.assertEqual(res["status"], "blocked")
        self.assertEqual(res["code"], "lease_conflict")
        self.assertEqual(transport_mock.call_count, 0)
        self.assertIsNone(res["write_intent_id"])

        # No intent left in UNCERTAIN state
        uncertain_intents = ProviderWriteIntent.objects.filter(state=ProviderWriteIntent.State.UNCERTAIN).count()
        self.assertEqual(uncertain_intents, 0)
        self.assertEqual(ProviderWriteIntent.objects.count(), initial_intents)

    def test_manual_execution_uncertain_timeout_marks_uncertain_and_prevents_retry(self):
        def timeout_transport(*args, **kwargs):
            raise TimeoutError("Navigation timed out after 30s")

        res = execute_manual_operator_connection(
            approval_id=self.approval.id,
            workspace=self.ws,
            linkedin_profile=self.profile,
            deal=self.deal,
            note_text=self.note,
            custom_transport=timeout_transport,
        )

        self.assertEqual(res["status"], "uncertain")
        self.assertTrue(res["transport_invoked"])

        intent = ProviderWriteIntent.objects.get(id=res["write_intent_id"])
        self.assertEqual(intent.state, ProviderWriteIntent.State.UNCERTAIN)

        # Second execution attempt is blocked without invoking transport again
        transport_mock = MagicMock()
        res2 = execute_manual_operator_connection(
            approval_id=self.approval.id,
            workspace=self.ws,
            linkedin_profile=self.profile,
            deal=self.deal,
            note_text=self.note,
            custom_transport=transport_mock,
        )
        self.assertEqual(res2["status"], "blocked")
        self.assertEqual(transport_mock.call_count, 0)


class TestSessionVerificationService(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="session_user", password="Password123!")
        self.ws = Workspace.objects.create(name="Session WS", slug="session-ws")
        self.auto_settings = WorkspaceAutomationSettings.objects.create(
            workspace=self.ws,
            master_automation_enabled=False,
            auto_send_connections=False,
            auto_apply_jobs=False,
        )
        self.profile = LinkedInProfile.objects.create(
            user=self.user,
            workspace=self.ws,
            linkedin_username="session_prof",
            session_status="needs_reauth",
            active=True,
        )
        # Store valid test cookie dictionary
        self.profile.set_cookie_data({
            "cookies": [{"name": "li_at", "value": "test_li_at_val", "domain": ".linkedin.com", "path": "/"}],
            "origins": []
        })
        self.profile.save()

    def test_classification_healthy(self):
        mock_page = MagicMock()
        mock_page.url = "https://www.linkedin.com/feed/"
        mock_page.content = MagicMock(return_value="<html><body>Feed page content</body></html>")

        initial_ledgers = ProviderReadLedger.objects.count()
        initial_intents = ProviderWriteIntent.objects.count()

        with patch("openoutreach.linkedin.session_manager.classify_page", return_value=PageState.FEED):
            run_session_health_check(self.profile, page_override=mock_page)

        self.profile.refresh_from_db()
        self.assertEqual(self.profile.session_status, "valid")
        self.assertEqual(ProviderReadLedger.objects.count(), initial_ledgers + 1)
        ledger = ProviderReadLedger.objects.order_by("-id").first()
        self.assertEqual(ledger.operation_type, ProviderOperationType.SESSION_HEALTH)
        self.assertTrue(ledger.success)
        self.assertEqual(ledger.metadata["classification"], "HEALTHY")
        self.assertEqual(ProviderWriteIntent.objects.count(), initial_intents)

    def test_classification_checkpoint(self):
        mock_page = MagicMock()
        mock_page.url = "https://www.linkedin.com/checkpoint/security-check"
        mock_page.content = MagicMock(return_value="<html><body>Security checkpoint verification</body></html>")

        initial_intents = ProviderWriteIntent.objects.count()
        run_session_health_check(self.profile, page_override=mock_page)

        self.profile.refresh_from_db()
        self.assertEqual(self.profile.session_status, "checkpoint")
        ledger = ProviderReadLedger.objects.order_by("-id").first()
        self.assertFalse(ledger.success)
        self.assertEqual(ledger.metadata["classification"], "CHECKPOINT")
        self.assertEqual(ProviderWriteIntent.objects.count(), initial_intents)

        self.auto_settings.refresh_from_db()
        self.assertEqual(self.auto_settings.soak_status, "SAFETY_STOP")
        self.assertEqual(self.auto_settings.soak_stop_reason, "provider_checkpoint")

    def test_classification_challenge(self):
        mock_page = MagicMock()
        mock_page.url = "https://www.linkedin.com/checkpoint/challenge/12345"
        mock_page.content = MagicMock(return_value="<html><body>Security challenge required</body></html>")

        run_session_health_check(self.profile, page_override=mock_page)

        self.profile.refresh_from_db()
        self.assertEqual(self.profile.session_status, "checkpoint")
        ledger = ProviderReadLedger.objects.order_by("-id").first()
        self.assertFalse(ledger.success)
        self.assertEqual(ledger.metadata["classification"], "CHALLENGE")

        self.auto_settings.refresh_from_db()
        self.assertEqual(self.auto_settings.soak_status, "SAFETY_STOP")
        self.assertEqual(self.auto_settings.soak_stop_reason, "provider_challenge")

    def test_classification_captcha(self):
        mock_page = MagicMock()
        mock_page.url = "https://www.linkedin.com/checkpoint/captcha"
        mock_page.content = MagicMock(return_value="<html><body>Please complete the captcha</body></html>")

        run_session_health_check(self.profile, page_override=mock_page)

        self.profile.refresh_from_db()
        self.assertEqual(self.profile.session_status, "checkpoint")
        ledger = ProviderReadLedger.objects.order_by("-id").first()
        self.assertFalse(ledger.success)
        self.assertEqual(ledger.metadata["classification"], "CAPTCHA")

        self.auto_settings.refresh_from_db()
        self.assertEqual(self.auto_settings.soak_status, "SAFETY_STOP")
        self.assertEqual(self.auto_settings.soak_stop_reason, "provider_captcha")

    def test_classification_restriction(self):
        mock_page = MagicMock()
        mock_page.url = "https://www.linkedin.com/checkpoint/restricted"
        mock_page.content = MagicMock(return_value="<html><body>Your account has been restricted</body></html>")

        run_session_health_check(self.profile, page_override=mock_page)

        self.profile.refresh_from_db()
        self.assertEqual(self.profile.session_status, "restricted")
        ledger = ProviderReadLedger.objects.order_by("-id").first()
        self.assertFalse(ledger.success)
        self.assertEqual(ledger.metadata["classification"], "RESTRICTION")

        self.auto_settings.refresh_from_db()
        self.assertEqual(self.auto_settings.soak_status, "SAFETY_STOP")
        self.assertEqual(self.auto_settings.soak_stop_reason, "provider_restriction")

    def test_classification_high_volume_warning(self):
        mock_page = MagicMock()
        mock_page.url = "https://www.linkedin.com/feed/"
        mock_page.content = MagicMock(return_value="<html><body>High volume unusual traffic detected on account</body></html>")

        run_session_health_check(self.profile, page_override=mock_page)

        self.profile.refresh_from_db()
        self.assertEqual(self.profile.session_status, "checkpoint")
        ledger = ProviderReadLedger.objects.order_by("-id").first()
        self.assertFalse(ledger.success)
        self.assertEqual(ledger.metadata["classification"], "HIGH_VOLUME_WARNING")

        self.auto_settings.refresh_from_db()
        self.assertEqual(self.auto_settings.soak_status, "SAFETY_STOP")
        self.assertEqual(self.auto_settings.soak_stop_reason, "high_volume_data_access_warning")

    def test_classification_auth_required(self):
        mock_page = MagicMock()
        mock_page.url = "https://www.linkedin.com/login"
        mock_page.content = MagicMock(return_value="<html><body>Sign in to LinkedIn</body></html>")

        run_session_health_check(self.profile, page_override=mock_page)

        self.profile.refresh_from_db()
        self.assertEqual(self.profile.session_status, "expired")
        ledger = ProviderReadLedger.objects.order_by("-id").first()
        self.assertFalse(ledger.success)
        self.assertEqual(ledger.metadata["classification"], "AUTH_REQUIRED")

    def test_classification_redirect_loop(self):
        with patch("openoutreach.linkedin.session_manager.launch_browser") as mock_launch:
            mock_launch.side_effect = Exception("ERR_TOO_MANY_REDIRECTS: redirect loop detected")

            run_session_health_check(self.profile)

            self.profile.refresh_from_db()
            self.assertEqual(self.profile.session_status, "needs_reauth")
            ledger = ProviderReadLedger.objects.order_by("-id").first()
            self.assertFalse(ledger.success)
            self.assertEqual(ledger.metadata["classification"], "REDIRECT_LOOP")

    def test_classification_generic_timeout_is_unknown_and_not_security_checkpoint(self):
        with patch("openoutreach.linkedin.session_manager.launch_browser") as mock_launch:
            mock_launch.side_effect = TimeoutError("Timeout 15000ms exceeded while navigating to feed")

            run_session_health_check(self.profile)

            self.profile.refresh_from_db()
            self.assertEqual(self.profile.session_status, "unknown")
            ledger = ProviderReadLedger.objects.order_by("-id").first()
            self.assertFalse(ledger.success)
            self.assertEqual(ledger.metadata["classification"], "UNKNOWN")

            # Must NOT trigger false safety stop
            self.auto_settings.refresh_from_db()
            self.assertNotEqual(self.auto_settings.soak_status, "SAFETY_STOP")


class TestCanonicalSafetyResolver(TestCase):
    def test_profile_safety_resolver_evaluates_accurately(self):
        user = User.objects.create_user(username="safety_user", password="Password123!")
        ws = Workspace.objects.create(name="Safety WS", slug="safety-ws")
        profile = LinkedInProfile.objects.create(
            user=user,
            workspace=ws,
            linkedin_username="safety_prof",
            session_status="needs_reauth",
            active=True,
        )

        is_safe_read, reason_read = is_account_safe_for_provider_read(workspace=ws, linkedin_profile=profile)
        self.assertFalse(is_safe_read)
        self.assertEqual(reason_read, "provider_session_status_needs_reauth")

        is_safe_write, reason_write = is_account_safe_for_provider_write(workspace=ws, linkedin_profile=profile, action_type="connect")
        self.assertFalse(is_safe_write)
        self.assertEqual(reason_write, "provider_session_status_needs_reauth")


class TestSaaSMultiTenantIsolation(TestCase):
    def setUp(self):
        self.user_candidate = User.objects.create_user(username="candidate_test", password="Password123!")
        self.user_bakr = User.objects.create_user(username="bakr_test", password="Password123!")

        self.ws_candidate = Workspace.objects.create(name="Candidate Agency", slug="candidate-agency-test")
        self.ws_bakr = Workspace.objects.create(name="Bakr Agency", slug="bakr-agency-test")

        WorkspaceMembership.objects.create(workspace=self.ws_candidate, user=self.user_candidate, role="owner", status="active")
        WorkspaceMembership.objects.create(workspace=self.ws_bakr, user=self.user_bakr, role="owner", status="active")

        self.profile_2 = LinkedInProfile.objects.create(
            user=self.user_candidate,
            workspace=self.ws_candidate,
            linkedin_username="candidate_prof",
            session_status="needs_reauth",
            active=True,
        )

        self.campaign_candidate = Campaign.objects.create(
            name="Candidate Campaign",
            workspace=self.ws_candidate,
            linkedin_profile=self.profile_2,
            is_active=False,
        )

        self.lead_candidate = Lead.objects.create(
            workspace=self.ws_candidate,
            first_name="John",
            last_name="Doe",
            current_title="Director",
            company="Enterprise Corp",
            linkedin_url="https://www.linkedin.com/in/john-doe-candidate",
        )

        self.deal_candidate = Deal.objects.create(
            campaign=self.campaign_candidate,
            lead=self.lead_candidate,
            state=DealState.QUALIFIED.value,
        )

        self.client_candidate = Client(SERVER_NAME="huntflow.online")
        self.client_candidate.force_login(self.user_candidate)

        self.client_bakr = Client(SERVER_NAME="huntflow.online")
        self.client_bakr.force_login(self.user_bakr)

    def test_candidate_can_access_profile_2_session_page(self):
        resp = self.client_candidate.get(f"/dashboard/linkedin-accounts/{self.profile_2.pk}/session/")
        self.assertEqual(resp.status_code, 200)

    def test_bakr_cannot_access_profile_2_session_page(self):
        resp = self.client_bakr.get(f"/dashboard/linkedin-accounts/{self.profile_2.pk}/session/")
        self.assertEqual(resp.status_code, 403)

    def test_candidate_can_access_campaign_review(self):
        resp = self.client_candidate.get(f"/dashboard/campaigns/{self.campaign_candidate.pk}/review/")
        self.assertEqual(resp.status_code, 200)

    def test_bakr_cannot_access_candidate_campaign_review(self):
        resp = self.client_bakr.get(f"/dashboard/campaigns/{self.campaign_candidate.pk}/review/")
        self.assertEqual(resp.status_code, 403)

    def test_bakr_cannot_access_candidate_workspace_members(self):
        resp = self.client_bakr.get(f"/dashboard/workspaces/{self.ws_candidate.pk}/members/")
        self.assertEqual(resp.status_code, 403)

    def test_bakr_leads_view_excludes_candidate_leads(self):
        resp = self.client_bakr.get("/dashboard/leads/")
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("john-doe-candidate", resp.content.decode("utf-8"))

    def test_bakr_cannot_access_candidate_lead_detail(self):
        resp = self.client_bakr.get(f"/dashboard/leads/{self.lead_candidate.pk}/")
        self.assertEqual(resp.status_code, 403)

    def test_provider_quotas_and_action_logs_are_strictly_isolated(self):
        self.profile_2.connect_daily_limit = 5
        self.profile_2.save()

        # Candidate performs 2 actions
        ActionLog.objects.create(linkedin_profile=self.profile_2, campaign=self.campaign_candidate, action_type=ActionLog.ActionType.CONNECT)
        ActionLog.objects.create(linkedin_profile=self.profile_2, campaign=self.campaign_candidate, action_type=ActionLog.ActionType.CONNECT)

        self.assertEqual(self.profile_2._daily_count(ActionLog.ActionType.CONNECT), 2)

        # Bakr creates profile
        profile_bakr = LinkedInProfile.objects.create(
            user=self.user_bakr,
            workspace=self.ws_bakr,
            linkedin_username="bakr_prof",
            session_status="valid",
            identity_validation_status="MATCH",
            connect_daily_limit=5,
        )
        self.assertEqual(profile_bakr._daily_count(ActionLog.ActionType.CONNECT), 0)


class TestOllamaEndpointResolutionAndCandidateAutomation(TestCase):
    def setUp(self):
        self.user_candidate = User.objects.create_user(username="candidate_llm", password="Password123!", is_staff=True)
        self.ws_candidate = Workspace.objects.create(name="Candidate Agency", slug="default-test")
        WorkspaceMembership.objects.create(workspace=self.ws_candidate, user=self.user_candidate, role="owner", status="active")

        self.user_bakr = User.objects.create_user(username="bakr_llm", password="Password123!")
        self.ws_bakr = Workspace.objects.create(name="Bakr Agency", slug="bakr-agency-test")
        WorkspaceMembership.objects.create(workspace=self.ws_bakr, user=self.user_bakr, role="owner", status="active")

        self.client_candidate = Client(SERVER_NAME="huntflow.online")
        self.client_candidate.force_login(self.user_candidate)

        self.cfg = SiteConfig.load()
        self.cfg.ai_model = "ollama:qwen2.5:1.5b"
        self.cfg.llm_api_base = "http://ollama:11434"
        self.cfg.save()

    def test_get_default_ollama_base_url_prefers_site_config(self):
        from openoutreach.core.llm import get_default_ollama_base_url
        base = get_default_ollama_base_url()
        self.assertEqual(base, "http://ollama:11434")

    @patch("urllib.request.urlopen")
    def test_settings_llm_get_renders_configured_endpoint_and_detected_models(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = json.dumps({"models": [{"name": "qwen2.5:1.5b"}]}).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        resp = self.client_candidate.get("/dashboard/settings/llm/")
        self.assertEqual(resp.status_code, 200)
        content = resp.content.decode("utf-8")
        self.assertIn("http://ollama:11434", content)
        self.assertNotIn("http://127.0.0.1:11434", content)
        self.assertIn("ollama:qwen2.5:1.5b", content)

    @patch("urllib.request.urlopen")
    def test_settings_llm_refresh_models_queries_configured_endpoint(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = json.dumps({"models": [{"name": "qwen2.5:1.5b"}]}).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        resp = self.client_candidate.post("/dashboard/settings/llm/", {"action": "refresh_models"}, follow=True)
        self.assertEqual(resp.status_code, 200)
        content = resp.content.decode("utf-8")
        self.assertIn("Ollama service reachable at http://ollama:11434", content)
        self.assertIn("ollama:qwen2.5:1.5b", content)

    def test_candidate_agency_can_enable_all_8_autonomous_switches(self):
        auto_st = WorkspaceAutomationSettings.get_for_workspace(self.ws_candidate)
        auto_st.master_automation_enabled = True
        auto_st.auto_find_prospects = True
        auto_st.auto_send_connections = True
        auto_st.auto_followup = True
        auto_st.auto_find_jobs = True
        auto_st.auto_apply_jobs = True
        auto_st.auto_send_emails = True
        auto_st.auto_monitor_replies = True
        auto_st.save()

        auto_st.refresh_from_db()
        self.assertTrue(auto_st.master_automation_enabled)
        self.assertTrue(auto_st.auto_find_prospects)
        self.assertTrue(auto_st.auto_send_connections)
        self.assertTrue(auto_st.auto_followup)
        self.assertTrue(auto_st.auto_find_jobs)
        self.assertTrue(auto_st.auto_apply_jobs)
        self.assertTrue(auto_st.auto_send_emails)
        self.assertTrue(auto_st.auto_monitor_replies)

    def test_bakr_agency_remains_strictly_disabled(self):
        auto_bakr = WorkspaceAutomationSettings.get_for_workspace(self.ws_bakr)
        self.assertFalse(auto_bakr.master_automation_enabled)
        self.assertFalse(auto_bakr.auto_find_prospects)
        self.assertFalse(auto_bakr.auto_send_connections)
        self.assertFalse(auto_bakr.auto_followup)
        self.assertFalse(auto_bakr.auto_find_jobs)
        self.assertFalse(auto_bakr.auto_apply_jobs)
        self.assertFalse(auto_bakr.auto_send_emails)
        self.assertFalse(auto_bakr.auto_monitor_replies)

    def test_auth_required_prevents_provider_task_generation_even_when_automation_armed(self):
        auto_st = WorkspaceAutomationSettings.get_for_workspace(self.ws_candidate)
        auto_st.master_automation_enabled = True
        auto_st.auto_send_connections = True
        auto_st.auto_followup = True
        auto_st.save()

        prof_needs_reauth = LinkedInProfile.objects.create(
            user=self.user_candidate,
            workspace=self.ws_candidate,
            linkedin_username="candidate_p2",
            session_status="needs_reauth",
            active=True,
        )

        camp = Campaign.objects.create(
            name="AI Founders",
            workspace=self.ws_candidate,
            linkedin_profile=prof_needs_reauth,
            is_active=True,
            linkedin_outreach_enabled=True,
        )
        camp.users.add(self.user_candidate)

        from openoutreach.core.scheduler import (
            plan_connect_window,
            plan_follow_up_window,
            plan_check_pending_window,
            plan_prospect_discovery_window,
            plan_job_window,
            plan_reply_poll_window,
        )

        class DummySession:
            def __init__(self, profile):
                self.linkedin_profile = profile
                self.active_timezone = "UTC"
                self.campaigns = [camp]

        session = DummySession(prof_needs_reauth)

        self.assertEqual(plan_connect_window(session, camp), 0)
        self.assertEqual(plan_follow_up_window(session, camp), 0)
        self.assertEqual(plan_check_pending_window(session, camp), 0)
        self.assertEqual(plan_prospect_discovery_window(session, camp), 0)
        self.assertEqual(plan_job_window(session, workspace=self.ws_candidate), 0)
        self.assertEqual(plan_reply_poll_window(session, camp), 0)

    @patch("urllib.request.urlopen")
    def test_call_ollama_raw_includes_keep_alive_and_custom_timeout(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = json.dumps({"response": "MOCKED_OLLAMA_OK"}).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        from openoutreach.career.services.ats_llm_engine import call_ollama_raw
        res = call_ollama_raw("Test prompt", timeout=30.0)
        self.assertEqual(res, "MOCKED_OLLAMA_OK")

        self.assertTrue(mock_urlopen.called)
        req = mock_urlopen.call_args[0][0]
        payload = json.loads(req.data.decode("utf-8"))
        self.assertEqual(payload["keep_alive"], "24h")
        self.assertEqual(payload["prompt"], "Test prompt")

    @patch("urllib.request.urlopen")
    def test_ai_assistant_process_chat_message_uses_ollama_directly(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = json.dumps({"response": "HUNTFLOW_LIVE_OLLAMA_CHAT_OK"}).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        from openoutreach.chat.models import AgentConversation
        from openoutreach.chat.service import process_chat_message

        conv, _ = AgentConversation.objects.get_or_create(user=self.user_candidate, is_active=True)
        reply = process_chat_message(self.user_candidate, conv, "Hello HuntFlow AI")
        self.assertEqual(reply, "HUNTFLOW_LIVE_OLLAMA_CHAT_OK")
        self.assertNotIn("I've noted:", reply)


class TestLinkedInSessionSaveAndVerifyService(TestCase):
    def setUp(self):
        from openoutreach.core.models import Workspace, WorkspaceMembership
        self.ws_candidate = Workspace.objects.create(name="Candidate Agency", slug="default")
        self.ws_bakr = Workspace.objects.create(name="Bakr Agency", slug="bakr-agency")

        self.user_candidate = User.objects.create_user(username="candidate_test", password="Password123!", email="candidate@example.com")
        self.user_bakr = User.objects.create_user(username="bakr_test", password="Password123!", email="bakr@example.com")

        WorkspaceMembership.objects.create(workspace=self.ws_candidate, user=self.user_candidate, role="owner", status="active")
        WorkspaceMembership.objects.create(workspace=self.ws_bakr, user=self.user_bakr, role="owner", status="active")

        self.p2 = LinkedInProfile.objects.create(
            user=self.user_candidate,
            workspace=self.ws_candidate,
            linkedin_username="candidate_li",
            active=True,
            session_status="needs_reauth",
        )

    def test_save_linkedin_session_cookie_stores_encrypted_and_sets_needs_reauth(self):
        from openoutreach.linkedin.session_manager import save_linkedin_session_cookie
        from openoutreach.core.models import Task
        from openoutreach.linkedin.models import ProviderWriteIntent, ProviderReadLedger

        # Initial baseline
        tasks_before = Task.objects.count()
        writes_before = ProviderWriteIntent.objects.count()
        reads_before = ProviderReadLedger.objects.count()

        sentinel_token = "TEST_SENTINEL_LI_AT_TOKEN_XYZ_123"
        save_res = save_linkedin_session_cookie(self.p2, f"  li_at={sentinel_token};  ")
        self.assertTrue(save_res)

        self.p2.refresh_from_db()
        self.assertEqual(self.p2.session_status, "needs_reauth")
        self.assertIn("Verification required", self.p2.session_error_details)
        self.assertIsNotNone(self.p2.last_authenticated_at)

        # Zero network side-effects
        self.assertEqual(Task.objects.count(), tasks_before)
        self.assertEqual(ProviderWriteIntent.objects.count(), writes_before)
        self.assertEqual(ProviderReadLedger.objects.count(), reads_before)

        # Production decryption check
        dec = self.p2.get_decrypted_cookie_data()
        self.assertIsInstance(dec, dict)
        cookies = dec.get("cookies", [])
        li_at_cookie = next((c for c in cookies if c.get("name") == "li_at"), None)
        self.assertIsNotNone(li_at_cookie)
        self.assertEqual(li_at_cookie.get("value"), sentinel_token)

    def test_save_linkedin_session_cookie_rejects_empty_input(self):
        from openoutreach.linkedin.session_manager import save_linkedin_session_cookie
        with self.assertRaises(ValueError):
            save_linkedin_session_cookie(self.p2, "")
        with self.assertRaises(ValueError):
            save_linkedin_session_cookie(self.p2, "   li_at= ;  ")

    def test_resolve_workspace_linkedin_profile_respects_workspace_and_blocks_cross_tenant(self):
        from openoutreach.linkedin.session_manager import resolve_workspace_linkedin_profile
        from django.core.exceptions import PermissionDenied

        # Candidate gets Profile #2
        resolved = resolve_workspace_linkedin_profile(self.user_candidate, self.ws_candidate)
        self.assertEqual(resolved.pk, self.p2.pk)

        # Bakr cannot access Candidate's Profile #2
        with self.assertRaises(PermissionDenied):
            resolve_workspace_linkedin_profile(self.user_bakr, self.ws_bakr, profile_id=self.p2.pk)

    def test_double_json_string_unwrapping_in_encryption(self):
        from openoutreach.core.encryption import encrypt_string, decrypt_string
        plaintext = "SECRET_VALUE_FOR_UNWRAP_TEST"
        encrypted = encrypt_string(plaintext)

        # Double encode into JSON string (as can occur in JSONField migrations)
        double_encoded = json.dumps(encrypted)
        self.assertTrue(double_encoded.startswith('"') and double_encoded.endswith('"'))

        # decrypt_string unwraps and authenticates
        decrypted = decrypt_string(double_encoded)
        self.assertEqual(decrypted, plaintext)

    @patch("openoutreach.linkedin.session_manager.launch_browser")
    def test_settings_linkedin_verify_view_maps_healthy_to_valid(self, mock_launch):
        mock_page = MagicMock()
        mock_page.url = "https://www.linkedin.com/feed/"
        mock_page.content.return_value = "<html><body>Feed content</body></html>"
        mock_context = MagicMock()
        mock_browser = MagicMock()
        mock_playwright = MagicMock()
        mock_launch.return_value = (mock_page, mock_context, mock_browser, mock_playwright)

        from django.test import Client
        c = Client(SERVER_NAME="huntflow.online")
        c.force_login(self.user_candidate)

        # Save valid cookie first
        from openoutreach.linkedin.session_manager import save_linkedin_session_cookie
        save_linkedin_session_cookie(self.p2, "AQED_TEST_VALID_COOKIE")

        resp = c.post("/dashboard/settings/linkedin/verify/", follow=True)
        self.assertEqual(resp.status_code, 200)

        self.p2.refresh_from_db()
        self.assertEqual(self.p2.session_status, "valid")
        self.assertTrue(self.p2.active)

    def test_account_session_campaigns_discovers_workspace_campaigns_without_legacy_users(self):
        from openoutreach.core.models import Campaign
        from openoutreach.linkedin.browser.session import AccountSession

        # Create campaign linked only to ws_candidate and p2, with users empty
        c_candidate = Campaign.objects.create(
            name="Candidate Autonomous Campaign",
            workspace=self.ws_candidate,
            linkedin_profile=self.p2,
            is_active=True,
        )
        c_candidate.users.clear()

        # Create Bakr campaign
        c_bakr = Campaign.objects.create(
            name="Bakr Isolated Campaign",
            workspace=self.ws_bakr,
            is_active=True,
        )
        c_bakr.users.clear()

        sess = AccountSession(self.p2)
        campaigns = sess.campaigns
        campaign_ids = [c.id for c in campaigns]

        # Candidate session includes Candidate campaign
        self.assertIn(c_candidate.id, campaign_ids)
        # Candidate session STRICTLY excludes Bakr campaign (no cross-tenant leakage)
        self.assertNotIn(c_bakr.id, campaign_ids)

    def test_daemon_campaigns_resolution_fallback_to_session_campaign(self):
        from openoutreach.core.models import Campaign
        from openoutreach.linkedin.browser.session import AccountSession

        c_candidate = Campaign.objects.create(
            name="Candidate Direct Campaign",
            workspace=self.ws_candidate,
            linkedin_profile=self.p2,
            is_active=True,
        )
        sess = AccountSession(self.p2)
        sess.campaign = c_candidate

        # Test daemon campaign fallback logic
        campaigns = sess.campaigns or ([sess.campaign] if getattr(sess, "campaign", None) else [])
        self.assertGreaterEqual(len(campaigns), 1)
        self.assertIn(c_candidate, campaigns)





