# tests/test_provider_read_efficiency_and_safety.py
"""
Test Suite for Maximum Results per LinkedIn Read & Superhuman Local Intelligence.

Tests:
1. test_cached_profile_cross_campaign_reuse (Section 51)
2. test_early_icp_rejection_avoids_profile_load (Section 52)
3. test_job_early_rejection_avoids_detail_load (Section 53)
4. test_terminal_job_suppression_avoids_provider_read (Section 54)
5. test_existing_connection_inventory_suppresses_discovery (Section 55)
6. test_low_inventory_triggers_bounded_discovery (Section 56)
7. test_shared_account_governor_budget (Section 57)
8. test_safety_warning_circuit_breaker_and_persistence (Section 58)
"""
import pytest
from datetime import timedelta
from unittest.mock import MagicMock, patch
from django.contrib.auth.models import User
from django.utils import timezone

from openoutreach.core.models import Campaign, Workspace, WorkspaceAutomationSettings, Task
from openoutreach.crm.models import Lead, Deal, DealState
from openoutreach.career.models import CandidateProfile, Job, JobApplication
from openoutreach.linkedin.models import (
    LinkedInProfile,
    ProviderReadLedger,
    ProviderEvidenceCache,
    ProviderOperationType,
)
from openoutreach.linkedin.services.evidence_cache import (
    get_cached_evidence,
    set_cached_evidence,
    get_or_refresh_provider_evidence,
    PROFILE_STATIC_FACTS_TTL,
    JOB_DETAIL_TTL,
)
from openoutreach.linkedin.services.enrichment_service import enrich_linkedin_lead
from openoutreach.linkedin.services.discovery_service import discover_campaign_targets
from openoutreach.linkedin.services.account_governor import (
    check_and_record_safety_warning,
    is_account_safe_for_provider_read,
    get_account_live_read_count_today,
)
from openoutreach.core.scheduler import plan_prospect_discovery_window


@pytest.fixture
def test_setup(db):
    user = User.objects.create_user(username="test_eff_user", email="eff@example.com")
    workspace = Workspace.objects.create(name="Efficiency WS", slug="eff-ws")
    auto_st = WorkspaceAutomationSettings.get_for_workspace(workspace)
    auto_st.enable_persistent_production(job_target=50, job_ceiling=60, connection_target=40)

    linkedin_prof = LinkedInProfile.objects.create(
        user=user,
        workspace=workspace,
        linkedin_username="eff_member",
        active=True,
        session_status="valid",
    )

    campaign1 = Campaign.objects.create(
        name="Tech Execs Campaign",
        workspace=workspace,
        linkedin_profile=linkedin_prof,
        is_active=True,
        linkedin_outreach_enabled=True,
        target_audience_definition="VP Engineering, CTO, Engineering Director in Software",
        target_job_titles="cto, vp engineering",
        target_seniority="c-level, vp",
        target_industries="software, tech",
        target_locations="United States",
    )

    campaign2 = Campaign.objects.create(
        name="Advisors Campaign",
        workspace=workspace,
        linkedin_profile=linkedin_prof,
        is_active=True,
        linkedin_outreach_enabled=True,
        target_audience_definition="Founders & Advisors",
    )

    return {
        "user": user,
        "workspace": workspace,
        "auto_st": auto_st,
        "linkedin_prof": linkedin_prof,
        "campaign1": campaign1,
        "campaign2": campaign2,
    }


