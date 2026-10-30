# tests/test_audit_events.py
"""Test suite for centralized audit logging service, event reconciliation, and idempotency."""
import pytest
from django.test import TestCase
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError

from openoutreach.core.models import (
    Workspace,
    CampaignPurpose,
    AuditEvent,
)
from openoutreach.core.services.workspace_membership import create_workspace_with_owner
from openoutreach.core.services.campaign_wizard import create_campaign_from_wizard
from openoutreach.core.services.campaign_generator import (
    generate_draft_for_deal_step,
    review_campaign_message_draft,
)
from openoutreach.core.services.audit_service import record_audit_event, sanitize_metadata
from openoutreach.crm.models.lead import Lead, TargetImport
from openoutreach.crm.models.deal import Deal
from openoutreach.crm.services.import_service import (
    upload_target_import,
    set_import_mapping,
    preview_and_validate_import,
    confirm_and_execute_import,
)

User = get_user_model()


class AuditServiceTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="audit_tester", email="audit@synthetic-demo.invalid")
        self.workspace = create_workspace_with_owner(
            name="Audit Workspace",
            slug="audit-ws",
            owner_user=self.user,
        )
        self.purpose = CampaignPurpose.objects.get(slug="job_outreach", is_system=True)

    def test_record_audit_event_redacts_sensitive_metadata(self):
        event = record_audit_event(
            workspace=self.workspace,
            event_type="test_event",
            actor=self.user,
            metadata={
                "password": "secret_password_123",
                "api_key": "test-1-aaaaaaaa",
                "normal_field": "public_data",
            },
        )
        self.assertEqual(event.payload["password"], "[test-1-aaaaaaaa]")
        self.assertEqual(event.payload["api_key"], "[test-1-aaaaaaaa]")
        self.assertEqual(event.payload["normal_field"], "public_data")

    def test_idempotent_audit_recording(self):
        evt1 = record_audit_event(
            workspace=self.workspace,
            event_type="idempotent_event",
            action_key="action_123",
            metadata={"step": 1},
        )
        evt2 = record_audit_event(
            workspace=self.workspace,
            event_type="idempotent_event",
            action_key="action_123",
            metadata={"step": 2},
        )
        self.assertEqual(evt1.id, evt2.id)
        self.assertEqual(AuditEvent.objects.filter(workspace=self.workspace, action_key="action_123").count(), 1)

    def test_audit_event_created_on_campaign_wizard(self):
        camp = create_campaign_from_wizard(
            workspace=self.workspace,
            campaign_purpose=self.purpose,
            name="Audit Campaign Wizard",
            user=self.user,
        )
        self.assertTrue(AuditEvent.objects.filter(workspace=self.workspace, event_type="campaign_created").exists())
        self.assertTrue(AuditEvent.objects.filter(workspace=self.workspace, event_type="campaign_sequence_instantiated").exists())

    def test_audit_event_created_on_import_flow(self):
        csv_data = "linkedin_url,first_name,last_name,email\nhttps://linkedin.com/in/audit1,Audit,User,audit1@synthetic-demo.invalid\n"
        target_import = upload_target_import(
            workspace=self.workspace,
            name="Audit Import",
            file_name="audit.csv",
            csv_content=csv_data,
            user=self.user,
        )
        set_import_mapping(target_import, {
            "linkedin_url": "linkedin_url",
            "first_name": "first_name",
            "last_name": "last_name",
            "email": "email",
        })
        preview_and_validate_import(target_import)
        self.assertTrue(AuditEvent.objects.filter(workspace=self.workspace, event_type="target_import_previewed").exists())

        confirm_and_execute_import(target_import)
        self.assertTrue(AuditEvent.objects.filter(workspace=self.workspace, event_type="target_created").exists())
        self.assertTrue(AuditEvent.objects.filter(workspace=self.workspace, event_type="target_import_confirmed").exists())

    def test_audit_event_created_on_draft_generation_and_review(self):
        camp = create_campaign_from_wizard(
            workspace=self.workspace,
            campaign_purpose=self.purpose,
            name="Draft Audit Campaign",
            user=self.user,
        )
        lead = Lead.objects.create(
            workspace=self.workspace,
            public_identifier="audit_lead",
            linkedin_url="https://linkedin.com/in/audit_lead",
        )
        deal = Deal.objects.create(campaign=camp, lead=lead)
        step = camp.sequence_steps.first()

        draft = generate_draft_for_deal_step(
            deal=deal,
            sequence_step=step,
            context_data={
                "recipient_name": "Audit Lead",
                "recipient_role": "CEO",
                "company_name": "Audit Corp",
                "reason_for_connecting": "Impressed by engineering work",
                "verified_candidate_facts": ["10 years experience"],
            },
            user=self.user,
        )

        self.assertTrue(AuditEvent.objects.filter(workspace=self.workspace, event_type="draft_generation_started").exists())
        self.assertTrue(AuditEvent.objects.filter(workspace=self.workspace, event_type="draft_style_validated").exists())
        self.assertTrue(AuditEvent.objects.filter(workspace=self.workspace, event_type="draft_claims_validated").exists())
        self.assertTrue(AuditEvent.objects.filter(workspace=self.workspace, event_type="draft_generated").exists())

        review_campaign_message_draft(draft, action="approve", reviewer=self.user)
        self.assertTrue(AuditEvent.objects.filter(workspace=self.workspace, event_type="draft_review_approved").exists())
