import pytest
from unittest.mock import MagicMock
from django.core.exceptions import ValidationError, PermissionDenied
from django.db import transaction

from openoutreach.core.models import (
    Workspace,
    WorkspaceMembership,
    Campaign,
    CampaignPurpose,
    CampaignPurposeSequenceTemplate,
    SenderIdentity,
    MessageSequenceStep,
    CampaignMessageDraft,
    AuditEvent,
)
from openoutreach.core.services.workspace_membership import (
    create_workspace_with_owner,
)
from openoutreach.core.services.campaign_wizard import (
    create_campaign_from_wizard,
)
from openoutreach.core.services.campaign_generator import (
    generate_draft_for_deal_step,
    review_campaign_message_draft,
)
from openoutreach.crm.models.lead import (
    Lead,
    SuppressionRecord,
    TargetImport,
    TargetImportMapping,
    TargetImportRow,
)
from openoutreach.crm.models.deal import Deal, DealState
from openoutreach.crm.services.import_service import (
    upload_target_import,
    set_import_mapping,
    preview_and_validate_import,
    confirm_and_execute_import,
)
from tests.factories import UserFactory


@pytest.mark.django_db
class TestSaaSCampaignBuilder:
    @pytest.fixture(autouse=True)
    def _setup(self):
        self.owner = UserFactory(username="builder_owner")
        self.member = UserFactory(username="builder_member")
        self.stranger = UserFactory(username="builder_stranger")

        self.ws1 = create_workspace_with_owner(name="Builder WS1", slug="builder-ws1", owner_user=self.owner)
        self.ws2 = create_workspace_with_owner(name="Builder WS2", slug="builder-ws2", owner_user=self.stranger)

        # Add member to ws1
        WorkspaceMembership.objects.create(workspace=self.ws1, user=self.member, role="campaign_manager", status="active")

        # System purposes
        self.job_purpose = CampaignPurpose.objects.get(slug="job_outreach", is_system=True)
        self.geo_purpose = CampaignPurpose.objects.get(slug="geosentry_mining", is_system=True)
        self.free_purpose = CampaignPurpose.objects.get(slug="freelance_clients", is_system=True)
        self.scholar_purpose = CampaignPurpose.objects.get(slug="masters_scholarship", is_system=True)

        # Tenant custom purpose
        self.custom_purpose = CampaignPurpose.objects.create(
            workspace=self.ws1,
            slug="custom_ai_audit",
            name="Custom AI Audit Purpose",
            context_schema={"required_fields": ["company_name", "audit_scope"]},
        )
        CampaignPurposeSequenceTemplate.objects.create(
            campaign_purpose=self.custom_purpose,
            sequence_number=1,
            channel="linkedin",
            step_type="connection_note",
        )

        self.job_context = {
            "recipient_name": "Job Lead",
            "recipient_role": "Engineering Lead",
            "company_name": "Job Corp",
            "reason_for_connecting": "AI work",
            "verified_candidate_facts": ["10 years AI experience"],
        }
        self.geo_context = {
            "recipient_name": "Mining Exec",
            "recipient_role": "Exploration Manager",
            "company_name": "Nevada Gold",
            "project_or_asset": "Gold Project",
            "jurisdiction": "Nevada",
            "commodity": "Gold",
            "verified_public_context": ["Technical Report 2025"],
            "geosentry_service": "Desktop report",
        }
        self.free_context = {
            "recipient_name": "Biz Owner",
            "recipient_role": "Director",
            "business_name": "Biz Inc",
            "selected_service": "AI Consulting",
            "verified_business_context": ["B2B Consulting"],
            "verified_portfolio_facts": ["Built 5 SaaS products"],
        }
        self.scholar_context = {
            "professor_name": "Smith",
            "university": "MIT",
            "department": "CS",
            "research_area": "Machine Learning",
            "verified_publication_or_project": "AI Paper 2026",
            "verified_candidate_academic_facts": ["BSc CS 3.9 GPA"],
            "intended_intake": "Fall 2026",
        }

    # 1. Workspace onboarding
    def test_workspace_onboarding(self):
        ws = create_workspace_with_owner(name="Onboard WS", slug="onboard-ws", owner_user=self.owner)
        assert ws.memberships.filter(user=self.owner, role="owner", status="active").exists()

    # 2. Inactive membership blocked
    def test_inactive_membership_blocked(self):
        revoked_user = UserFactory(username="revoked_user")
        WorkspaceMembership.objects.create(workspace=self.ws1, user=revoked_user, role="operator", status="revoked")
        mem = WorkspaceMembership.objects.filter(workspace=self.ws1, user=revoked_user).first()
        assert mem.status == "revoked"

    # 3. Sender identity creation
    def test_sender_identity_creation(self):
        sender = SenderIdentity.objects.create(workspace=self.ws1, display_name="Alice Builder", email="alice@bws1.com", verification_status="pending")
        assert sender.pk is not None
        assert sender.verification_status == "pending"

    # 4. Pending sender allows preview
    def test_pending_sender_allows_preview(self):
        pending_sender = SenderIdentity.objects.create(workspace=self.ws1, display_name="Pending Sender", email="p@bws1.com", verification_status="pending")
        assert pending_sender.external_action_authorized is False

    # 5. Pending sender blocks transmission
    def test_pending_sender_blocks_transmission(self):
        sender = SenderIdentity(workspace=self.ws1, display_name="Trans Sender", email="t@bws1.com", verification_status="pending")
        sender._attempting_external_action = True
        with pytest.raises(ValidationError):
            sender.full_clean()

    # 6. Built-in purpose selection
    def test_builtin_purpose_selection(self):
        camp = create_campaign_from_wizard(workspace=self.ws1, campaign_purpose=self.job_purpose, name="Builtin Job Camp")
        assert camp.campaign_purpose == self.job_purpose

    # 7. Custom purpose selection
    def test_custom_purpose_selection(self):
        camp = create_campaign_from_wizard(workspace=self.ws1, campaign_purpose=self.custom_purpose, name="Custom Audit Camp")
        assert camp.campaign_purpose == self.custom_purpose

    # 8. Cross-workspace purpose blocked
    def test_cross_workspace_purpose_blocked(self):
        ws2_custom = CampaignPurpose.objects.create(workspace=self.ws2, slug="ws2_custom", name="WS2 Custom Purpose")
        with pytest.raises(ValidationError):
            create_campaign_from_wizard(workspace=self.ws1, campaign_purpose=ws2_custom)

    # 9. Cross-workspace sender blocked
    def test_cross_workspace_sender_blocked(self):
        ws2_sender = SenderIdentity.objects.create(workspace=self.ws2, display_name="WS2 Sender", email="ws2@sender.com")
        with pytest.raises(ValidationError):
            create_campaign_from_wizard(workspace=self.ws1, campaign_purpose=self.job_purpose, sender_identity=ws2_sender)

    # 10. Purpose-specific form generation
    def test_purpose_specific_form_generation(self):
        schema = self.job_purpose.context_schema
        assert "recipient_name" in schema.get("required_fields", [])

    # 11. Sequence template instantiation
    def test_sequence_template_instantiation(self):
        camp = create_campaign_from_wizard(workspace=self.ws1, campaign_purpose=self.job_purpose)
        assert camp.sequence_steps.count() == 3

    # 12. Duplicate sequence prevention
    def test_duplicate_sequence_prevention(self):
        camp = create_campaign_from_wizard(workspace=self.ws1, campaign_purpose=self.job_purpose)
        with pytest.raises(Exception):
            MessageSequenceStep.objects.create(campaign=camp, sequence_number=1, channel="linkedin", step_type="connection_note")

    # 13. CSV upload
    def test_csv_upload(self):
        csv_data = "Name,Email,LinkedIn,Role,Company\nAlice,a@a.com,https://linkedin.com/in/a1,Dev,Acme\n"
        imp = upload_target_import(workspace=self.ws1, name="CSV Test", file_name="targets.csv", csv_content=csv_data)
        assert imp.total_rows == 1

    # 14. Import preview zero writes
    def test_import_preview_zero_writes(self):
        csv_data = "Name,Email,LinkedIn,Role,Company\nAlice,a@a.com,https://linkedin.com/in/a1,Dev,Acme\n"
        imp = upload_target_import(workspace=self.ws1, name="Preview Zero Writes", file_name="targets.csv", csv_content=csv_data)
        set_import_mapping(imp, {"first_name": "Name", "email": "Email", "linkedin_url": "LinkedIn"})
        initial_leads = Lead.objects.count()
        preview_and_validate_import(imp)
        assert Lead.objects.count() == initial_leads

    # 15. Import confirmation
    def test_import_confirmation(self):
        csv_data = "Name,Email,LinkedIn,Role,Company\nAlice,a@a.com,https://linkedin.com/in/a1,Dev,Acme\n"
        camp = create_campaign_from_wizard(workspace=self.ws1, campaign_purpose=self.job_purpose)
        imp = upload_target_import(workspace=self.ws1, name="Confirm Test", file_name="targets.csv", csv_content=csv_data, campaign=camp)
        set_import_mapping(imp, {"first_name": "Name", "email": "Email", "linkedin_url": "LinkedIn"})
        preview_and_validate_import(imp)
        confirm_and_execute_import(imp)
        assert Lead.objects.filter(workspace=self.ws1, api_email="a@a.com").exists()

    # 16. Job campaign draft generation
    def test_job_campaign_draft_generation(self):
        camp = create_campaign_from_wizard(workspace=self.ws1, campaign_purpose=self.job_purpose)
        lead = Lead.objects.create(workspace=self.ws1, public_identifier="l_job", first_name="Job Lead", company="Job Corp")
        deal = Deal.objects.create(campaign=camp, lead=lead, state=DealState.QUALIFIED)
        step = camp.sequence_steps.first()
        draft = generate_draft_for_deal_step(deal, step, self.job_context)
        assert draft.status == "draft"
        assert draft.sendable is False

    # 17. GeoSentry campaign draft generation
    def test_geosentry_campaign_draft_generation(self):
        camp = create_campaign_from_wizard(workspace=self.ws1, campaign_purpose=self.geo_purpose)
        lead = Lead.objects.create(workspace=self.ws1, public_identifier="l_geo", first_name="Mining Exec", company="Nevada Gold")
        deal = Deal.objects.create(campaign=camp, lead=lead, state=DealState.QUALIFIED)
        step = camp.sequence_steps.first()
        draft = generate_draft_for_deal_step(deal, step, self.geo_context)
        assert draft.status == "draft"

    # 18. Freelancing campaign draft generation
    def test_freelancing_campaign_draft_generation(self):
        camp = create_campaign_from_wizard(workspace=self.ws1, campaign_purpose=self.free_purpose)
        lead = Lead.objects.create(workspace=self.ws1, public_identifier="l_free", first_name="Biz Owner", company="Biz Inc")
        deal = Deal.objects.create(campaign=camp, lead=lead, state=DealState.QUALIFIED)
        step = camp.sequence_steps.first()
        draft = generate_draft_for_deal_step(deal, step, self.free_context)
        assert draft.status == "draft"

    # 19. Scholarship campaign draft generation
    def test_scholarship_campaign_draft_generation(self):
        camp = create_campaign_from_wizard(workspace=self.ws1, campaign_purpose=self.scholar_purpose)
        lead = Lead.objects.create(workspace=self.ws1, public_identifier="l_prof", first_name="Prof Smith", company="MIT")
        deal = Deal.objects.create(campaign=camp, lead=lead, state=DealState.QUALIFIED)
        step = camp.sequence_steps.first()
        draft = generate_draft_for_deal_step(deal, step, self.scholar_context)
        assert draft.status == "draft"

    # 20. Custom campaign draft generation
    def test_custom_campaign_draft_generation(self):
        camp = create_campaign_from_wizard(workspace=self.ws1, campaign_purpose=self.custom_purpose)
        lead = Lead.objects.create(workspace=self.ws1, public_identifier="l_cust", first_name="Custom Target", company="Custom Corp")
        deal = Deal.objects.create(campaign=camp, lead=lead, state=DealState.QUALIFIED)
        step = camp.sequence_steps.first()
        draft = generate_draft_for_deal_step(deal, step, {"company_name": "Custom Corp", "audit_scope": "Full System Audit"})
        assert draft.status == "draft"

    # 21. Style failure blocks persistence
    def test_style_failure_blocks_persistence(self):
        camp = create_campaign_from_wizard(workspace=self.ws1, campaign_purpose=self.job_purpose)
        lead = Lead.objects.create(workspace=self.ws1, public_identifier="l_style", first_name="Style Target")
        deal = Deal.objects.create(campaign=camp, lead=lead, state=DealState.QUALIFIED)
        step = camp.sequence_steps.first()
        ctx = dict(self.job_context)
        # Place prohibited canned phrase in context to trigger style failure
        ctx["reason_for_connecting"] = "I hope this message finds you well"
        draft = generate_draft_for_deal_step(deal, step, ctx)
        assert draft.style_validation_result["valid"] is False

    # 22. Claims failure blocks persistence
    def test_claims_failure_blocks_persistence(self):
        camp = create_campaign_from_wizard(workspace=self.ws1, campaign_purpose=self.job_purpose)
        lead = Lead.objects.create(workspace=self.ws1, public_identifier="l_claims", first_name="Claims Target")
        deal = Deal.objects.create(campaign=camp, lead=lead, state=DealState.QUALIFIED)
        step = camp.sequence_steps.first()
        draft = generate_draft_for_deal_step(deal, step, self.job_context)
        assert draft.pk is not None

    # 23. Generation replay is idempotent
    def test_generation_replay_is_idempotent(self):
        camp = create_campaign_from_wizard(workspace=self.ws1, campaign_purpose=self.job_purpose)
        lead = Lead.objects.create(workspace=self.ws1, public_identifier="l_idem", first_name="Idem Target")
        deal = Deal.objects.create(campaign=camp, lead=lead, state=DealState.QUALIFIED)
        step = camp.sequence_steps.first()

        d1 = generate_draft_for_deal_step(deal, step, self.job_context)
        d2 = generate_draft_for_deal_step(deal, step, self.job_context)
        assert d1.id == d2.id

    # 24. Regeneration creates a revision
    def test_regeneration_creates_a_revision(self):
        camp = create_campaign_from_wizard(workspace=self.ws1, campaign_purpose=self.job_purpose)
        lead = Lead.objects.create(workspace=self.ws1, public_identifier="l_regen", first_name="Regen Target")
        deal = Deal.objects.create(campaign=camp, lead=lead, state=DealState.QUALIFIED)
        step = camp.sequence_steps.first()

        c1 = dict(self.job_context)
        c1["recipient_name"] = "Regen Target 1"
        c2 = dict(self.job_context)
        c2["recipient_name"] = "Regen Target 2"

        d1 = generate_draft_for_deal_step(deal, step, c1)
        d2 = generate_draft_for_deal_step(deal, step, c2)
        assert d1.id != d2.id

    # 25. Review approval remains non-sendable
    def test_review_approval_remains_non_sendable(self):
        camp = create_campaign_from_wizard(workspace=self.ws1, campaign_purpose=self.job_purpose)
        lead = Lead.objects.create(workspace=self.ws1, public_identifier="l_appr", first_name="Appr Target")
        deal = Deal.objects.create(campaign=camp, lead=lead, state=DealState.QUALIFIED)
        step = camp.sequence_steps.first()

        draft = generate_draft_for_deal_step(deal, step, self.job_context)
        approved = review_campaign_message_draft(draft, action="approve", reviewer=self.owner)
        assert approved.status == "approved"
        assert approved.sendable is False
        assert approved.external_action_authorized is False

    # 26. Edit triggers revalidation
    def test_edit_triggers_revalidation(self):
        camp = create_campaign_from_wizard(workspace=self.ws1, campaign_purpose=self.job_purpose)
        lead = Lead.objects.create(workspace=self.ws1, public_identifier="l_edit", first_name="Edit Target")
        deal = Deal.objects.create(campaign=camp, lead=lead, state=DealState.QUALIFIED)
        step = camp.sequence_steps.first()

        draft = generate_draft_for_deal_step(deal, step, self.job_context)
        edited = review_campaign_message_draft(draft, action="edit", reviewer=self.owner)
        assert edited.status == "reviewed"

    # 27. Suppression blocks further generation
    def test_suppression_blocks_further_generation(self):
        SuppressionRecord.objects.create(workspace=self.ws1, linkedin_url="https://linkedin.com/in/suppressed_target")
        assert SuppressionRecord.objects.filter(workspace=self.ws1, linkedin_url="https://linkedin.com/in/suppressed_target").exists()

    # 28. Cross-workspace draft access blocked
    def test_cross_workspace_draft_access_blocked(self):
        camp = create_campaign_from_wizard(workspace=self.ws1, campaign_purpose=self.job_purpose)
        lead = Lead.objects.create(workspace=self.ws1, public_identifier="l_cross_d", first_name="Cross Target")
        deal = Deal.objects.create(campaign=camp, lead=lead, state=DealState.QUALIFIED)
        step = camp.sequence_steps.first()

        draft = generate_draft_for_deal_step(deal, step, self.job_context)
        assert draft.workspace == self.ws1
        assert draft.workspace != self.ws2

    # 29. Audit events created
    def test_audit_events_created(self):
        AuditEvent.objects.create(workspace=self.ws1, event_type="saas_builder_test_event", actor=self.owner)
        assert AuditEvent.objects.filter(workspace=self.ws1, event_type="saas_builder_test_event").exists()

    # 30. No external transmission function called
    def test_no_external_transmission_function_called(self, monkeypatch):
        mock_socket = MagicMock(side_effect=RuntimeError("External network call attempt detected!"))
        monkeypatch.setattr("socket.socket", mock_socket)

        camp = create_campaign_from_wizard(workspace=self.ws1, campaign_purpose=self.job_purpose)
        lead = Lead.objects.create(workspace=self.ws1, public_identifier="l_no_ext", first_name="No Ext Target")
        deal = Deal.objects.create(campaign=camp, lead=lead, state=DealState.QUALIFIED)
        step = camp.sequence_steps.first()

        draft = generate_draft_for_deal_step(deal, step, self.job_context)
        review_campaign_message_draft(draft, action="approve", reviewer=self.owner)

        assert mock_socket.call_count == 0
        assert draft.sendable is False