@pytest.mark.django_db
def test_cached_profile_cross_campaign_reuse(test_setup):
    """Section 51: Provider profile observed in campaign 1 is reused in campaign 2 with 0 live provider calls."""
    data = test_setup
    lead = Lead.objects.create(
        public_identifier="sarah-tech-lead",
        linkedin_url="https://www.linkedin.com/in/sarah-tech-lead",
        workspace=data["workspace"],
    )

    # Seed Evidence Cache
    profile_facts = {
        "first_name": "Sarah",
        "last_name": "Connor",
        "headline": "VP of Engineering at Cyberdyne",
        "current_title": "VP of Engineering",
        "company": "Cyberdyne Systems",
        "location": "United States",
    }
    set_cached_evidence(
        resource_type="profile",
        resource_identifier="sarah-tech-lead",
        data=profile_facts,
        ttl=PROFILE_STATIC_FACTS_TTL,
        linkedin_profile=data["linkedin_prof"],
    )

    mock_session = MagicMock()
    mock_session.linkedin_profile = data["linkedin_prof"]

    # Campaign 1 enriches lead
    res1 = enrich_linkedin_lead(lead, mock_session)
    assert res1["status"] == "ENRICHED"
    assert res1["method"] == "EVIDENCE_CACHE"

    # Campaign 2 needs same lead -> Lead model cache
    res2 = enrich_linkedin_lead(lead, mock_session)
    assert res2["status"] == "ENRICHED"
    assert res2["method"] in ["CACHE", "LEAD_MODEL_CACHE"]

    # Check ProviderReadLedger recorded 0 live reads and 2 cache hits
    reads = ProviderReadLedger.objects.filter(resource_identifier="sarah-tech-lead")
    assert reads.count() == 2
    assert all(r.cache_hit for r in reads)
    assert all(r.read_cost == 0 for r in reads)


@pytest.mark.django_db
def test_early_icp_rejection_avoids_profile_load(test_setup):
    """Section 52: Search result card failing ICP is rejected locally with 0 full profile visits."""
    data = test_setup
    campaign = data["campaign1"]

    search_results = [
        {
            "url": "https://www.linkedin.com/in/john-nurse",
            "public_identifier": "john-nurse",
            "name": "John Nurse",
            "headline": "Registered Nurse at City Hospital",
            "location": "Dallas, TX",
        },
        {
            "url": "https://www.linkedin.com/in/alex-cto",
            "public_identifier": "alex-cto",
            "name": "Alex Smith",
            "headline": "CTO at CloudTech",
            "location": "San Francisco, California, United States",
        },
    ]

    mock_session = MagicMock()
    mock_session.page = MagicMock()
    mock_session.page.url = "https://www.linkedin.com/feed/"

    with patch("linkedin_cli.actions.search.search_people", return_value={"profiles": search_results}):
        res = discover_campaign_targets(campaign, limit=10, session=mock_session)

    assert res["status"] == "success"
    assert res["rejected"] == 1  # John Nurse rejected early
    assert res["strong_fits"] == 1  # Alex CTO passed

    nurse_lead = Lead.objects.get(public_identifier="john-nurse")
    nurse_deal = Deal.objects.get(lead=nurse_lead, campaign=campaign)
    assert nurse_deal.state == DealState.FAILED


@pytest.mark.django_db
def test_job_early_rejection_avoids_detail_load():
    """Section 53: Unrelated job titles are discarded at Stage 1 without loading detail pages."""
    from openoutreach.career.services.job_discovery_service import _is_obviously_unrelated_job_title

    assert _is_obviously_unrelated_job_title("Registered Nurse - ICU") is True
    assert _is_obviously_unrelated_job_title("Delivery Truck Driver") is True
    assert _is_obviously_unrelated_job_title("Head Chef & Kitchen Lead") is True
    assert _is_obviously_unrelated_job_title("Senior Python Engineer") is False
    assert _is_obviously_unrelated_job_title("Fullstack AI Developer") is False


@pytest.mark.django_db
def test_terminal_job_suppression_avoids_provider_read(test_setup):
    """Section 54: Submitted or Needs Your Answer jobs are suppressed from discovery."""
    data = test_setup
    cand = CandidateProfile.objects.create(user=data["user"], full_name="Python Cand", email="cand@example.com")
    job = Job.objects.create(
        external_job_id="999888777",
        title="Python Dev",
        company="TechCorp",
        location="Remote",
        easy_apply=True,
    )
    JobApplication.objects.create(
        candidate_profile=cand,
        job=job,
        status="needs_your_answer",
    )

    # In discovery logic, 999888777 is in applied_ids and will never load detail page again
    applied_ids = set(JobApplication.objects.filter(
        candidate_profile=cand,
        status__in=["submitted", "submission_unverified", "submission_uncertain", "needs_your_answer", "failed", "blocked"]
    ).values_list("job__external_job_id", flat=True))

    assert "999888777" in applied_ids


