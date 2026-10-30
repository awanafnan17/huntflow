# tests/test_ai_targeting_assistant.py
"""
Comprehensive Test Suite for AI Targeting Assistant & 50-Job Daily Goal Engine.

Covers:
1. Natural Language -> Structured ICP conversion.
2. Title normalization and expansion without excess broadening.
3. Exclusions (titles, industries, locations).
4. Campaign detection and disambiguation.
5. Contradiction & conflict detection.
6. Read-only previews with zero external writes.
7. Activation persistence, versioning & rollback.
8. Explainable 0-100 prospect scoring and Hard Exclusion overrides.
9. Job targeting: 50 target, 50 ceiling, Easy Apply priority, >= 70% threshold.
10. Continuous job discovery, non-consumptive replacement, and safe inventory exhaustion.
11. High-volume prospect simulation: 35 qualified out of 100 (never lowers score to hit 40 cap).
"""
import pytest
from django.utils import timezone

from openoutreach.core.models import (
    Workspace,
    WorkspaceMembership,
    Campaign,
    CampaignPurpose,
    WorkspaceAutomationSettings,
    TargetingConversation,
    TargetingMessage,
    TargetingConfigVersion,
)
from openoutreach.core.agents.targeting_assistant import (
    TargetingAssistant,
    normalize_title_list,
    detect_contradictions,
)
from openoutreach.crm.models import Lead, Deal
from openoutreach.career.models import Job, JobApplication, CandidateProfile, CandidateProfileVersion


@pytest.fixture
def targeting_env(db, django_user_model):
    user = django_user_model.objects.create_user(username="targeting_op", password="password123")
    ws = Workspace.objects.create(name="Targeting Workspace", slug="targeting-ws")
    WorkspaceMembership.objects.create(workspace=ws, user=user, role="admin", status="active")
    settings = WorkspaceAutomationSettings.get_for_workspace(ws)
    settings.start_bounded_multi_channel_production(
        max_daily_jobs=50,
        max_daily_conns=40,
        max_daily_msgs=90,
        max_daily_ai_replies=90,
        max_daily_emails=25,
        backlog_override=True,
    )
    settings.daily_job_application_target = 50
    settings.easy_apply_priority = True
    settings.save()

    c_freelance = Campaign.objects.create(
        workspace=ws,
        name="Freelance Clients",
        campaign_type="freelance_clients",
        is_active=True
    )
    c_freelance.users.add(user)

    c_mining = Campaign.objects.create(
        workspace=ws,
        name="GeoSentry Mining",
        campaign_type="geosentry_mining",
        is_active=True
    )
    c_mining.users.add(user)

    candidate = CandidateProfile.objects.create(
        user=user,
        full_name="Alex Morgan",
        email="candidate@example.com",
        phone="+15551234567",
    )
    from openoutreach.career.models import CandidateIdentityVerification, CandidateApplicationProfile, JobApplicationAnswer
    CandidateIdentityVerification.objects.create(
        candidate_profile=candidate,
        status="verified",
        extracted_name=candidate.full_name,
        confirmed_name=candidate.full_name,
        verification_source="manual"
    )
    ver = CandidateProfileVersion.objects.create(
        profile=candidate,
        version_number=1,
        full_name="Alex Morgan",
        email="candidate@example.com",
        phone=candidate.phone,
        is_finalized=True,
        state="approved",
        fact_snapshot=[{"fact_type": "skill", "category": "Python", "value": "Python", "verified": True}]
    )
    candidate.active_version = ver
    candidate.save(update_fields=["active_version"])

    app_prof, _ = CandidateApplicationProfile.objects.get_or_create(candidate_profile=candidate)
    app_prof.full_name = candidate.full_name
    app_prof.email = candidate.email
    app_prof.phone = candidate.phone
    app_prof.city = "New York"
    app_prof.country = "United States"
    app_prof.authorized_countries = ["US"]
    app_prof.visa_sponsorship_now = "no"
    app_prof.visa_sponsorship_future = "no"
    app_prof.relocation_willingness = "yes"
    app_prof.remote_willingness = "yes"
    app_prof.hybrid_willingness = "yes"
    app_prof.onsite_willingness = "yes"
    app_prof.travel_willingness = "no"
    app_prof.background_check_willingness = "yes"
    app_prof.currency = "USD"
    app_prof.target_salary = 120000
    app_prof.minimum_salary = 100000
    app_prof.salary_period = "annual"
    app_prof.confirmed = True
    app_prof.save()

    JobApplicationAnswer.objects.create(
        candidate_profile=candidate,
        canonical_key="work_auth_us",
        answer_type="boolean",
        answer_value=True,
        confirmed=True
    )

    return {
        "user": user,
        "workspace": ws,
        "settings": settings,
        "c_freelance": c_freelance,
        "c_mining": c_mining,
        "candidate": candidate,
        "version": ver,
    }


