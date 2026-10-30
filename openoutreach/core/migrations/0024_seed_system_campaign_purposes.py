from django.db import migrations

def seed_purposes_and_backfill(apps, schema_editor):
    CampaignPurpose = apps.get_model("core", "CampaignPurpose")
    Campaign = apps.get_model("core", "Campaign")

    purposes_data = [
        {
            "slug": "job_outreach",
            "name": "Job Outreach",
            "description": "Outreach to founders, CEOs, hiring managers, and talent partners for career opportunities.",
            "target_type": "hiring_decision_maker",
            "objective": "Secure job interviews and career conversations.",
            "supported_channels": ["linkedin", "email"],
            "required_recipient_fields": ["first_name", "company", "current_title"],
            "required_sender_facts": ["career_summary", "verified_skills"],
            "required_personalization_facts": ["company_fact", "role_alignment"],
            "max_connection_note_length": 300,
            "max_follow_ups": 3,
            "is_system": True,
        },
        {
            "slug": "geosentry_mining",
            "name": "GeoSentry Mining",
            "description": "Desktop screening and remote sensing reports for mining executives and exploration teams.",
            "target_type": "mining_executive",
            "objective": "Offer GeoSentry desktop geological screening reports.",
            "supported_channels": ["linkedin", "email"],
            "required_recipient_fields": ["first_name", "company", "location"],
            "required_sender_facts": ["geosentry_capabilities"],
            "required_personalization_facts": ["asset_commodity", "project_location"],
            "max_connection_note_length": 300,
            "max_follow_ups": 3,
            "is_system": True,
        },
        {
            "slug": "freelance_clients",
            "name": "Freelance Clients",
            "description": "B2B client acquisition for engineering, AI development, and software consulting services.",
            "target_type": "business_owner",
            "objective": "Secure freelance consulting engagements and service contracts.",
            "supported_channels": ["linkedin", "email"],
            "required_recipient_fields": ["first_name", "company"],
            "required_sender_facts": ["portfolio_evidence", "service_offerings"],
            "required_personalization_facts": ["business_need"],
            "max_connection_note_length": 300,
            "max_follow_ups": 3,
            "is_system": True,
        },
        {
            "slug": "masters_scholarship",
            "name": "Masters Scholarship",
            "description": "Academic outreach to university professors, PIs, and lab directors for research supervision and scholarships.",
            "target_type": "professor",
            "objective": "Inquire about research supervision, lab openings, and fully funded scholarship positions.",
            "supported_channels": ["email", "linkedin"],
            "required_recipient_fields": ["first_name", "university", "department"],
            "required_sender_facts": ["academic_candidacy_facts", "publication_record"],
            "required_personalization_facts": ["lab_research_alignment"],
            "max_connection_note_length": 300,
            "max_follow_ups": 3,
            "is_system": True,
        },
    ]

    purposes_by_slug = {}
    for data in purposes_data:
        p, _ = CampaignPurpose.objects.get_or_create(
            slug=data["slug"],
            workspace=None,
            defaults=data
        )
        purposes_by_slug[data["slug"]] = p

    # Backfill existing campaigns
    for campaign in Campaign.objects.all():
        if campaign.campaign_type in purposes_by_slug:
            campaign.campaign_purpose = purposes_by_slug[campaign.campaign_type]
            campaign.save()

def rollback_seed(apps, schema_editor):
    pass

class Migration(migrations.Migration):

    dependencies = [
        ('core', '0023_campaignpurpose_campaign_campaign_purpose_and_more'),
    ]

    operations = [
        migrations.RunPython(seed_purposes_and_backfill, rollback_seed),
    ]