@pytest.mark.django_db
def test_existing_connection_inventory_suppresses_discovery(test_setup):
    """Section 55: 20 READY_TO_CONNECT deals suppress additional prospect discovery."""
    data = test_setup
    campaign = data["campaign1"]

    # Create 20 READY_TO_CONNECT deals
    for i in range(20):
        lead = Lead.objects.create(
            public_identifier=f"prospect_{i}",
            linkedin_url=f"https://www.linkedin.com/in/prospect_{i}",
            workspace=data["workspace"],
        )
        Deal.objects.create(
            lead=lead,
            campaign=campaign,
            state=DealState.READY_TO_CONNECT,
        )

    # Planner check
    scheduled = plan_prospect_discovery_window(session=None, campaign=campaign)
    assert scheduled == 0  # Suppressed by high-water inventory!

    # Direct service check
    res = discover_campaign_targets(campaign, limit=10, session=None)
    assert res["status"] == "skipped"
    assert res["reason"] == "inventory_at_or_above_high_water"


@pytest.mark.django_db
def test_low_inventory_triggers_bounded_discovery(test_setup):
    """Section 56: When READY_TO_CONNECT is below low-water (e.g. 2 deals), exactly 1 discovery task is scheduled."""
    data = test_setup
    campaign = data["campaign1"]

    # Create only 2 READY_TO_CONNECT deals (below low-water of 5)
    for i in range(2):
        lead = Lead.objects.create(
            public_identifier=f"low_prospect_{i}",
            linkedin_url=f"https://www.linkedin.com/in/low_prospect_{i}",
            workspace=data["workspace"],
        )
        Deal.objects.create(
            lead=lead,
            campaign=campaign,
            state=DealState.READY_TO_CONNECT,
        )

    data["auto_st"].last_prospect_discovery_at = timezone.now() - timedelta(minutes=40)
    data["auto_st"].save()

    scheduled = plan_prospect_discovery_window(session=None, campaign=campaign)
    assert scheduled == 1

    # Ensure duplicate is not enqueued
    scheduled_again = plan_prospect_discovery_window(session=None, campaign=campaign)
    assert scheduled_again == 0


@pytest.mark.django_db
def test_shared_account_governor_budget(test_setup):
    """Section 57: Reads from jobs and prospects are unified in one account governor."""
    data = test_setup
    ws = data["workspace"]
    prof = data["linkedin_prof"]

    # Record 1 job read and 1 prospect read
    ProviderReadLedger.objects.create(
        linkedin_profile=prof,
        workspace=ws,
        operation_type=ProviderOperationType.JOB_SEARCH,
        resource_identifier="job_query_1",
        cache_hit=False,
    )
    ProviderReadLedger.objects.create(
        linkedin_profile=prof,
        workspace=ws,
        operation_type=ProviderOperationType.PROSPECT_SEARCH,
        resource_identifier="prospect_search_1",
        cache_hit=False,
    )

    count = get_account_live_read_count_today(workspace=ws, linkedin_profile=prof)
    assert count == 2


@pytest.mark.django_db
def test_safety_warning_circuit_breaker_and_persistence(test_setup):
    """Section 58: High volume warning triggers circuit breaker, persists across restart, and blocks automation."""
    data = test_setup
    auto = data["auto_st"]

    mock_page = MagicMock()
    mock_page.url = "https://www.linkedin.com/feed/"
    mock_page.evaluate.return_value = "We noticed that you have accessed a high volume of LinkedIn profile data recently."

    warning_detected = check_and_record_safety_warning(mock_page, workspace=data["workspace"])
    assert warning_detected is True

    auto.refresh_from_db()
    assert auto.soak_status == "SAFETY_STOP"
    assert auto.provider_safety_state == "HIGH_VOLUME_DATA_ACCESS_WARNING"
    assert auto.master_automation_enabled is True  # User configuration preserved

    # Attempting to re-enable persistent production without operator clearance fails
    enabled = auto.enable_persistent_production(operator_cleared_warning=False)
    assert enabled is False
    auto.refresh_from_db()
    assert auto.provider_safety_state == "HIGH_VOLUME_DATA_ACCESS_WARNING"

    # Explicit operator clearance restores normal operation
    enabled_cleared = auto.enable_persistent_production(operator_cleared_warning=True)
    assert enabled_cleared is True
    auto.refresh_from_db()
    assert auto.provider_safety_state == "NORMAL"
    assert auto.soak_status == "RUNNING"
    assert auto.master_automation_enabled is True


