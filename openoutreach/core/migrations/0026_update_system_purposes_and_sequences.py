from django.db import migrations

def update_purposes_and_sequences(apps, schema_editor):
    CampaignPurpose = apps.get_model("core", "CampaignPurpose")
    Campaign = apps.get_model("core", "Campaign")
    MessageSequenceStep = apps.get_model("core", "MessageSequenceStep")

    schemas = {
        "job_outreach": {
            "required_fields": ["recipient_name", "recipient_role", "company_name", "reason_for_connecting", "verified_candidate_facts"],
            "field_types": {"recipient_name": "str", "recipient_role": "str", "company_name": "str", "reason_for_connecting": "str", "verified_candidate_facts": "list"},
            "max_lengths": {"recipient_name": 100, "company_name": 100}
        },
        "geosentry_mining": {
            "required_fields": ["recipient_name", "recipient_role", "company_name", "project_or_asset", "jurisdiction", "commodity", "verified_public_context", "geosentry_service"],
            "field_types": {"recipient_name": "str", "recipient_role": "str", "company_name": "str", "project_or_asset": "str", "jurisdiction": "str", "commodity": "str", "verified_public_context": "list", "geosentry_service": "str"},
            "max_lengths": {"recipient_name": 100}
        },
        "freelance_clients": {
            "required_fields": ["recipient_name", "business_name", "recipient_role", "selected_service", "verified_business_context", "verified_portfolio_facts"],
            "field_types": {"recipient_name": "str", "business_name": "str", "recipient_role": "str", "selected_service": "str", "verified_business_context": "list", "verified_portfolio_facts": "list"},
            "max_lengths": {"recipient_name": 100}
        },
        "masters_scholarship": {
            "required_fields": ["professor_name", "university", "department", "research_area", "verified_publication_or_project", "verified_candidate_academic_facts", "intended_intake"],
            "field_types": {"professor_name": "str", "university": "str", "department": "str", "research_area": "str", "verified_publication_or_project": "str", "verified_candidate_academic_facts": "list", "intended_intake": "str"},
            "max_lengths": {"professor_name": 100}
        }
    }

    for slug, context_schema in schemas.items():
        CampaignPurpose.objects.filter(slug=slug, is_system=True).update(context_schema=context_schema)

    # Seed default sequence steps on existing campaigns
    sequence_templates = {
        "job_outreach": [
            {"sequence_number": 1, "channel": "linkedin", "step_type": "connection_note", "delay_hours": 0, "length_limit": 300},
            {"sequence_number": 2, "channel": "linkedin", "step_type": "first_message", "delay_hours": 24, "length_limit": 500},
            {"sequence_number": 3, "channel": "linkedin", "step_type": "follow_up", "delay_hours": 48, "length_limit": 500},
        ],
        "geosentry_mining": [
            {"sequence_number": 1, "channel": "linkedin", "step_type": "connection_note", "delay_hours": 0, "length_limit": 300},
            {"sequence_number": 2, "channel": "linkedin", "step_type": "first_message", "delay_hours": 24, "length_limit": 500},
            {"sequence_number": 3, "channel": "linkedin", "step_type": "follow_up", "delay_hours": 48, "length_limit": 500},
            {"sequence_number": 4, "channel": "email", "step_type": "email_outbound", "delay_hours": 72, "length_limit": 1000},
        ],
        "freelance_clients": [
            {"sequence_number": 1, "channel": "linkedin", "step_type": "connection_note", "delay_hours": 0, "length_limit": 300},
            {"sequence_number": 2, "channel": "linkedin", "step_type": "first_message", "delay_hours": 24, "length_limit": 500},
            {"sequence_number": 3, "channel": "linkedin", "step_type": "follow_up", "delay_hours": 48, "length_limit": 500},
        ],
        "masters_scholarship": [
            {"sequence_number": 1, "channel": "email", "step_type": "email_outbound", "delay_hours": 0, "length_limit": 1000},
            {"sequence_number": 2, "channel": "email", "step_type": "follow_up", "delay_hours": 72, "length_limit": 800},
            {"sequence_number": 3, "channel": "linkedin", "step_type": "connection_note", "delay_hours": 120, "length_limit": 300},
        ],
    }

    for campaign in Campaign.objects.all():
        ctype = campaign.campaign_type
        if ctype in sequence_templates:
            for step_data in sequence_templates[ctype]:
                MessageSequenceStep.objects.get_or_create(
                    campaign=campaign,
                    sequence_number=step_data["sequence_number"],
                    defaults={
                        "channel": step_data["channel"],
                        "step_type": step_data["step_type"],
                        "delay_hours": step_data["delay_hours"],
                        "length_limit": step_data["length_limit"],
                        "approval_requirement": True,
                        "sending_policy": "draft_only",
                        "is_enabled": True
                    }
                )

def rollback_update(apps, schema_editor):
    pass

class Migration(migrations.Migration):

    dependencies = [
        ('core', '0025_remove_empty_legacy_workspace'),
    ]

    operations = [
        migrations.RunPython(update_purposes_and_sequences, rollback_update),
    ]
