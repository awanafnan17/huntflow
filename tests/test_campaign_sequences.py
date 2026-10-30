import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from openoutreach.core.models import (
    Workspace,
    Campaign,
    CampaignPurpose,
    CampaignPurposeSequenceTemplate,
    MessageSequenceStep,
)
from tests.factories import UserFactory


@pytest.mark.django_db
class TestCampaignSequences:
    @pytest.fixture(autouse=True)
    def _setup(self):
        self.user = UserFactory(username="seq_user")
        self.ws = Workspace.objects.create(name="Seq WS", slug="seq-ws")
        self.job_purpose = CampaignPurpose.objects.get(slug="job_outreach", is_system=True)

    def test_sequence_templates_copied_to_campaign(self):
        camp = Campaign.objects.create(
            workspace=self.ws,
            campaign_purpose=self.job_purpose,
            campaign_type="job_outreach",
            name="Job Sequence Campaign",
        )
        # Copy templates to campaign steps
        templates = CampaignPurposeSequenceTemplate.objects.filter(campaign_purpose=self.job_purpose)
        for tmpl in templates:
            MessageSequenceStep.objects.get_or_create(
                campaign=camp,
                sequence_number=tmpl.sequence_number,
                defaults={
                    "channel": tmpl.channel,
                    "step_type": tmpl.step_type,
                    "delay_hours": tmpl.delay_hours,
                    "length_limit": tmpl.length_limit,
                    "approval_requirement": True,
                    "sending_policy": "draft_only",
                }
            )

        steps = list(camp.sequence_steps.all())
        assert len(steps) == 3
        assert steps[0].step_type == "connection_note"
        assert steps[1].step_type == "first_message"
        assert steps[2].step_type == "follow_up"

    def test_campaign_specific_sequence_step_editing(self):
        camp = Campaign.objects.create(workspace=self.ws, campaign_purpose=self.job_purpose, name="Edit Seq")
        step1 = MessageSequenceStep.objects.create(
            campaign=camp,
            sequence_number=1,
            channel="linkedin",
            step_type="connection_note",
            delay_hours=0,
            length_limit=250,
        )
        step1.length_limit = 280
        step1.save()

        # System sequence template remains untouched
        tmpl1 = CampaignPurposeSequenceTemplate.objects.get(campaign_purpose=self.job_purpose, sequence_number=1)
        assert tmpl1.length_limit == 300
        assert step1.length_limit == 280

    def test_duplicate_sequence_number_rejected(self):
        camp = Campaign.objects.create(workspace=self.ws, name="Dup Seq")
        MessageSequenceStep.objects.create(campaign=camp, sequence_number=1, channel="linkedin", step_type="connection_note")

        with pytest.raises((IntegrityError, ValidationError)):
            with transaction.atomic():
                MessageSequenceStep.objects.create(campaign=camp, sequence_number=1, channel="email", step_type="email_outbound")
