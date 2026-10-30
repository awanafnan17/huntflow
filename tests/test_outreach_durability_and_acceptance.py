"""
Tests for Outreach Provider-Write Durability, Idempotency, Personalization Hardening, and Acceptance Detection.
"""
from unittest.mock import MagicMock, patch
import pytest
from django.utils import timezone
from django.contrib.auth.models import User
from openoutreach.core.models import Workspace, Campaign, OperatorActionApproval
from openoutreach.crm.models import Lead, Deal, DealState
from openoutreach.chat.models import ChatMessage
from openoutreach.linkedin.models import LinkedInProfile, ActionLog, ConnectionAttempt
from openoutreach.core.content_style import validate_user_facing_content_style, validate_generated_claims
from openoutreach.core.agents.connection_note import generate_connection_note, _clean_and_cap
from openoutreach.linkedin.services.reconciliation import (
    finalize_external_connection_success,
    handle_external_connection_uncertainty,
    reconcile_connection_attempt,
)
from openoutreach.linkedin.tasks.connect import handle_connect
from openoutreach.linkedin.tasks.follow_up import handle_follow_up
from openoutreach.linkedin.tasks.check_pending import handle_check_pending
from linkedin_cli.enums import ProfileState


@pytest.mark.django_db
class TestOutreachDurabilityAndAcceptance:
    def setup_method(self):
        self.user = User.objects.create_user(username="test_dur_op", email="test_dur_op@example.com")
        self.ws = Workspace.objects.create(name="Durability Workspace", slug="dur-ws")
        self.campaign = Campaign.objects.create(
            name="Durability Outreach",
            workspace=self.ws,
            is_active=True,
            campaign_type="job_outreach",
            connection_note_enabled=True,
            connection_note_template="Hi {first_name}, noticed your work at {company_or_industry}. Thought it'd be good to connect."
        )
        self.profile = LinkedInProfile.objects.create(
            user=self.user,
            workspace=self.ws,
            linkedin_username="dur_user",
            active=True
        )
        self.lead = Lead.objects.create(
            linkedin_url="https://www.linkedin.com/in/test-prospect-durability/",
            public_identifier="test-prospect-durability",
            first_name="Jane",
            last_name="Doe",
            workspace=self.ws
        )
        self.deal = Deal.objects.create(
            lead=self.lead,
            campaign=self.campaign,
            state=DealState.READY_TO_CONNECT
        )
        self.session = MagicMock()
        self.session.campaign = self.campaign
        self.session.linkedin_profile = self.profile
        self.session.page = MagicMock()

    def test_provider_success_and_actionlog_failure_no_retry(self):
        """If provider succeeds but ActionLog fails, external confirmation holds and no retry occurs."""
        idempotency_key = f"connect_{self.profile.id}_{self.lead.public_identifier}"
        conn_attempt = ConnectionAttempt.objects.create(
            linkedin_profile=self.profile,
            recipient_public_id=self.lead.public_identifier,
            idempotency_key=idempotency_key,
            status="attempting",
            note_text="Hi Jane"
        )
        approval = OperatorActionApproval.objects.create(
            workspace=self.ws,
            operator=self.user,
            action_type="connect",
            status="claimed"
        )

        with patch("openoutreach.linkedin.models.ActionLog.objects.create", side_effect=Exception("Database lock error")):
            finalize_external_connection_success(
                connection_attempt=conn_attempt,
                campaign=self.campaign,
                new_state=DealState.PENDING,
                approval=approval
            )

        conn_attempt.refresh_from_db()
        approval.refresh_from_db()
        self.deal.refresh_from_db()

        assert conn_attempt.status == "externally_confirmed"
        assert approval.status == "consumed"
        assert self.deal.state == DealState.PENDING

        # Second worker attempt must be blocked by the existing lock
        with patch("openoutreach.linkedin.browser.connect_with_note.send_connection_request_with_note") as mock_send:
            task = MagicMock()
            task.payload = {"public_identifier": self.lead.public_identifier}
            handle_connect(task, self.session, {})
            mock_send.assert_not_called()

    def test_provider_uncertainty_blocks_retry(self):
        """Provider timeout marks attempt uncertain, preserves non-reusable approval and blocks retry."""
        idempotency_key = f"connect_{self.profile.id}_{self.lead.public_identifier}"
        conn_attempt = ConnectionAttempt.objects.create(
            linkedin_profile=self.profile,
            recipient_public_id=self.lead.public_identifier,
            idempotency_key=idempotency_key,
            status="attempting",
            note_text="Hi Jane"
        )
        approval = OperatorActionApproval.objects.create(
            workspace=self.ws,
            operator=self.user,
            action_type="connect",
            status="claimed"
        )

        handle_external_connection_uncertainty(
            connection_attempt=conn_attempt,
            error_message="Playwright TimeoutError: Navigation timed out after 30000ms",
            approval=approval
        )

        conn_attempt.refresh_from_db()
        approval.refresh_from_db()

        assert conn_attempt.status == "uncertain"
        assert approval.status == "claimed"  # Not reverted to 'approved'

        # Automated read-only reconciliation
        with patch("linkedin_cli.actions.status.get_connection_status", return_value=ProfileState.PENDING):
            res = reconcile_connection_attempt(conn_attempt, self.session)
            assert res["status"] == "reconciled_confirmed"
            conn_attempt.refresh_from_db()
            assert conn_attempt.status == "externally_confirmed"

    def test_unsupported_relational_claim_blocked(self):
        """Phrases claiming unverified familiarity or tracking are blocked."""
        bad_texts = [
            "Hi Jane, I've been following your work at Acme Corp. Let's connect!",
            "I loved your recent article on distributed systems.",
            "I saw your talk at PyCon and was inspired.",
            "We share a passion for AI automation.",
            "I have been following your posts regularly."
        ]
        for text in bad_texts:
            res = validate_user_facing_content_style(text, "connection_note")
            assert res["valid"] is False, f"Expected '{text}' to fail validation"
            assert any("Prohibited" in err for err in res["errors"])

    def test_malformed_fragment_generation_blocked(self):
        """Malformed phrases resulting from placeholder strip are blocked."""
        bad_texts = [
            "Hi Jane! I'm, a at. Thought it'd be great to connect.",
            "Hi Jane! I'm a at Acme. Let's chat.",
            "Hi Jane, noticed your work at [Company Name].",
        ]
        for text in bad_texts:
            res = validate_user_facing_content_style(text, "connection_note")
            assert res["valid"] is False, f"Expected '{text}' to fail validation"

    def test_grounded_profile_fact_generation_passes(self):
        """Naturally grounded profile facts pass validation."""
        good_text = "Hi Jane, noticed you're leading engineering at Acme Corp. Working on Python infrastructure and thought it'd be good to connect."
        res = validate_user_facing_content_style(good_text, "connection_note")
        assert res["valid"] is True, f"Errors: {res.get('errors')}"

    def test_validation_failure_prevents_note_send(self):
        """If LLM generation fails validation, generate_connection_note returns validated template or blocked."""
        profile = {
            "first_name": "Jane",
            "positions": [{"company_name": "Acme Corp"}]
        }
        with patch("openoutreach.core.agents.connection_note._generate_llm_note", return_value="I've been following your work at [Company]."):
            note, source = generate_connection_note(self.session, self.campaign, profile)
            # Must fall back to safe validated template note
            assert "following your work" not in note
            assert "[" not in note
            assert source == "template"

    def test_acceptance_detected_exactly_once(self):
        """Read-only check_pending detects acceptance and transitions Deal to CONNECTED."""
        self.deal.state = DealState.PENDING
        self.deal.next_check_pending_at = timezone.now() - timezone.timedelta(minutes=5)
        self.deal.save()

        with patch("linkedin_cli.actions.status.get_connection_status", return_value=ProfileState.CONNECTED):
            handle_check_pending(MagicMock(), self.session, {})

        self.deal.refresh_from_db()
        assert self.deal.state == DealState.CONNECTED

    def test_inbound_reply_suppresses_canned_follow_up(self):
        """If prospect sent an inbound message, canned follow-up is suppressed."""
        self.deal.state = DealState.CONNECTED
        self.deal.save()

        # Inbound reply exists
        ChatMessage.objects.create(
            deal=self.deal,
            content="Hi! Thanks for connecting. What are you working on?",
            linkedin_urn="urn:li:msg:123456",
            is_outgoing=False,
            creation_date=timezone.now()
        )

        with patch("linkedin_cli.actions.message.send_raw_message") as mock_send:
            handle_follow_up(MagicMock(), self.session, {})
            mock_send.assert_not_called()