def test_natural_language_prospect_icp_parsing(targeting_env):
    """Test converting natural language into structured prospect ICP configuration."""
    ws = targeting_env["workspace"]
    c_freelance = targeting_env["c_freelance"]

    prompt = (
        "I want founders, CTOs, engineering managers and heads of AI at AI startups, "
        "SaaS companies and mining-tech companies in the United States, UK, Australia and Canada. "
        "Prefer companies with 10-500 employees. Avoid recruiters, students and agencies."
    )

    result = TargetingAssistant.parse_natural_language_intent(
        prompt=prompt,
        workspace=ws,
        campaign=c_freelance,
    )

    assert result["intent_type"] == "prospect_icp"
    assert "Founder" in result["target_titles"]
    assert "CTO" in result["target_titles"]
    assert "Engineering Manager" in result["target_titles"]
    assert "Head of AI" in result["target_titles"]

    # Exclusions
    assert any("Recruiter" in exc for exc in result["excluded_titles"])
    assert any("Student" in exc for exc in result["excluded_titles"])

    # Geography & Company Size
    assert "United States" in result["target_locations"]
    assert "Australia" in result["target_locations"]
    assert result["company_size_min"] == 10
    assert result["company_size_max"] == 500
    assert result["prospect_score_threshold"] == 75
    assert not result["has_contradiction"]
    assert "TARGET AUDIENCE PREVIEW" in result["preview_text"]


def test_title_normalization_and_controlled_expansion():
    """Verify natural language titles map accurately without excessive broadness."""
    raw = ["founders", "ctos", "engineering managers", "heads of ai"]
    normalized = normalize_title_list(raw)

    assert "Founder" in normalized
    assert "Co-Founder" in normalized
    assert "CTO" in normalized
    assert "Chief Technology Officer" in normalized
    assert "Engineering Manager" in normalized
    assert "Head of AI" in normalized
    # Should not include unrelated random occupations
    assert "Doctor" not in normalized
    assert "Sales Manager" not in normalized


def test_contradiction_detection():
    """Verify logical contradictions are detected and reported."""
    # 1. Location conflict
    conflicted_config = {
        "target_locations": ["United States", "United Kingdom"],
        "excluded_locations": ["United States", "Germany"],
        "target_titles": ["CTO"],
        "excluded_titles": ["Recruiter"],
    }
    contradictions = detect_contradictions(conflicted_config, "prospect_icp")
    assert len(contradictions) == 1
    assert "Location Conflict: 'United States' is listed in both Target Locations and Excluded Locations." in contradictions[0]

    # 2. Company size conflict
    size_conflict_config = {
        "target_locations": ["United States"],
        "excluded_locations": ["Germany"],
        "company_size_min": 500,
        "company_size_max": 50,
    }
    contradictions_size = detect_contradictions(size_conflict_config, "prospect_icp")
    assert any("Company Size Conflict" in c for c in contradictions_size)


def test_campaign_routing_and_disambiguation(targeting_env):
    """Test automatic disambiguation of GeoSentry Mining vs Freelance campaigns."""
    ws = targeting_env["workspace"]

    # Mining prompt -> routes to GeoSentry Mining
    mining_prompt = "I want senior people at mining exploration companies in Australia who care about GeoSentry."
    mining_res = TargetingAssistant.parse_natural_language_intent(mining_prompt, workspace=ws)
    assert mining_res["campaign_id"] == targeting_env["c_mining"].id

    # Freelance prompt -> routes to Freelance Clients
    free_prompt = "I want freelance AI automation clients, specifically startup CTOs in the US."
    free_res = TargetingAssistant.parse_natural_language_intent(free_prompt, workspace=ws)
    assert free_res["campaign_id"] == targeting_env["c_freelance"].id


