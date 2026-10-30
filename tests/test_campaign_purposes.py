import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.http import Http404

from openoutreach.core.models import (
    Workspace,
    Campaign,
    CampaignPurpose,
    PromptPack,
    PromptPackVersion,
    CampaignPurposeVersion,
    MessageSequenceStep,
)
from openoutreach.core.permissions import get_workspace_object_or_404
from openoutreach.core.schema_validation import validate_campaign_context_schema
from tests.factories import UserFactory


@pytest.mark.django_db
class TestCampaignPurposesAndSequences:
    @pytest.fixture(autouse=True)
    def _setup(self):
        self.user = UserFactory(username="purpose_user")
        self.ws1 = Workspace.objects.create(name="Purpose Workspace 1", slug="purpose-ws1")
        self.ws2 = Workspace.objects.create(name="Purpose Workspace 2", slug="purpose-ws2")

    def test_seeded_system_purposes_exist(self):
        system_slugs = {"job_outreach", "geosentry_mining", "freelance_clients", "masters_scholarship"}
        found_slugs = set(CampaignPurpose.objects.filter(is_system=True).values_list("slug", flat=True))
        assert system_slugs.issubset(found_slugs)

    def test_system_purpose_deletion_blocked(self):
        job_p = CampaignPurpose.objects.get(slug="job_outreach", is_system=True)
        with pytest.raises(ValidationError, match="System campaign purposes cannot be deleted"):
            job_p.delete()

    def test_tenant_custom_purpose_creation_and_cross_workspace_isolation(self):
        p1 = CampaignPurpose.objects.create(
            workspace=self.ws1,
            slug="saas_sales",
            name="SaaS B2B Sales WS1",
            is_system=False,
        )
        assert p1.pk is not None
        assert p1.workspace == self.ws1

        # Same slug allowed in another workspace
        p2 = CampaignPurpose.objects.create(
            workspace=self.ws2,
            slug="saas_sales",
            name="SaaS B2B Sales WS2",
            is_system=False,
        )
        assert p2.pk is not None
        assert p2.workspace == self.ws2

        # Duplicate slug inside same workspace rejected
        with pytest.raises((IntegrityError, ValidationError)):
            with transaction.atomic():
                CampaignPurpose.objects.create(
                    workspace=self.ws1,
                    slug="saas_sales",
                    name="Duplicate SaaS Sales",
                    is_system=False,
                )

        # Cross-workspace access isolation
        with pytest.raises(Http404):
            get_workspace_object_or_404(CampaignPurpose, p1.pk, self.ws2)

    def test_campaign_workspace_and_purpose_matching(self):
        tenant_p1 = CampaignPurpose.objects.create(
            workspace=self.ws1,
            slug="custom_purpose",
            name="Custom Purpose WS1",
        )

        # Campaign in ws1 with ws1 tenant purpose succeeds
        c1 = Campaign(name="C1", workspace=self.ws1, campaign_purpose=tenant_p1, campaign_type="custom_purpose")
        c1.full_clean()
        c1.save()

        # Campaign in ws2 with ws1 tenant purpose fails validation
        c2 = Campaign(name="C2", workspace=self.ws2, campaign_purpose=tenant_p1, campaign_type="custom_purpose")
        with pytest.raises(ValidationError, match="workspace must match campaign workspace"):
            c2.full_clean()

        # System purpose can be used by any workspace
        job_p = CampaignPurpose.objects.get(slug="job_outreach", is_system=True)
        c3 = Campaign(name="C3", workspace=self.ws2, campaign_purpose=job_p, campaign_type="job_outreach")
        c3.full_clean()

    def test_inactive_purpose_cannot_be_assigned(self):
        inactive_p = CampaignPurpose.objects.create(
            workspace=self.ws1,
            slug="old_purpose",
            name="Old Inactive Purpose",
            is_active=False,
        )
        camp = Campaign(name="Inactive Purpose Campaign", workspace=self.ws1, campaign_purpose=inactive_p)
        with pytest.raises(ValidationError, match="Cannot assign an inactive campaign purpose"):
            camp.full_clean()

    def test_legacy_campaign_type_mismatch_fails_validation(self):
        job_p = CampaignPurpose.objects.get(slug="job_outreach", is_system=True)
        camp = Campaign(
            name="Mismatch Campaign",
            workspace=self.ws1,
            campaign_purpose=job_p,
            campaign_type="freelance_clients",  # mismatch with job_outreach
        )
        with pytest.raises(ValidationError, match="does not match campaign purpose slug"):
            camp.full_clean()

    def test_context_schema_validation_and_dangerous_constructs(self):
        schema = {
            "required_fields": ["hiring_manager_name", "role_title"],
            "field_types": {"hiring_manager_name": "str", "role_title": "str", "experience_years": "int"},
            "max_lengths": {"hiring_manager_name": 50},
        }

        # Valid
        ok, errors = validate_campaign_context_schema({"hiring_manager_name": "Alice", "role_title": "AI Dev", "experience_years": 3}, schema)
        assert ok is True

        # Malformed / missing required field
        ok, errors = validate_campaign_context_schema({"hiring_manager_name": "Alice"}, schema)
        assert ok is False
        assert any("Missing required field: role_title" in e for e in errors)

        # Dangerous construct injection rejected
        dangerous_data = {
            "hiring_manager_name": "Alice",
            "role_title": "__import__('os').system('rm -rf /')",
        }
        ok, errors = validate_campaign_context_schema(dangerous_data, schema)
        assert ok is False
        assert any("prohibited dangerous construct" in e for e in errors)

    def test_prompt_pack_versioning(self):
        job_p = CampaignPurpose.objects.get(slug="job_outreach", is_system=True)
        pack = PromptPack.objects.create(campaign_purpose=job_p, name="Job Pack", version=1)

        v1 = PromptPackVersion.objects.create(
            prompt_pack=pack,
            version_number=1,
            prompt_template="v1 template {first_name}",
            checksum="hash1",
        )
        assert v1.version_number == 1

        # Creating a new version instead of mutating v1
        v2 = PromptPackVersion.objects.create(
            prompt_pack=pack,
            version_number=2,
            prompt_template="v2 updated template {first_name}",
            checksum="hash2",
        )
        assert v2.version_number == 2
        assert v1.prompt_template == "v1 template {first_name}"  # v1 untouched

    def test_sequence_step_cross_workspace_prompt_pack_rejected(self):
        job_p = CampaignPurpose.objects.get(slug="job_outreach", is_system=True)
        p1 = CampaignPurpose.objects.create(workspace=self.ws1, slug="ws1_purpose", name="WS1 Purpose")
        pack1 = PromptPack.objects.create(campaign_purpose=p1, name="WS1 Pack", version=1)
        v1 = PromptPackVersion.objects.create(prompt_pack=pack1, version_number=1, prompt_template="T1")

        camp_ws2 = Campaign.objects.create(name="WS2 Campaign", workspace=self.ws2, campaign_purpose=job_p)

        step = MessageSequenceStep(
            campaign=camp_ws2,
            sequence_number=1,
            prompt_version=v1,  # Belongs to ws1!
        )
        with pytest.raises(ValidationError, match="Sequence step prompt pack must belong to the same workspace"):
            step.full_clean()

    def test_message_sequence_steps_ordering_and_uniqueness(self):
        camp = Campaign.objects.create(name="Sequence Campaign", workspace=self.ws1)

        step1 = MessageSequenceStep.objects.create(
            campaign=camp,
            sequence_number=1,
            channel="linkedin",
            step_type="connection_note",
            delay_hours=0,
            length_limit=300,
        )
        step2 = MessageSequenceStep.objects.create(
            campaign=camp,
            sequence_number=2,
            channel="linkedin",
            step_type="follow_up",
            delay_hours=48,
            length_limit=500,
            is_enabled=False,
        )

        steps = list(camp.sequence_steps.all())
        assert len(steps) == 2
        assert steps[0] == step1
        assert steps[1] == step2

        # Disabled step excluded from generation query
        enabled_steps = list(camp.sequence_steps.filter(is_enabled=True))
        assert len(enabled_steps) == 1
        assert enabled_steps[0] == step1

        # Duplicate sequence number in same campaign fails constraint
        with pytest.raises((IntegrityError, ValidationError)):
            with transaction.atomic():
                MessageSequenceStep.objects.create(
                    campaign=camp,
                    sequence_number=1,
                    channel="email",
                    step_type="email_outbound",
                )
