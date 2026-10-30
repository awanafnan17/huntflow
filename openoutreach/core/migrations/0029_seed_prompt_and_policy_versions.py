import hashlib
from django.db import migrations

def seed_prompt_policy_and_templates(apps, schema_editor):
    CampaignPurpose = apps.get_model("core", "CampaignPurpose")
    PromptPack = apps.get_model("core", "PromptPack")
    PromptPackVersion = apps.get_model("core", "PromptPackVersion")
    CampaignPurposeVersion = apps.get_model("core", "CampaignPurposeVersion")
    CampaignPurposeSequenceTemplate = apps.get_model("core", "CampaignPurposeSequenceTemplate")
    Campaign = apps.get_model("core", "Campaign")
    MessageSequenceStep = apps.get_model("core", "MessageSequenceStep")

    system_purposes_data = {
        "job_outreach": {
            "prompt_name": "Job Outreach Default Pack",
            "prompt_template": "Hi {recipient_name}, I noticed your leadership at {company_name} in {recipient_role}. {reason_for_connecting}. Thought it would be great to connect.",
            "writing_policy": "Professional, natural tone. No em-dashes or corporate filler phrases.",
            "claims_policy": "Strict factual claims only matching verified candidate facts.",
            "sequence_templates": [
                {"sequence_number": 1, "channel": "linkedin", "step_type": "connection_note", "delay_hours": 0, "length_limit": 300},
                {"sequence_number": 2, "channel": "linkedin", "step_type": "first_message", "delay_hours": 24, "length_limit": 500},
                {"sequence_number": 3, "channel": "linkedin", "step_type": "follow_up", "delay_hours": 48, "length_limit": 500},
            ]
        },
        "geosentry_mining": {
            "prompt_name": "GeoSentry Mining Default Pack",
            "prompt_template": "Hello {recipient_name}, regarding {project_or_asset} in {jurisdiction}: we prepare desktop geological screening reports for {commodity} assets. Would love to share details.",
            "writing_policy": "Technical, concise executive communication. Zero hype.",
            "claims_policy": "Geological data references grounded in verified public context.",
            "sequence_templates": [
                {"sequence_number": 1, "channel": "linkedin", "step_type": "connection_note", "delay_hours": 0, "length_limit": 300},
                {"sequence_number": 2, "channel": "linkedin", "step_type": "first_message", "delay_hours": 24, "length_limit": 500},
                {"sequence_number": 3, "channel": "linkedin", "step_type": "follow_up", "delay_hours": 48, "length_limit": 500},
                {"sequence_number": 4, "channel": "email", "step_type": "email_outbound", "delay_hours": 72, "length_limit": 1000},
            ]
        },
        "freelance_clients": {
            "prompt_name": "Freelance Consulting Pack",
            "prompt_template": "Hi {recipient_name}, saw your work at {business_name}. We provide AI software engineering services for {selected_service}. Let us know if you would like to review our portfolio.",
            "writing_policy": "Direct B2B consulting style. No buzzwords.",
            "claims_policy": "Portfolio references matched against verified client evidence.",
            "sequence_templates": [
                {"sequence_number": 1, "channel": "linkedin", "step_type": "connection_note", "delay_hours": 0, "length_limit": 300},
                {"sequence_number": 2, "channel": "linkedin", "step_type": "first_message", "delay_hours": 24, "length_limit": 500},
                {"sequence_number": 3, "channel": "linkedin", "step_type": "follow_up", "delay_hours": 48, "length_limit": 500},
            ]
        },
        "masters_scholarship": {
            "prompt_name": "Academic Scholarship Inquiry Pack",
            "prompt_template": "Dear Professor {professor_name}, I am writing to express my strong interest in your research on {research_area} at {university}. My academic background aligns closely with your lab.",
            "writing_policy": "Respectful, academic formal tone.",
            "claims_policy": "Academic credentials matched strictly against candidate transcripts and publications.",
            "sequence_templates": [
                {"sequence_number": 1, "channel": "email", "step_type": "email_outbound", "delay_hours": 0, "length_limit": 1000},
                {"sequence_number": 2, "channel": "email", "step_type": "follow_up", "delay_hours": 72, "length_limit": 800},
                {"sequence_number": 3, "channel": "linkedin", "step_type": "connection_note", "delay_hours": 120, "length_limit": 300},
            ]
        },
    }

    for slug, data in system_purposes_data.items():
        purpose = CampaignPurpose.objects.filter(slug=slug, is_system=True).first()
        if not purpose:
            continue

        # 1. PromptPack & Version
        pack, _ = PromptPack.objects.get_or_create(
            campaign_purpose=purpose,
            name=data["prompt_name"],
            defaults={"version": 1, "is_active": True}
        )

        template_text = data["prompt_template"]
        checksum = hashlib.sha256(template_text.encode("utf-8")).hexdigest()

        PromptPackVersion.objects.get_or_create(
            prompt_pack=pack,
            version_number=1,
            defaults={
                "prompt_template": template_text,
                "context_schema": purpose.context_schema,
                "writing_policy": data["writing_policy"],
                "claims_policy": data["claims_policy"],
                "checksum": checksum,
            }
        )

        # 2. CampaignPurposeVersion
        p_checksum = hashlib.sha256(f"{slug}:v1".encode("utf-8")).hexdigest()
        CampaignPurposeVersion.objects.get_or_create(
            campaign_purpose=purpose,
            version_number=1,
            defaults={
                "writing_policy_version": "v1",
                "claims_policy_version": "v1",
                "context_schema": purpose.context_schema,
                "checksum": p_checksum,
            }
        )

        # 3. Purpose Sequence Templates
        for st in data["sequence_templates"]:
            CampaignPurposeSequenceTemplate.objects.get_or_create(
                campaign_purpose=purpose,
                sequence_number=st["sequence_number"],
                defaults={
                    "channel": st["channel"],
                    "step_type": st["step_type"],
                    "delay_hours": st["delay_hours"],
                    "length_limit": st["length_limit"],
                    "approval_requirement": True,
                    "sending_policy": "draft_only",
                }
            )

    # 4. Backfill existing campaigns with sequence steps from templates idempotently
    for campaign in Campaign.objects.all():
        if campaign.campaign_purpose:
            templates = CampaignPurposeSequenceTemplate.objects.filter(campaign_purpose=campaign.campaign_purpose)
            for tmpl in templates:
                MessageSequenceStep.objects.get_or_create(
                    campaign=campaign,
                    sequence_number=tmpl.sequence_number,
                    defaults={
                        "channel": tmpl.channel,
                        "step_type": tmpl.step_type,
                        "delay_hours": tmpl.delay_hours,
                        "length_limit": tmpl.length_limit,
                        "approval_requirement": True,
                        "sending_policy": "draft_only",
                        "is_enabled": True,
                    }
                )

def rollback_seed(apps, schema_editor):
    pass

class Migration(migrations.Migration):

    dependencies = [
        ('core', '0028_sequence_template_and_message_draft'),
    ]

    operations = [
        migrations.RunPython(seed_prompt_policy_and_templates, rollback_seed),
    ]