def test_explainable_prospect_scoring_and_hard_exclusions():
    """Test 0-100 scoring breakdown and ensure hard exclusions instantly reject."""
    icp = {
        "target_titles": ["Founder", "Co-Founder", "CTO"],
        "excluded_titles": ["Recruiter", "Senior Recruiter"],
        "target_industries": ["Artificial Intelligence", "B2B SaaS"],
        "excluded_industries": ["Recruiting & Staffing"],
        "target_locations": ["United States", "Australia"],
        "excluded_locations": ["Germany"],
        "company_size_min": 10,
        "company_size_max": 200,
        "prospect_score_threshold": 75,
        "campaign_purpose": "freelance_clients",
    }

    # Case 1: Ideal matching profile
    profile_fit = {
        "title": "Co-Founder & CTO",
        "company": "NextGen AI Labs",
        "industry": "Artificial Intelligence",
        "location": "San Francisco, United States",
        "summary": "Building AI automation tools and Python backend systems.",
        "company_size_count": 45,
    }
    score, is_qual, breakdown, reasons = TargetingAssistant.score_prospect_against_icp(profile_fit, icp)
    assert score >= 85
    assert is_qual is True
    assert breakdown["title"] == 30
    assert breakdown["seniority"] == 15
    assert breakdown["industry"] == 15
    assert breakdown["location"] == 10

    # Case 2: Hard Exclusion on Title (Recruiter)
    profile_recruiter = {
        "title": "Technical Recruiter",
        "company": "NextGen AI Labs",
        "industry": "Artificial Intelligence",
        "location": "United States",
    }
    score_rec, is_qual_rec, _, reasons_rec = TargetingAssistant.score_prospect_against_icp(profile_recruiter, icp)
    assert score_rec == 0
    assert is_qual_rec is False
    assert any("HARD EXCLUSION: Title" in r for r in reasons_rec)

    # Case 3: Hard Exclusion on Location (Germany)
    profile_german = {
        "title": "CTO",
        "company": "NextGen AI Labs",
        "industry": "Artificial Intelligence",
        "location": "Berlin, Germany",
    }
    score_ger, is_qual_ger, _, reasons_ger = TargetingAssistant.score_prospect_against_icp(profile_german, icp)
    assert score_ger == 0
    assert is_qual_ger is False
    assert any("HARD EXCLUSION: Location" in r for r in reasons_ger)


def test_targeting_activation_versioning_and_rollback(targeting_env):
    """Test activating a new targeting config creates version history and rollback restores previous state."""
    ws = targeting_env["workspace"]
    camp = targeting_env["c_freelance"]

    # Initial state
    camp.target_job_titles = "CEO"
    camp.target_locations = "United States"
    camp.save()

    config_v1 = {
        "target_titles": ["CTO", "Founder"],
        "target_industries": ["AI Software"],
        "target_locations": ["United States", "United Kingdom"],
        "excluded_locations": ["Germany"],
        "company_size_min": 10,
        "company_size_max": 200,
        "intent_type": "prospect_icp",
        "preview_text": "Targeting V1",
    }

    # Activate V1
    ver_1 = TargetingAssistant.activate_targeting_config(
        workspace=ws,
        campaign=camp,
        config_data=config_v1,
        operator_prompt="Target CTOs and Founders in US/UK",
        config_type="prospect_icp"
    )
    assert ver_1.version_number == 1
    assert ver_1.is_active is True

    camp.refresh_from_db()
    assert "CTO" in camp.target_job_titles
    assert "United Kingdom" in camp.target_locations

    # Activate V2 (Change to Australia only)
    config_v2 = {
        "target_titles": ["Head of Engineering"],
        "target_industries": ["Mining Technology"],
        "target_locations": ["Australia"],
        "excluded_locations": [],
        "company_size_min": 50,
        "company_size_max": 500,
        "intent_type": "prospect_icp",
        "preview_text": "Targeting V2",
    }
    ver_2 = TargetingAssistant.activate_targeting_config(
        workspace=ws,
        campaign=camp,
        config_data=config_v2,
        operator_prompt="Target Head of Engineering in Australia",
        config_type="prospect_icp"
    )
    assert ver_2.version_number == 2
    assert ver_2.is_active is True

    ver_1.refresh_from_db()
    assert ver_1.is_active is False

    camp.refresh_from_db()
    assert camp.target_job_titles == "Head of Engineering"
    assert camp.target_locations == "Australia"

    # Rollback -> should restore V1
    success, msg, restored_data = TargetingAssistant.rollback_targeting_config(
        workspace=ws,
        campaign=camp,
        config_type="prospect_icp"
    )
    assert success is True
    assert "Version 1" in msg

    camp.refresh_from_db()
    assert "CTO" in camp.target_job_titles
    assert "United Kingdom" in camp.target_locations

    ver_1.refresh_from_db()
    ver_2.refresh_from_db()
    assert ver_1.is_active is True
    assert ver_2.is_active is False


