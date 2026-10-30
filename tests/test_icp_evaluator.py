# tests/test_icp_evaluator.py
import pytest
from openoutreach.core.models import Campaign, Workspace
from openoutreach.crm.models.lead import Lead
from openoutreach.linkedin.pipeline.icp_evaluator import (
    evaluate_lead_icp_fit, extract_title_seniority
)
from openoutreach.linkedin.services.discovery_service import discover_campaign_targets


@pytest.fixture
def test_campaign(db):
    ws = Workspace.objects.create(name="ICP Test WS", slug="icp-test-ws")
    c = Campaign.objects.create(
        name="Targeting Test Campaign",
        workspace=ws,
        target_job_titles="Founder, CEO, CTO, Head of Product",
        target_seniority="Executive, C-Level, Head",
        target_industries="Software, IT, Web",
        target_locations="United Kingdom, Germany, Europe",
        target_company_size="11-50",
        is_active=False
    )
    return c


# =====================================================================
# 1. TITLE MATCH MATRIX TESTS
# =====================================================================

@pytest.mark.parametrize("title_input, expected_match", [
    ("Founder & Co-founder", "MATCH"),
    ("Co-founder at TechCorp", "MATCH"),
    ("Chief Executive Officer (CEO)", "MATCH"),
    ("Chief Technology Officer", "MATCH"),
    ("Head of Product", "MATCH"),
    ("VP of Product", "POSSIBLE"),
    ("Director of Product", "POSSIBLE"),
    ("Product Lead", "POSSIBLE"),
    ("Owner & Director", "POSSIBLE"),
    ("E-commerce Manager at Prusa Research", "MISMATCH"),
    ("Engineering Manager | 30+ Years Tech", "MISMATCH"),
    ("Senior Product Manager", "MISMATCH"),
    ("Account Executive", "MISMATCH"),
    ("Software Engineer", "MISMATCH"),
    ("", "UNKNOWN"),
])
def test_title_match_matrix(test_campaign, title_input, expected_match):
    profile = {"current_title": title_input}
    res = evaluate_lead_icp_fit(profile, test_campaign)
    assert res["title_match"] == expected_match


# =====================================================================
# 2. SENIORITY EXTRACTION & MATCH MATRIX TESTS
# =====================================================================

@pytest.mark.parametrize("title_input, expected_seniority, expected_match", [
    ("Co-founder", "Executive", "MATCH"),
    ("Chief Technology Officer", "C-Level", "MATCH"),
    ("Head of Product", "Head", "MATCH"),
    ("E-commerce Manager", "Manager", "MISMATCH"),
    ("Engineering Manager", "Manager", "MISMATCH"),
    ("Senior Software Engineer", "Senior", "MISMATCH"),
])
def test_seniority_match_matrix(test_campaign, title_input, expected_seniority, expected_match):
    profile = {"current_title": title_input}
    res = evaluate_lead_icp_fit(profile, test_campaign)
    assert res["extracted_seniority"] == expected_seniority
    assert res["seniority_match"] == expected_match


# =====================================================================
# 3. LOCATION MATCH MATRIX TESTS
# =====================================================================

@pytest.mark.parametrize("location_input, expected_match", [
    ("Manchester, England, United Kingdom", "MATCH"),
    ("Berlin, Germany", "MATCH"),
    ("Prague, Czechia", "MATCH"),
    ("Dubai, United Arab Emirates", "MISMATCH"),
    ("Perak, Malaysia", "MISMATCH"),
    ("", "UNKNOWN"),
])
def test_location_match_matrix(test_campaign, location_input, expected_match):
    profile = {"current_title": "CTO", "location": location_input}
    res = evaluate_lead_icp_fit(profile, test_campaign)
    assert res["location_match"] == expected_match


# =====================================================================
# 4. FINAL FIT MATRIX TESTS
# =====================================================================

def test_final_fit_strong(test_campaign):
    """A. CTO, Software Industry, UK Location -> STRONG FIT."""
    profile = {
        "current_title": "Chief Technology Officer",
        "company": "CloudScale Ltd",
        "industry": "Software",
        "location": "London, United Kingdom",
    }
    res = evaluate_lead_icp_fit(profile, test_campaign)
    assert res["final_fit"] == "STRONG"
    assert res["title_match"] == "MATCH"
    assert res["location_match"] == "MATCH"


def test_final_fit_ecommerce_manager_reject(test_campaign):
    """B. E-commerce Manager -> REJECT (Title Mismatch)."""
    profile = {
        "current_title": "E-commerce Manager",
        "company": "Prusa Research",
        "location": "Prague, Czechia",
    }
    res = evaluate_lead_icp_fit(profile, test_campaign)
    assert res["final_fit"] == "REJECT"
    assert res["title_match"] == "MISMATCH"


def test_final_fit_cofounder_dubai_reject(test_campaign):
    """C. Co-founder in Dubai -> REJECT (Location Mismatch)."""
    profile = {
        "current_title": "Co-founder",
        "company": "Game Studio",
        "location": "Dubai, United Arab Emirates",
    }
    res = evaluate_lead_icp_fit(profile, test_campaign)
    assert res["final_fit"] == "REJECT"
    assert res["location_match"] == "MISMATCH"


def test_final_fit_url_only_possible(test_campaign):
    """D. Empty profile facts -> POSSIBLE (Needs Review)."""
    profile = {}
    res = evaluate_lead_icp_fit(profile, test_campaign)
    assert res["final_fit"] == "POSSIBLE"
    assert res["title_match"] == "UNKNOWN"


# =====================================================================
# 5. ICP CONFIGURATION VALIDATOR TESTS
# =====================================================================

def test_validate_campaign_icp_valid(test_campaign):
    """Verify valid ICP configuration returns status VALID."""
    from openoutreach.linkedin.pipeline.icp_evaluator import validate_campaign_icp_consistency
    test_campaign.target_job_titles = "Founder, CEO, CTO"
    test_campaign.target_seniority = "Executive, C-Level"
    test_campaign.save()

    res = validate_campaign_icp_consistency(test_campaign)
    assert res["status"] == "VALID"
    assert res["is_valid"] is True
    assert len(res["warnings"]) == 0


def test_validate_campaign_icp_seniority_conflict(test_campaign):
    """Verify title-seniority conflict returns WARNING status."""
    from openoutreach.linkedin.pipeline.icp_evaluator import validate_campaign_icp_consistency
    test_campaign.target_job_titles = "Head of Product"
    test_campaign.target_seniority = "Executive, C-Level"
    test_campaign.save()

    res = validate_campaign_icp_consistency(test_campaign)
    assert res["status"] == "WARNING"
    assert res["is_valid"] is False
    assert any("Seniority Conflict" in w for w in res["warnings"])


def test_validate_campaign_icp_head_resolved(test_campaign):
    """Verify adding Head to Target Seniority resolves conflict."""
    from openoutreach.linkedin.pipeline.icp_evaluator import validate_campaign_icp_consistency
    test_campaign.target_job_titles = "Head of Product"
    test_campaign.target_seniority = "Executive, C-Level, Head"
    test_campaign.save()

    res = validate_campaign_icp_consistency(test_campaign)
    assert res["status"] == "VALID"
    assert res["is_valid"] is True
    assert len(res["warnings"]) == 0