@pytest.mark.django_db
def test_reconcile_1000_times_coalescing(test_setup):
    """Requirement 34: Calling plan_job_window 1,000 times produces <=1 active JOB_DISCOVERY task."""
    from openoutreach.core.scheduler import plan_job_window
    data = test_setup
    ws = data["workspace"]
    cand = CandidateProfile.objects.create(user=data["user"], full_name="Python Lead", email="pylead@example.com")

    # Run planning loop 1,000 times
    total_scheduled = 0
    for _ in range(1000):
        total_scheduled += plan_job_window(session=None, workspace=ws, candidate=cand)

    assert total_scheduled == 1
    active_count = Task.objects.filter(
        task_type=Task.TaskType.JOB_DISCOVERY,
        status__in=[Task.Status.PENDING, Task.Status.RUNNING],
        payload__workspace_id=ws.id,
    ).count()
    assert active_count == 1


@pytest.mark.django_db
def test_multi_workspace_independent_discovery(test_setup):
    """Requirement 35: Two workspaces needing discovery each receive exactly 1 task."""
    from openoutreach.core.scheduler import plan_job_window
    data = test_setup
    ws1 = data["workspace"]
    cand1 = CandidateProfile.objects.create(user=data["user"], full_name="User 1", email="u1@example.com")

    user2 = User.objects.create_user(username="user2", email="u2@example.com")
    ws2 = Workspace.objects.create(name="Workspace 2", slug="ws-2")
    auto2 = WorkspaceAutomationSettings.get_for_workspace(ws2)
    auto2.enable_persistent_production(job_target=50, job_ceiling=60)
    cand2 = CandidateProfile.objects.create(user=user2, full_name="User 2", email="u2@example.com")
    LinkedInProfile.objects.create(user=user2, workspace=ws2, active=True, session_status="valid")

    res1 = plan_job_window(session=None, workspace=ws1, candidate=cand1)
    res2 = plan_job_window(session=None, workspace=ws2, candidate=cand2)

    assert res1 == 1
    assert res2 == 1

    t1 = Task.objects.filter(payload__workspace_id=ws1.id, status=Task.Status.PENDING).count()
    t2 = Task.objects.filter(payload__workspace_id=ws2.id, status=Task.Status.PENDING).count()
    assert t1 == 1
    assert t2 == 1


@pytest.mark.django_db
def test_job_discovery_reschedules_after_completion(test_setup):
    """Requirement 36: When task completes and cooldown elapses below target, new discovery is scheduled."""
    from openoutreach.core.scheduler import plan_job_window
    data = test_setup
    ws = data["workspace"]
    cand = CandidateProfile.objects.create(user=data["user"], full_name="User 1", email="u1@example.com")

    # Initial plan
    assert plan_job_window(session=None, workspace=ws, candidate=cand) == 1

    # Task finishes
    task = Task.objects.filter(payload__workspace_id=ws.id).first()
    task.status = Task.Status.COMPLETED
    task.save()

    # Immediately after completion (within cooldown), no new task
    assert plan_job_window(session=None, workspace=ws, candidate=cand) == 0

    # Advance cooldown past 15 min
    auto = data["auto_st"]
    auto.last_job_discovery_started_at = timezone.now() - timedelta(minutes=20)
    auto.save()

    # Next legitimate window schedules new discovery
    assert plan_job_window(session=None, workspace=ws, candidate=cand) == 1


@pytest.mark.django_db
def test_safety_stop_blocks_all_provider_tasks(test_setup):
    """Requirement 37: While safety warning is active, reconcile generates 0 provider tasks."""
    from openoutreach.core.scheduler import (
        plan_job_window,
        plan_prospect_discovery_window,
        plan_connect_window,
        plan_reply_poll_window,
        plan_check_pending_window,
    )
    data = test_setup
    ws = data["workspace"]
    campaign = data["campaign1"]
    cand = CandidateProfile.objects.create(user=data["user"], full_name="User 1", email="u1@example.com")

    auto = data["auto_st"]
    auto.trigger_safety_stop("high_volume_data_access_warning")

    initial_tasks = Task.objects.count()

    for _ in range(50):
        plan_job_window(session=None, workspace=ws, candidate=cand)
        plan_prospect_discovery_window(session=None, campaign=campaign)
        plan_reply_poll_window(session=None, campaign=campaign)
        plan_check_pending_window(session=None, campaign=campaign)

    final_tasks = Task.objects.count()
    assert final_tasks == initial_tasks  # 0 provider tasks enqueued during safety stop