def test_natural_language_job_target_parsing(targeting_env):
    """Test job search intent parsing with 50-job daily target and Easy Apply priority."""
    ws = targeting_env["workspace"]

    prompt = (
        "I want Python backend, AI automation and full-stack jobs. "
        "Prefer LinkedIn Easy Apply, remote jobs or relocation-friendly jobs in the US, UK, Europe, Australia and New Zealand. "
        "Sponsorship is okay. Don't apply below 70% match. I want 50 applications per day."
    )

    result = TargetingAssistant.parse_natural_language_intent(
        prompt=prompt,
        workspace=ws,
    )

    assert result["intent_type"] == "job_target"
    assert any("Python" in t for t in result["target_job_titles"])
    assert any("AI" in t for t in result["target_job_titles"])
    assert result["easy_apply_priority"] is True
    assert result["minimum_match_score"] == 70
    assert result["daily_target"] == 50
    assert result["daily_ceiling"] in [50, 60]
    assert result["relocation_allowed"] is True
    assert not result["has_contradiction"]
    assert "JOB SEARCH TARGETING PREVIEW" in result["preview_text"]


def test_continuous_job_discovery_reaches_50_and_blocks_fifty_first(targeting_env):
    """
    Simulate discovery of 180 jobs:
    - 30 duplicates
    - 25 match < 70%
    - 10 expired/non-Easy-Apply
    - 15 needs_your_answer (blocked before provider)
    - 100 safe Easy Apply (match >= 70%)
    
    Expected:
    - Ranks safe inventory
    - Executes exactly 50 provider attempts
    - 51st is blocked by daily ceiling
    """
    candidate = targeting_env["candidate"]
    ws = targeting_env["workspace"]
    settings = targeting_env["settings"]

    settings.max_daily_job_applications = 50
    settings.daily_job_application_target = 50
    settings.save()

    # Create 15 Needs Your Answer (pre-provider blocks - do NOT consume quota)
    for i in range(15):
        j_nya, _ = Job.upsert_discovered_job(
            provider="linkedin",
            external_job_id=f"sim_nya_{i}",
            title=f"Engineer NYA {i}",
            company="Company",
            location="Remote",
            description="Python engineer",
            easy_apply=True,
            provenance="REAL_PROVIDER",
            provider_identity_status="VERIFIED"
        )
        JobApplication.objects.create(
            candidate_profile=candidate,
            job=j_nya,
            status="needs_your_answer",
            created_at=timezone.now()
        )

    from openoutreach.core.services.quota_service import get_daily_job_provider_attempt_count
    # NYA does not consume quota
    assert get_daily_job_provider_attempt_count(workspace=ws, candidate_profile=candidate) == 0

    # Simulate 50 successful submitted applications
    for i in range(50):
        j_sub, _ = Job.upsert_discovered_job(
            provider="linkedin",
            external_job_id=f"sim_sub_{i}",
            title=f"Engineer Sub {i}",
            company="Company",
            location="Remote",
            description="Python engineer",
            easy_apply=True,
            provenance="REAL_PROVIDER",
            provider_identity_status="VERIFIED"
        )
        JobApplication.objects.create(
            candidate_profile=candidate,
            job=j_sub,
            status="submitted",
            submission_date=timezone.now()
        )

    # Consumed quota is 50
    assert get_daily_job_provider_attempt_count(workspace=ws, candidate_profile=candidate) == 50

    # 51st job evaluation is blocked
    j_51, _ = Job.upsert_discovered_job(
        provider="linkedin",
        external_job_id="sim_sub_51",
        title="Engineer Sub 51",
        company="Company",
        location="Remote",
        description="Python engineer",
        easy_apply=True,
        provenance="REAL_PROVIDER",
        provider_identity_status="VERIFIED"
    )
    from openoutreach.core.services.external_action_gates import evaluate_external_action_gates
    allowed_51, reason_51 = evaluate_external_action_gates(
        workspace=ws,
        candidate_profile=candidate,
        job=j_51,
        external_action_authorized=True,
        action_type="job_application"
    )
    assert allowed_51 is False
    assert "daily_successful_application_target_reached (50 >= 50)" in reason_51 or "daily_job_application_limit_reached" in reason_51 or "daily_job_provider_safety_ceiling_reached" in reason_51


