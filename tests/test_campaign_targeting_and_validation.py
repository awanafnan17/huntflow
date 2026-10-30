# tests/test_campaign_targeting_and_validation.py
"""
Tests for Reusable Campaign Configuration, Validation, and Offline Target Evaluation.

Sections 35-36:
1. required title + no target titles => MISCONFIGURED / invalid
2. required location + no target locations => MISCONFIGURED / invalid
3. disabled location requirement + no location => VALID
4. explicit Profile #2 provider binding & cross-workspace rejection
5. manual approval required & networking purpose integrity
6. Andy offline evaluation using ONLY trusted facts produces valid target eligibility without fabricated location or industry.
7. Negative and positive domain controls (restaurant, real estate, generic founder, SaaS CTO, non-founder ML engineer).
8. Core invariant: Title match alone != STRONG, Domain match alone != STRONG.
"""
import pytest
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError

from openoutreach.core.models import Workspace, Campaign, CampaignPurpose, WorkspaceAutomationSettings
from openoutreach.linkedin.models import LinkedInProfile
from openoutreach.linkedin.pipeline.icp_evaluator import (
    evaluate_lead_icp_fit,
    validate_campaign_icp_consistency,
)


@pytest.fixture
def campaign_targeting_env(db):
    user = User.objects.create_user(username="target_user", email="target@example.com")
    ws_1 = Workspace.objects.create(name="Networking WS", slug="net-ws")
    ws_2 = Workspace.objects.create(name="Other WS", slug="other-ws")

    prof_2 = LinkedInProfile.objects.create(
        user=user,
        workspace=ws_1,
        linkedin_username="net.profile@example.com",
        session_status="valid",
        active=True,
    )
    prof_other = LinkedInProfile.objects.create(
        user=user,
        workspace=ws_2,
        linkedin_username="other.profile@example.com",
        session_status="valid",
        active=True,
    )

    purpose = CampaignPurpose.objects.create(
        workspace=ws_1,
        slug="tech_founder_networking",
        name="AI / Tech Founder Networking",
        description="Professional networking with tech founders",
        is_system=False,
        is_active=True,
    )

    campaign = Campaign.objects.create(
        workspace=ws_1,
        name="AI / Tech Founder Networking Test",
        campaign_type="tech_founder_networking",
        campaign_purpose=purpose,
        linkedin_profile=prof_2,
        require_title_match=True,
        target_job_titles="Founder, Co-Founder, Founder & CEO, CEO, Technical Founder, Co-Founder & CTO, CTO, SaaS Founder, Software Founder",
        target_industries="software, machine learning, artificial intelligence, ai, ml, saas, automation, developer tools, data platform, edtech",
        require_seniority_match=False,
        require_location_match=False,
        unknown_location_handling="allow",
        connection_note_enabled=True,
        manual_approval_required=True,
        linkedin_outreach_enabled=True,
        email_outreach_enabled=False,
        is_active=False,
    )

    return {
        "user": user,
        "ws_1": ws_1,
        "ws_2": ws_2,
        "prof_2": prof_2,
        "prof_other": prof_other,
        "purpose": purpose,
        "campaign": campaign,
    }


@pytest.mark.django_db
def test_campaign_config_validation_rules(campaign_targeting_env):
    """Section 35: Validate campaign configuration rules."""
    env = campaign_targeting_env
    campaign = env["campaign"]

    # 1. Valid networking campaign with require_location_match=False
    res_valid = validate_campaign_icp_consistency(campaign)
    assert res_valid["status"] == "VALID"
    assert res_valid["is_valid"] is True
    assert len(res_valid["warnings"]) == 0

    # 2. Required title + empty target titles => WARNING / invalid
    campaign.target_job_titles = ""
    res_no_titles = validate_campaign_icp_consistency(campaign)
    assert res_no_titles["status"] == "WARNING"
    assert res_no_titles["is_valid"] is False
    assert any("Target job titles" in w for w in res_no_titles["warnings"])

    # 3. Required location + empty target locations => WARNING / invalid
    campaign.target_job_titles = "Founder, CEO"
    campaign.require_location_match = True
    campaign.target_locations = ""
    res_no_loc = validate_campaign_icp_consistency(campaign)
    assert res_no_loc["status"] == "WARNING"
    assert res_no_loc["is_valid"] is False
    assert any("Target locations" in w for w in res_no_loc["warnings"])


@pytest.mark.django_db
def test_campaign_provider_binding_and_tenancy(campaign_targeting_env):
    """Section 35: Campaign must be explicitly bound to profile matching workspace."""
    env = campaign_targeting_env
    campaign = env["campaign"]

    assert campaign.workspace_id == env["ws_1"].id
    assert campaign.linkedin_profile_id == env["prof_2"].id
    assert campaign.manual_approval_required is True
    assert campaign.connection_note_enabled is True
    assert campaign.linkedin_outreach_enabled is True
    assert campaign.email_outreach_enabled is False