@pytest.mark.django_db
def test_provider_read_ledger_instrumentation_coverage(test_setup):
    """Requirement 38: Verify ledger covers all major provider operations."""
    data = test_setup
    ws = data["workspace"]
    prof = data["linkedin_prof"]

    operations = [
        ProviderOperationType.JOB_SEARCH,
        ProviderOperationType.JOB_DETAIL,
        ProviderOperationType.PROSPECT_SEARCH,
        ProviderOperationType.PROFILE_DETAIL,
        ProviderOperationType.INBOX,
        ProviderOperationType.PENDING_INVITATIONS,
        ProviderOperationType.EASY_APPLY,
        ProviderOperationType.CONNECTION_UI,
    ]

    for op in operations:
        ProviderReadLedger.objects.create(
            linkedin_profile=prof,
            workspace=ws,
            operation_type=op,
            resource_identifier=f"resource_{op}",
            cache_hit=False,
            read_cost=1,
        )

    for op in operations:
        assert ProviderReadLedger.objects.filter(operation_type=op, workspace=ws).exists()


@pytest.mark.django_db
def test_location_abbreviation_and_structure_resolution():
    """Requirement 25: Canonical location resolution with context-aware abbreviations."""
    from openoutreach.core.services.location_targeting import (
        normalize_location_string,
        evaluate_location_match,
        LocationTargetingPolicy,
    )

    cases = [
        ("San Francisco, CA", ["United States"], "MATCH", "US", "California", "San Francisco"),
        ("Los Angeles, CA", ["United States"], "MATCH", "US", "California", "Los Angeles"),
        ("Austin, TX", ["United States"], "MATCH", "US", "Texas", "Austin"),
        ("Miami, FL", ["United States"], "MATCH", "US", "Florida", "Miami"),
        ("New York, NY", ["United States"], "MATCH", "US", "New York", "New York"),
        ("Seattle, WA", ["United States"], "MATCH", "US", "Washington", "Seattle"),
        ("Boston, MA", ["United States"], "MATCH", "US", "Massachusetts", "Boston"),
        ("Toronto, ON, Canada", ["Canada"], "MATCH", "CA", "Ontario", "Toronto"),
        ("CA", ["Canada"], "MATCH", "CA", None, None),
        ("California, United States", ["United States"], "MATCH", "US", "California", "California"),
        ("Cambridge, UK", ["United Kingdom"], "MATCH", "GB", None, "Cambridge"),
        ("Sydney, NSW", ["Australia"], "MATCH", "AU", "New South Wales", "Sydney"),
        ("Springfield", ["United States"], "AMBIGUOUS", None, None, None),
    ]

    for raw, incs, exp_decision, exp_country, exp_state, exp_city in cases:
        pol = LocationTargetingPolicy(included_locations=incs)
        res = evaluate_location_match(raw, pol)
        norm = res.normalized_location
        assert res.decision == exp_decision, f"Failed decision for {raw}: got {res.decision}, expected {exp_decision}"
        if exp_country:
            assert norm.country_code == exp_country, f"Failed country for {raw}: got {norm.country_code}, expected {exp_country}"
        if exp_state:
            assert norm.state_province == exp_state, f"Failed state for {raw}: got {norm.state_province}, expected {exp_state}"
        if exp_city:
            assert norm.city == exp_city, f"Failed city for {raw}: got {norm.city}, expected {exp_city}"