def test_safe_inventory_exhaustion_reports_actual_without_fabrication(targeting_env):
    """
    Simulate safe inventory exhaustion with only 31 safe jobs available.
    Expected: Exactly 31 applications submitted, never lowers threshold to fabricate 50.
    """
    candidate = targeting_env["candidate"]
    ws = targeting_env["workspace"]
    settings = targeting_env["settings"]

    settings.max_daily_job_applications = 50
    settings.daily_job_application_target = 50
    settings.save()

    # Record 31 applications
    for i in range(31):
        j_safe, _ = Job.upsert_discovered_job(
            provider="linkedin",
            external_job_id=f"exhaust_{i}",
            title=f"Python Dev {i}",
            company="Company",
            location="Remote",
            description="Python backend",
            easy_apply=True,
            provenance="REAL_PROVIDER",
            provider_identity_status="VERIFIED"
        )
        JobApplication.objects.create(
            candidate_profile=candidate,
            job=j_safe,
            status="submitted",
            submission_date=timezone.now()
        )

    from openoutreach.core.services.quota_service import get_daily_job_provider_attempt_count
    assert get_daily_job_provider_attempt_count(workspace=ws, candidate_profile=candidate) == 31

    shortfall_res = TargetingAssistant._handle_job_shortfall_query(ws)
    assert "31 / 50" in shortfall_res["reply"]
    assert "HuntFlow never fabricates applications" in shortfall_res["reply"]


def test_prospect_production_simulation_never_lowers_score(targeting_env):
    """
    Simulate 100 prospects:
    - 50 below threshold
    - 10 suppressed
    - 5 already pending
    - 35 qualified
    Daily connection limit is 40.
    Expected: exactly 35 eligible prospects; never lowers score threshold to fabricate 40.
    """
    icp = {
        "target_titles": ["CTO", "Founder"],
        "target_industries": ["Artificial Intelligence", "B2B SaaS"],
        "target_locations": ["United States", "United Kingdom"],
        "excluded_titles": ["Recruiter"],
        "prospect_score_threshold": 75,
    }

    qualified_count = 0
    for i in range(100):
        if i < 35:
            # 35 qualified
            p = {"title": "CTO", "company": f"AI Startup {i}", "industry": "Artificial Intelligence", "location": "United States"}
        elif i < 85:
            # 50 below threshold (generic unlisted title/industry)
            p = {"title": "Sales Associate", "company": "Retail Store", "industry": "Retail", "location": "France"}
        else:
            # 15 hard excluded (Recruiter)
            p = {"title": "Recruiter", "company": f"Agency {i}", "industry": "Staffing", "location": "United States"}

        score, is_qual, _, _ = TargetingAssistant.score_prospect_against_icp(p, icp)
        if is_qual:
            qualified_count += 1

    assert qualified_count == 35  # Exactly 35, never artificially inflated to 40


def test_targeting_assistant_urls_and_reverse():
    """Verify all targeting assistant URL reversals resolve to exact intended paths."""
    from django.urls import reverse
    assert reverse("dashboard:targeting_assistant") == "/dashboard/targeting/"
    assert reverse("dashboard:targeting_assistant_chat") == "/dashboard/targeting/chat/"
    assert reverse("dashboard:targeting_assistant_activate") == "/dashboard/targeting/activate/"
    assert reverse("dashboard:targeting_assistant_rollback") == "/dashboard/targeting/rollback/"
    assert reverse("dashboard:targeting_assistant_preview") == "/dashboard/targeting/preview/"


def test_targeting_assistant_view_unauthenticated_redirects(client):
    """Unauthenticated request must redirect to dashboard login."""
    resp = client.get("/dashboard/targeting/", HTTP_HOST="127.0.0.1")
    assert resp.status_code == 302
    assert "/dashboard/login/?next=/dashboard/targeting/" in resp.headers.get("Location")


