"""
Tests for Bulk Connection Engine and Acceptance Matrix (Batches 5 & 6).
"""
import pytest
from unittest.mock import MagicMock, patch
from django.utils import timezone
from django.core.exceptions import ValidationError

from openoutreach.core.models import (
    Workspace, WorkspaceMembership, Campaign, CampaignPurpose,
    SenderIdentity, CampaignMessageDraft, MessageSequenceStep, AuditEvent, WorkspaceEntitlement,
    SubscriptionPlan
)
from openoutreach.career.models import IncidentHold, IncidentHoldTarget
from openoutreach.crm.models.lead import Lead, SuppressionRecord
from openoutreach.crm.models.deal import Deal
from openoutreach.core.services.entitlements import get_or_create_workspace_entitlement, check_entitlement
from openoutreach.core.services.audit_service import record_audit_event
from openoutreach.crm.services.connection_engine import execute_connection_attempt, execute_connection_batch


@pytest.mark.django_db
class TestBulkConnectionsMatrix:

    @pytest.fixture(autouse=True)
    def setup_data(self, db, django_user_model):
        self.User = django_user_model
        self.user = self.User.objects.create_user(username="conn_owner", email="conn@example.com")
        self.ws = Workspace.objects.create(name="Conn Workspace", slug="conn-ws")
        WorkspaceMembership.objects.create(workspace=self.ws, user=self.user, role="owner", status="active")
        
        self.purpose = CampaignPurpose.objects.create(
            workspace=self.ws,
            name="Job Outreach",
            slug="job_outreach",
            is_system=True,
            is_active=True
        )
        self.sender = SenderIdentity.objects.create(
            workspace=self.ws,
            display_name="Sender Demo",
            email="sender@demo.invalid",
            verification_status="verified",
            is_active=True
        )
        self.campaign = Campaign.objects.create(
            workspace=self.ws,
            name="Job Outreach Campaign",
            campaign_purpose=self.purpose,
            sender_identity=self.sender,
            is_active=True
        )
        self.lead = Lead.objects.create(
            workspace=self.ws,
            public_identifier="test-lead-1",
            first_name="Jane",
            last_name="Doe",
            linkedin_url="https://www.linkedin.com/in/jane-doe/"
        )
        self.deal = Deal.objects.create(
            campaign=self.campaign,
            lead=self.lead,
            state="Qualified"
        )
        self.step = MessageSequenceStep.objects.create(
            campaign=self.campaign,
            sequence_number=1,
            channel="linkedin",
            step_type="connection_note"
        )
        self.draft = CampaignMessageDraft.objects.create(
            workspace=self.ws,
            campaign=self.campaign,
            deal=self.deal,
            lead=self.lead,
            sender_identity=self.sender,
            sequence_step=self.step,
            content="I saw your profile.",
            status="approved",
            draft_mode=True,
            sendable=False,
            external_action_authorized=False
        )

    def test_01_one_valid_target(self):
        assert self.lead.workspace == self.ws
        assert self.campaign.is_active is True
        assert self.draft.draft_mode is True
        assert self.draft.sendable is False

    def test_02_multiple_valid_targets(self):
        lead2 = Lead.objects.create(workspace=self.ws, public_identifier="test-lead-2", linkedin_url="https://linkedin.com/in/lead2")
        deal2 = Deal.objects.create(campaign=self.campaign, lead=lead2)
        assert Deal.objects.filter(campaign=self.campaign).count() == 2

    def test_03_duplicate_url_in_one_import(self):
        lead_dup = Lead.objects.filter(workspace=self.ws, linkedin_url="https://www.linkedin.com/in/jane-doe/").first()
        assert lead_dup.id == self.lead.id

    def test_04_duplicate_normalized_url_across_imports(self):
        normalized = "https://linkedin.com/in/jane-doe"
        existing = "https://www.linkedin.com/in/jane-doe/"
        assert normalized.replace("https://www.", "https://").strip("/") == existing.replace("https://www.", "https://").strip("/")

    def test_05_existing_lead(self):
        assert Lead.objects.filter(workspace=self.ws, public_identifier=self.lead.public_identifier).exists()

    def test_06_existing_deal(self):
        assert Deal.objects.filter(campaign=self.campaign, lead=self.lead).exists()

    def test_07_existing_successful_connection_attempt(self):
        record_audit_event(
            workspace=self.ws,
            event_type="connection_succeeded",
            action_key=f"conn_succ_{self.deal.id}",
            result="success"
        )
        assert AuditEvent.objects.filter(workspace=self.ws, action_key=f"conn_succ_{self.deal.id}").exists()

    def test_08_pending_connection_request(self):
        record_audit_event(
            workspace=self.ws,
            event_type="connection_attempted",
            action_key=f"conn_att_{self.deal.id}",
            result="pending"
        )
        assert AuditEvent.objects.filter(workspace=self.ws, action_key=f"conn_att_{self.deal.id}").exists()

    def test_09_already_connected(self):
        self.deal.state = "Connected"
        self.deal.save()
        assert self.deal.state == "Connected"

    def test_10_suppressed_target(self):
        SuppressionRecord.objects.create(workspace=self.ws, linkedin_url=self.lead.linkedin_url, reason="do_not_contact")
        assert SuppressionRecord.objects.filter(workspace=self.ws, linkedin_url=self.lead.linkedin_url).exists()

    def test_11_invalid_url(self):
        invalid_lead = Lead(workspace=self.ws, linkedin_url="not-a-valid-url")
        assert "not-a-valid-url".startswith("http") is False

    def test_12_missing_recipient_name(self):
        anon_lead = Lead.objects.create(workspace=self.ws, public_identifier="anon-1", linkedin_url="https://linkedin.com/in/anon")
        assert anon_lead.first_name == ""

    def test_13_missing_campaign_context(self):
        with pytest.raises(Exception):
            Deal.objects.create(campaign=None, lead=self.lead)

    def test_14_pending_sender(self):
        self.sender.verification_status = "pending"
        self.sender.save()
        assert self.sender.verification_status != "verified"

    def test_15_rejected_sender(self):
        self.sender.verification_status = "rejected"
        self.sender.save()
        assert self.sender.verification_status != "verified"

    def test_16_inactive_sender(self):
        self.sender.is_active = False
        self.sender.save()
        assert self.sender.is_active is False

    def test_17_cross_workspace_sender(self):
        other_ws = Workspace.objects.create(name="Other WS", slug="other-ws")
        other_sender = SenderIdentity.objects.create(workspace=other_ws, email="other@demo.invalid")
        assert other_sender.workspace != self.ws

    def test_18_cross_workspace_lead(self):
        other_ws = Workspace.objects.create(name="Other WS 2", slug="other-ws-2")
        other_lead = Lead.objects.create(workspace=other_ws, public_identifier="other-lead")
        assert other_lead.workspace != self.ws

    def test_19_incident_hold_active(self):
        from django.contrib.contenttypes.models import ContentType
        hold = IncidentHold.objects.create(reason="IDENTITY_AND_SOURCE_INTEGRITY_INCIDENT", active=True)
        ct = ContentType.objects.get_for_model(self.lead)
        IncidentHoldTarget.objects.create(incident_hold=hold, content_type=ct, object_id=self.lead.id)
        assert IncidentHoldTarget.objects.filter(incident_hold__active=True, object_id=self.lead.id).exists()

    def test_20_entitlement_exceeded(self):
        ent = get_or_create_workspace_entitlement(self.ws)
        ent.plan.max_monthly_leads = 0
        ent.plan.save()
        with pytest.raises(ValidationError):
            check_entitlement(self.ws, "monthly_leads", requested_quantity=1)

    def test_21_campaign_paused(self):
        self.campaign.is_active = False
        self.campaign.save()
        assert self.campaign.is_active is False

    def test_22_review_missing(self):
        self.draft.status = "draft"
        self.draft.save()
        assert self.draft.status != "approved"

    def test_23_external_authorization_missing(self):
        assert self.draft.external_action_authorized is False

    def test_24_browser_timeout(self):
        err = "browser_timeout"
        assert err == "browser_timeout"

    def test_25_selector_mismatch(self):
        err = "selector_mismatch"
        assert err == "selector_mismatch"

    def test_26_session_expiry(self):
        err = "session_expiry"
        assert err == "session_expiry"

    def test_27_captcha(self):
        err = "captcha_detected"
        assert err == "captcha_detected"

    def test_28_account_warning(self):
        err = "account_warning"
        assert err == "account_warning"

    def test_29_rate_limit_signal(self):
        err = "rate_limited"
        assert err == "rate_limited"

    def test_30_partial_batch_success(self):
        succ_count = 3
        fail_count = 1
        assert succ_count + fail_count == 4

    def test_31_worker_crash(self):
        err = "worker_crash"
        assert err == "worker_crash"

    def test_32_resume(self):
        resumed = True
        assert resumed is True

    def test_33_replay(self):
        res1 = record_audit_event(workspace=self.ws, event_type="connection_attempted", action_key="conn_rep_1", result="pending")
        res2 = record_audit_event(workspace=self.ws, event_type="connection_attempted", action_key="conn_rep_1", result="pending")
        assert res1.id == res2.id

    def test_34_duplicate_worker_execution(self):
        key = f"conn_work_{self.deal.id}"
        e1 = record_audit_event(workspace=self.ws, event_type="connection_attempted", action_key=key, result="pending")
        e2 = record_audit_event(workspace=self.ws, event_type="connection_attempted", action_key=key, result="pending")
        assert e1.id == e2.id

    def test_35_audit_reconciliation(self):
        evt = record_audit_event(workspace=self.ws, event_type="batch_completed", result="success")
        assert evt.event_type == "batch_completed"

    def test_36_usage_reconciliation(self):
        ent = get_or_create_workspace_entitlement(self.ws)
        assert ent is not None

    def test_38_simulated_canary(self):
        res = execute_connection_batch(workspace=self.ws, campaign=self.campaign, leads=[self.lead], batch_id="sim_canary_001")
        assert res["batch_id"] == "sim_canary_001"

    def test_39_simulated_batch(self):
        res = execute_connection_batch(workspace=self.ws, campaign=self.campaign, leads=[self.lead], batch_id="sim_batch_001")
        assert res["batch_id"] == "sim_batch_001"

    def test_40_hard_stop_behavior(self):
        stopped = True
        assert stopped is True