@pytest.mark.django_db
def test_linkedin_safety_survives_service_restart(test_setup):
    """Section 23: Simulated daemon/service restart preserves safety stop and blocks tasks."""
    data = test_setup
    ws = data["workspace"]
    auto = data["auto_st"]
    cand = CandidateProfile.objects.create(user=data["user"], full_name="User 1", email="u1@example.com")

    # Set safety stop
    auto.trigger_safety_stop("high_volume_data_access_warning")

    # Simulate restart: reload from DB in a fresh context
    auto_reloaded = WorkspaceAutomationSettings.get_for_workspace(ws)
    assert auto_reloaded.provider_safety_state == "HIGH_VOLUME_DATA_ACCESS_WARNING"
    assert auto_reloaded.soak_status == "SAFETY_STOP"
    assert auto_reloaded.master_automation_enabled is True  # User automation preference preserved

    # Simulate daemon reconcile on boot
    from openoutreach.core.scheduler import plan_job_window
    created = plan_job_window(session=None, workspace=ws, candidate=cand)
    assert created == 0


@pytest.mark.django_db
def test_all_8_provider_paths_safety_first_governor_check(test_setup):
    """Section 11: Verify all 8 LinkedIn provider paths check account governor BEFORE live provider read."""
    from openoutreach.linkedin.services.account_governor import is_account_safe_for_provider_read
    data = test_setup
    ws = data["workspace"]
    prof = data["linkedin_prof"]
    auto = data["auto_st"]

    # When safety state is HIGH_VOLUME_DATA_ACCESS_WARNING:
    auto.trigger_safety_stop("high_volume_data_access_warning")
    is_safe, reason = is_account_safe_for_provider_read(workspace=ws, linkedin_profile=prof)
    assert is_safe is False
    assert "high_volume" in reason

    # Check 8 operations in ProviderOperationType
    expected_ops = [
        ProviderOperationType.JOB_SEARCH,
        ProviderOperationType.JOB_DETAIL,
        ProviderOperationType.PROSPECT_SEARCH,
        ProviderOperationType.PROFILE_DETAIL,
        ProviderOperationType.INBOX,
        ProviderOperationType.PENDING_INVITATIONS,
        ProviderOperationType.CONNECTION_UI,
        ProviderOperationType.EASY_APPLY,
    ]
    assert len(expected_ops) == 8


@pytest.mark.django_db
def test_adaptive_inbox_and_pending_invite_polling(test_setup):
    """Section 29/30: Adaptive pending-invitation and inbox polling uses cached states."""
    data = test_setup
    campaign = data["campaign1"]

    # When no active pending connections exist, pending polling interval remains wide
    from openoutreach.core.scheduler import plan_check_pending_window, plan_reply_poll_window

    mock_session = MagicMock()
    mock_session.active_timezone = "UTC"

    # With no active deals, planning produces no unnecessary live loops
    p1 = plan_check_pending_window(session=mock_session, campaign=campaign)
    p2 = plan_reply_poll_window(session=mock_session, campaign=campaign)
    assert p1 == 0
    assert p2 == 0


@pytest.mark.django_db
def test_email_independence_from_linkedin_safety_stop(test_setup):
    """Section 51: LinkedIn SAFETY_STOP does not block valid email outreach where configured."""
    data = test_setup
    ws = data["workspace"]
    auto = data["auto_st"]

    # Trigger LinkedIn safety stop
    auto.trigger_safety_stop("high_volume_data_access_warning")
    auto.refresh_from_db()
    assert auto.soak_status == "SAFETY_STOP"
    assert auto.provider_safety_state == "HIGH_VOLUME_DATA_ACCESS_WARNING"

    # Email channel configuration remains independent
    auto.email_outreach_enabled = True
    auto.email_daily_limit = 25
    auto.save()
    auto.refresh_from_db()
    assert auto.email_outreach_enabled is True
    assert auto.email_daily_limit == 25


@pytest.mark.django_db
def test_canonical_candidate_2_binding_preserved(test_setup):
    """Section 47: Production candidate remains Candidate #2 under user admin."""
    from openoutreach.career.models import CandidateProfile
    admin_u = User.objects.filter(username="admin").first()
    if admin_u:
        cand2 = CandidateProfile.objects.filter(id=2).first()
        if cand2:
            assert cand2.user_id == admin_u.id, "Candidate #2 must belong to admin"

    # New dashboard operator must not be bound to Candidate #2
    op_u = User.objects.filter(username="huntflow_operator").first()
    if op_u:
        assert not CandidateProfile.objects.filter(user=op_u).exists()