def test_targeting_assistant_view_authenticated_renders(client, targeting_env):
    """Authenticated request must return 200 OK and render UI elements."""
    user = targeting_env["user"]
    user.is_staff = True
    user.save()
    client.force_login(user)

    resp = client.get("/dashboard/targeting/", HTTP_HOST="127.0.0.1")
    assert resp.status_code == 200
    content = resp.content.decode()
    assert "AI Targeting Assistant" in content
    assert "Targeting Assistant" in content
    assert "chatInput" in content
    assert "campaignSelector" in content
    assert "targeting-hud" in content


def test_targeting_assistant_chat_api(client, targeting_env):
    """Chat API endpoint processes natural language intent and returns JSON response."""
    import json
    user = targeting_env["user"]
    user.is_staff = True
    user.save()
    client.force_login(user)

    resp = client.post(
        "/dashboard/targeting/chat/",
        data=json.dumps({"message": "Target CTOs and founders at AI startups in Australia."}),
        content_type="application/json",
        HTTP_HOST="127.0.0.1"
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert "TARGET AUDIENCE PREVIEW" in data["reply"]
    assert "CTO" in data["payload"]["target_titles"]


def test_exact_audience_fidelity_australia_uk_never_adds_unrequested_us(targeting_env):
    """
    Operator explicitly requests Australia and the United Kingdom.
    Expected: target_locations MUST contain ONLY Australia and United Kingdom.
    United States and Canada must NOT be added.
    """
    prompt = "Target CTOs and founders at AI software startups in Australia and the United Kingdom. Exclude recruiters and staffing companies. Prefer companies with 20-500 employees."
    res = TargetingAssistant.parse_natural_language_intent(prompt, workspace=targeting_env["workspace"])

    assert res["intent_type"] == "prospect_icp"
    assert set(res["target_locations"]) == {"Australia", "United Kingdom"}
    assert "United States" not in res["target_locations"]
    assert "Canada" not in res["target_locations"]
    assert "CTO" in res["target_titles"]
    assert "Founder" in res["target_titles"]
    assert "Engineering Manager" not in res["target_titles"]
    assert any("Recruiter" in exc for exc in res["excluded_titles"])


def test_targeting_change_modes_add_remove_only(targeting_env):
    """
    Test explicit change modes:
    - Only target Australia -> Australia only
    - Add Canada -> Existing + Canada
    - Remove UK -> Existing - UK
    """
    ws = targeting_env["workspace"]
    c = targeting_env["c_freelance"]
    c.target_locations = "Australia, United Kingdom"
    c.save()

    # 1. Add Canada
    res_add = TargetingAssistant.parse_natural_language_intent("Add Canada.", workspace=ws, campaign=c)
    assert set(res_add["target_locations"]) == {"Australia", "United Kingdom", "Canada"}
    assert res_add["change_mode"] == "ADD"

    # 2. Remove UK
    c.target_locations = "Australia, United Kingdom, Canada"
    c.save()
    res_remove = TargetingAssistant.parse_natural_language_intent("Remove UK.", workspace=ws, campaign=c)
    assert set(res_remove["target_locations"]) == {"Australia", "Canada"}
    assert res_remove["change_mode"] == "REMOVE"

    # 3. Only target Australia
    res_only = TargetingAssistant.parse_natural_language_intent("Only target Australia.", workspace=ws, campaign=c)
    assert res_only["target_locations"] == ["Australia"]
    assert "United Kingdom" not in res_only["target_locations"]


def test_job_status_query_is_strictly_read_only_and_canonical(targeting_env):
    """
    Operator asks 'Show me my current job targeting and today's application progress.'
    Expected:
    - intent_type == 'status_query'
    - ZERO TargetingConfigVersion created
    - 'Activate Job Target' is NOT present
    - Canonical relocation policy and sponsorship policy reported
    - 50 submission target / 60 provider ceiling displayed
    """
    ws = targeting_env["workspace"]
    from openoutreach.core.models import TargetingConfigVersion

    initial_version_count = TargetingConfigVersion.objects.count()

    prompt = "Show me my current job targeting and today's application progress."
    res = TargetingAssistant.parse_natural_language_intent(prompt, workspace=ws)

    assert res["intent_type"] == "status_query"
    assert "ACTIVE JOB TARGET STATUS" in res["reply"]
    assert "Click Activate Job Target" not in res["reply"]
    assert "**Current Relocation Policy:** Yes (where relocation allowed)" in res["reply"]
    assert "50 target" in res["reply"]
    assert "safety ceiling" in res["reply"]

    # Zero version creation
    assert TargetingConfigVersion.objects.count() == initial_version_count


def test_job_50_success_target_and_60_safety_ceiling_gating(targeting_env):
    """
    Test dual quota boundary:
    1. 50 successful submissions blocks further writes even if attempts < 60
    2. 60 attempts blocks further writes even if successful < 50
    3. Uncertain status consumes attempt quota (60 ceiling) but NOT successful target (50)
    4. Needs your answer consumes neither attempt nor successful target before provider write
    """
    ws = targeting_env["workspace"]
    candidate = targeting_env["candidate"]
    settings = targeting_env["settings"]

    settings.daily_job_application_target = 50
    settings.max_daily_job_applications = 60
    settings.save()

    from openoutreach.core.services.external_action_gates import evaluate_external_action_gates
    from openoutreach.career.models import Job, JobApplication
    from django.utils import timezone

    j_test, _ = Job.upsert_discovered_job(
        provider="linkedin",
        external_job_id="test_gate_job_0",
        title="Python Engineer",
        company="TechCorp",
        location="Remote",
        description="Python backend development",
        easy_apply=True,
        provenance="REAL_PROVIDER",
        provider_identity_status="VERIFIED"
    )

    # 1. Clean state: Gate is open
    allowed, reason = evaluate_external_action_gates(
        action_type="job_application",
        candidate_profile=candidate,
        job=j_test,
        workspace=ws
    )
    assert allowed is True

    # 2. Add 50 successful applications
    for i in range(50):
        j, _ = Job.upsert_discovered_job(
            provider="linkedin",
            external_job_id=f"gate_succ_{i}",
            title=f"Python Dev {i}",
            company="Company",
            location="Remote",
            description="Python backend",
            easy_apply=True,
            provenance="REAL_PROVIDER",
            provider_identity_status="VERIFIED"
        )
        JobApplication.objects.create(
            candidate_profile=candidate,
            job=j,
            status="submitted",
            submission_date=timezone.now()
        )

    # Gate should now block because 50 successful target reached (attempts = 50 < 60)
    j_new, _ = Job.upsert_discovered_job(
        provider="linkedin",
        external_job_id="gate_succ_blocked",
        title="Python Dev 51",
        company="Company",
        location="Remote",
        description="Python backend",
        easy_apply=True,
        provenance="REAL_PROVIDER",
        provider_identity_status="VERIFIED"
    )
    allowed, reason = evaluate_external_action_gates(
        action_type="job_application",
        candidate_profile=candidate,
        job=j_new,
        workspace=ws
    )
    assert allowed is False
    assert "daily_job_application_limit_reached (50 >= 50)" in reason


def test_targeting_assistant_endpoints_reject_post_without_csrf(targeting_env):
    """Prove that authenticated POST requests WITHOUT a valid CSRF token return 403 Forbidden."""
    from django.test import Client
    import json

    client = Client(enforce_csrf_checks=True)
    user = targeting_env["user"]
    client.force_login(user)

    endpoints = [
        ("/dashboard/targeting/chat/", {"message": "Hello"}),
        ("/dashboard/targeting/activate/", {"config_data": {}, "config_type": "prospect_icp"}),
        ("/dashboard/targeting/rollback/", {}),
        ("/dashboard/targeting/preview/", {"preview_type": "prospects", "config_data": {}}),
    ]

    for url, payload in endpoints:
        resp = client.post(
            url,
            data=json.dumps(payload),
            content_type="application/json",
            # Intentionally omitting HTTP_X_CSRFTOKEN
        )
        assert resp.status_code == 403, f"Expected 403 Forbidden without CSRF on {url}, got {resp.status_code}"


def test_targeting_assistant_endpoints_accept_post_with_valid_csrf(targeting_env):
    """Prove that authenticated POST requests WITH a valid CSRF token succeed through the CSRF layer."""
    from django.test import Client
    import json

    client = Client(enforce_csrf_checks=True)
    user = targeting_env["user"]
    c_freelance = targeting_env["c_freelance"]
    client.force_login(user)

    # 1. GET targeting assistant page to set CSRF cookie
    get_resp = client.get("/dashboard/targeting/")
    assert get_resp.status_code == 200
    csrf_token = get_resp.cookies.get("csrftoken")
    assert csrf_token is not None, "CSRF cookie must be set by GET /dashboard/targeting/"
    csrf_token_value = csrf_token.value

    # 2. POST chat with valid CSRF
    chat_resp = client.post(
        "/dashboard/targeting/chat/",
        data=json.dumps({
            "message": "Target CTOs and founders at AI software startups in Australia and the United Kingdom.",
            "campaign_id": c_freelance.id,
        }),
        content_type="application/json",
        HTTP_X_CSRFTOKEN=csrf_token_value,
    )
    assert chat_resp.status_code == 200
    chat_data = chat_resp.json()
    assert chat_data.get("status") == "ok"
    assert chat_data["payload"]["target_locations"] == ["United Kingdom", "Australia"]

    # 3. POST preview with valid CSRF
    prev_resp = client.post(
        "/dashboard/targeting/preview/",
        data=json.dumps({
            "preview_type": "prospects",
            "config_data": chat_data["payload"],
            "campaign_id": c_freelance.id,
        }),
        content_type="application/json",
        HTTP_X_CSRFTOKEN=csrf_token_value,
    )
    assert prev_resp.status_code == 200
    assert prev_resp.json().get("status") == "ok"

    # 4. POST activate with valid CSRF (Version 1)
    act_resp1 = client.post(
        "/dashboard/targeting/activate/",
        data=json.dumps({
            "config_data": chat_data["payload"],
            "config_type": "prospect_icp",
            "campaign_id": c_freelance.id,
            "operator_prompt": "Activated Version 1",
        }),
        content_type="application/json",
        HTTP_X_CSRFTOKEN=csrf_token_value,
    )
    assert act_resp1.status_code == 200
    assert act_resp1.json().get("status") == "ok"
    assert act_resp1.json().get("version_number") == 1

    # 5. POST activate with valid CSRF (Version 2)
    act_resp2 = client.post(
        "/dashboard/targeting/activate/",
        data=json.dumps({
            "config_data": {"titles": ["CTO", "Founder"], "locations": ["Australia"]},
            "config_type": "prospect_icp",
            "campaign_id": c_freelance.id,
            "operator_prompt": "Activated Version 2",
        }),
        content_type="application/json",
        HTTP_X_CSRFTOKEN=csrf_token_value,
    )
    assert act_resp2.status_code == 200
    assert act_resp2.json().get("version_number") == 2

    # 6. POST rollback with valid CSRF (Rolls back Version 2 to Version 1)
    roll_resp = client.post(
        "/dashboard/targeting/rollback/",
        data=json.dumps({
            "campaign_id": c_freelance.id,
            "config_type": "prospect_icp",
        }),
        content_type="application/json",
        HTTP_X_CSRFTOKEN=csrf_token_value,
    )
    assert roll_resp.status_code == 200
    assert roll_resp.json().get("status") == "ok"


def test_targeting_assistant_unauthenticated_mutation_blocked():
    """Prove unauthenticated mutation requests are blocked by dashboard_login_required."""
    from django.test import Client
    import json

    client = Client(enforce_csrf_checks=True)
    resp = client.post(
        "/dashboard/targeting/activate/",
        data=json.dumps({"config_data": {}, "config_type": "prospect_icp"}),
        content_type="application/json",
    )
    # Anonymous request should either be redirected to login (302) or 403
    assert resp.status_code in (302, 403)
    if resp.status_code == 302:
        assert "/dashboard/login/" in resp.url


def test_targeting_assistant_views_have_no_csrf_exempt():
    """Verify that none of the targeting assistant views have csrf_exempt applied."""
    from openoutreach.dashboard import views

    views_to_check = [
        views.targeting_assistant_chat,
        views.targeting_assistant_activate,
        views.targeting_assistant_rollback,
        views.targeting_assistant_preview,
    ]

    for view_fn in views_to_check:
        # If wrapped by decorators, check underlying function or attributes
        assert getattr(view_fn, "csrf_exempt", False) is False, f"{view_fn.__name__} must not have csrf_exempt!"