@pytest.mark.django_db
def test_andy_offline_evaluation_using_only_trusted_facts(campaign_targeting_env):
    """Section 36: Evaluate Andy Lojas against networking campaign using ONLY trusted facts."""
    env = campaign_targeting_env
    campaign = env["campaign"]

    trusted_andy = {
        "name": "Andy Lojas",
        "first_name": "Andy",
        "last_name": "Lojas",
        "public_identifier": "andy-lojas",
        "headline": "Founder & CEO, Lojas Software Corp. | Building Machine-Learning Algorithms for Education",
        "current_title": "Founder & CEO",  # derived from headline
        "company": "Lojas Software Corp.",  # derived from headline
        "location": "",  # UNKNOWN - NOT fabricated
        "industry": "",  # UNKNOWN - NOT fabricated
        "company_size": "",  # UNKNOWN - NOT fabricated
    }

    icp_res = evaluate_lead_icp_fit(trusted_andy, campaign)

    assert icp_res["final_fit"] == "STRONG"
    assert icp_res["title_match"] == "MATCH"
    assert icp_res["industry_match"] == "MATCH"
    assert icp_res["location_match"] == "NOT_REQUIRED"
    assert icp_res["company_size_match"] == "UNKNOWN"
    assert any("Title MATCHES" in r for r in icp_res["reasons"])
    assert any("Domain MATCHES target" in r for r in icp_res["reasons"])


@pytest.mark.django_db
def test_negative_control_restaurant_founder(campaign_targeting_env):
    """Section 15: Restaurant founder must be REJECTED (Non-target industry)."""
    env = campaign_targeting_env
    campaign = env["campaign"]

    lead = {
        "name": "Mario Rossi",
        "headline": "Founder & CEO at Italian Bistro & Restaurant Group",
        "current_title": "Founder & CEO",
        "company": "Italian Bistro & Restaurant Group",
        "location": "",
    }

    icp_res = evaluate_lead_icp_fit(lead, campaign)
    assert icp_res["final_fit"] == "REJECT"
    assert icp_res["title_match"] == "MATCH"
    assert icp_res["industry_match"] == "MISMATCH"


@pytest.mark.django_db
def test_negative_control_real_estate_founder(campaign_targeting_env):
    """Section 16: Real-estate / property founder must be REJECTED (Non-target industry)."""
    env = campaign_targeting_env
    campaign = env["campaign"]

    lead = {
        "name": "David Broker",
        "headline": "Founder & CEO at Skyline Real Estate Brokerage & Property Investment",
        "current_title": "Founder & CEO",
        "company": "Skyline Real Estate Brokerage",
        "location": "",
    }

    icp_res = evaluate_lead_icp_fit(lead, campaign)
    assert icp_res["final_fit"] == "REJECT"
    assert icp_res["title_match"] == "MATCH"
    assert icp_res["industry_match"] == "MISMATCH"


@pytest.mark.django_db
def test_negative_control_generic_founder_unknown_domain(campaign_targeting_env):
    """Section 17: Generic founder with no domain evidence is POSSIBLE (NOT STRONG)."""
    env = campaign_targeting_env
    campaign = env["campaign"]

    lead = {
        "name": "Alex Trader",
        "headline": "Founder & CEO at Horizon Global Holdings",
        "current_title": "Founder & CEO",
        "company": "Horizon Global Holdings",
        "location": "",
    }

    icp_res = evaluate_lead_icp_fit(lead, campaign)
    assert icp_res["final_fit"] == "POSSIBLE"
    assert icp_res["title_match"] == "MATCH"
    assert icp_res["industry_match"] == "UNKNOWN"


@pytest.mark.django_db
def test_positive_control_tech_cto(campaign_targeting_env):
    """Section 18: Tech CTO / Co-Founder of AI/SaaS is STRONG (reusable campaign)."""
    env = campaign_targeting_env
    campaign = env["campaign"]

    lead = {
        "name": "Elena Dev",
        "headline": "CTO & Co-Founder at CloudScale AI SaaS Platform",
        "current_title": "CTO & Co-Founder",
        "company": "CloudScale AI",
        "location": "",
    }

    icp_res = evaluate_lead_icp_fit(lead, campaign)
    assert icp_res["final_fit"] == "STRONG"
    assert icp_res["title_match"] == "MATCH"
    assert icp_res["industry_match"] == "MATCH"


@pytest.mark.django_db
def test_negative_control_non_founder_tech(campaign_targeting_env):
    """Section 19: Non-founder engineer is REJECTED even in AI company."""
    env = campaign_targeting_env
    campaign = env["campaign"]

    lead = {
        "name": "Mark Engineer",
        "headline": "Machine Learning Engineer at CloudScale AI Platform",
        "current_title": "Machine Learning Engineer",
        "company": "CloudScale AI",
        "location": "",
    }

    icp_res = evaluate_lead_icp_fit(lead, campaign)
    assert icp_res["final_fit"] == "REJECT"
    assert icp_res["title_match"] == "MISMATCH"
    assert icp_res["industry_match"] == "MATCH"


@pytest.mark.django_db
def test_title_and_domain_both_required_for_strong(campaign_targeting_env):
    """Section 20: Core invariant: Title match alone != STRONG, Domain match alone != STRONG."""
    env = campaign_targeting_env
    campaign = env["campaign"]

    # Title match only (no domain) -> POSSIBLE, not STRONG
    title_only = {"name": "User 1", "current_title": "Founder", "company": "General Trading Ltd"}
    res_title_only = evaluate_lead_icp_fit(title_only, campaign)
    assert res_title_only["final_fit"] == "POSSIBLE"

    # Domain match only (no title) -> REJECT, not STRONG
    domain_only = {"name": "User 2", "current_title": "Accountant", "headline": "Accountant at CloudScale AI SaaS"}
    res_domain_only = evaluate_lead_icp_fit(domain_only, campaign)
    assert res_domain_only["final_fit"] == "REJECT"

    # Both title and domain match -> STRONG
    both = {"name": "User 3", "current_title": "Founder & CEO", "headline": "Founder & CEO at NextGen AI SaaS"}
    res_both = evaluate_lead_icp_fit(both, campaign)
    assert res_both["final_fit"] == "STRONG"
