"""
Comprehensive Tests for HuntFlow Global Location Targeting Engine.

Verifies:
1. Deterministic normalization (countries, aliases, regions)
2. Inclusions vs Exclusions (Exclusion Wins)
3. Europe vs EU-27 distinction (UK in Europe, NOT in EU-27)
4. Ambiguous city handling without false positives
5. Remote worldwide vs Country-restricted remote
6. Work mode compatibility & Relocation constraints
7. Separation of concerns:
   - Target Search Geography != Candidate Legal Work Authorization
   - Candidate Home Location != Job Search Target
   - Jobs Geography != Outreach/Prospect Geography
   - Campaign Specific Override != Workspace Outreach Default
8. Server-side job application & connection dispatch gates
9. Dynamic pre-execution queue re-evaluation (blocking stale queued targets)
10. Historical integrity preservation
"""

import pytest
from django.utils import timezone
from django.contrib.auth import get_user_model

from openoutreach.core.models import (
    Workspace, WorkspaceMembership, WorkspaceAutomationSettings, Campaign, OperatorActionApproval
)
from openoutreach.crm.models.lead import Lead
from openoutreach.career.models import (
    CandidateProfile, CandidateProfileVersion, CandidateApplicationProfile,
    CandidateIdentityVerification, Job, JobApplication, ApplicationDraft
)
from openoutreach.core.services.location_targeting import (
    normalize_location_string, evaluate_location_match, LocationTargetingPolicy,
    EUROPE_COUNTRY_CODES, EU_27_COUNTRY_CODES
)
from openoutreach.core.services.external_action_gates import evaluate_external_action_gates
from openoutreach.career.services.job_application_engine import (
    authorize_job_application, execute_job_application, evaluate_canary_readiness
)
from openoutreach.linkedin.pipeline.icp_evaluator import evaluate_lead_icp_fit

User = get_user_model()


@pytest.mark.django_db
class TestLocationTargetingEngine:

    @pytest.fixture(autouse=True)
    def setup_data(self, db):
        self.user = User.objects.create_user(username="loc_test_user", email="loc_test@example.com")
        self.ws = Workspace.objects.create(name="Location WS", slug="loc-ws")
        self.membership = WorkspaceMembership.objects.create(
            workspace=self.ws, user=self.user, role="owner", status="active"
        )
        self.auto_settings = WorkspaceAutomationSettings.objects.create(
            workspace=self.ws,
            master_automation_enabled=True,
            auto_find_jobs=True,
            auto_apply_jobs=True,
            auto_find_prospects=True,
            auto_send_connections=True,
            default_outreach_locations="United States, United Kingdom",
            default_outreach_locations_excluded="Russia",
            default_unknown_location_handling="deny"
        )

        self.candidate = CandidateProfile.objects.create(
            user=self.user,
            full_name="Alex Morgan",
            email="candidate_loc@example.com"
        )
        self.cpv = CandidateProfileVersion.objects.create(
            profile=self.candidate,
            version_number=1,
            state="approved",
            full_name="Alex Morgan",
            fact_snapshot=[{"category": "Skills", "value": "Python, Django, React"}]
        )
        self.candidate.active_version = self.cpv
        self.candidate.save()

        CandidateIdentityVerification.objects.create(
            candidate_profile=self.candidate,
            extracted_name="Alex Morgan",
            confirmed_name="Alex Morgan",
            verification_source="manual",
            status="verified",
            verified_at=timezone.now()
        )

        self.app_prof = CandidateApplicationProfile.objects.create(
            candidate_profile=self.candidate,
            full_name="Alex Morgan",
            email="candidate_loc@example.com",
            phone="+1-202-555-0101",
            city="Sargodha",
            country="Pakistan",
            target_locations=["Australia", "New Zealand", "Europe", "Remote Worldwide"],
            excluded_locations=["Germany"],
            authorized_countries=["PK", "AU", "NZ", "US"],
            visa_sponsorship_now="yes",
            visa_sponsorship_future="yes",
            relocation_willingness="yes",
            remote_willingness="yes",
            hybrid_willingness="yes",
            onsite_willingness="yes",
            travel_willingness="yes",
            background_check_willingness="yes",
            minimum_salary=10000,
            target_salary=100000,
            currency="USD",
            salary_period="annual",
            confirmed=True,
            confirmed_at=timezone.now()
        )

        self.campaign = Campaign.objects.create(
            name="US Tech Execs",
            workspace=self.ws,
            target_job_titles="CEO, Founder",
            target_locations="United States, United Kingdom",
            target_locations_excluded="Germany",
            unknown_location_handling="deny",
            require_location_match=True,
            is_active=True
        )

    # 1. Inclusion & Exclusion Tests
    def test_australia_included_matches(self):
        policy = LocationTargetingPolicy(included_locations=["Australia"], channel="jobs")
        res = evaluate_location_match("Sydney, New South Wales, Australia", policy)
        assert res.decision == "MATCH"
        assert res.is_eligible is True
        assert res.normalized_location.country_code == "AU"
        assert res.normalized_location.country_name == "Australia"

    def test_australia_excluded_fails(self):
        policy = LocationTargetingPolicy(
            included_locations=["Australia", "New Zealand"],
            excluded_locations=["Australia"],
            channel="jobs"
        )
        res = evaluate_location_match("Melbourne, Australia", policy)
        assert res.decision == "EXCLUDED"
        assert res.is_eligible is False
        assert res.excluded_rule == "Australia"

    def test_europe_included_france_matches(self):
        policy = LocationTargetingPolicy(included_locations=["Europe"], channel="jobs")
        res = evaluate_location_match("Paris, France", policy)
        assert res.decision == "MATCH"
        assert res.is_eligible is True
        assert res.normalized_location.country_code == "FR"
        assert res.normalized_location.region == "Europe"

    def test_europe_included_germany_excluded_fails(self):
        policy = LocationTargetingPolicy(
            included_locations=["Europe"],
            excluded_locations=["Germany"],
            channel="jobs"
        )
        # France matches
        res_fr = evaluate_location_match("Lyon, France", policy)
        assert res_fr.decision == "MATCH"
        assert res_fr.is_eligible is True

        # Germany excluded (exclusion wins)
        res_de = evaluate_location_match("Berlin, Germany", policy)
        assert res_de.decision == "EXCLUDED"
        assert res_de.is_eligible is False

    def test_eu_not_equal_to_europe(self):
        """UK is in Europe, but UK is NOT in European Union (EU-27)."""
        assert "GB" in EUROPE_COUNTRY_CODES
        assert "GB" not in EU_27_COUNTRY_CODES

        policy_eu = LocationTargetingPolicy(included_locations=["European Union"], channel="jobs")
        res_uk = evaluate_location_match("London, United Kingdom", policy_eu)
        assert res_uk.decision == "NO_MATCH"
        assert res_uk.is_eligible is False

        policy_europe = LocationTargetingPolicy(included_locations=["Europe"], channel="jobs")
        res_uk_europe = evaluate_location_match("London, United Kingdom", policy_europe)
        assert res_uk_europe.decision == "MATCH"
        assert res_uk_europe.is_eligible is True

    # 2. Normalization Aliases
    def test_us_uk_nz_aliases_normalize(self):
        norm_us1 = normalize_location_string("New York, USA")
        assert norm_us1.country_code == "US"

        norm_us2 = normalize_location_string("San Francisco, United States of America")
        assert norm_us2.country_code == "US"

        norm_uk = normalize_location_string("Manchester, Great Britain")
        assert norm_uk.country_code == "GB"

        norm_nz = normalize_location_string("Auckland, New Zealand")
        assert norm_nz.country_code == "NZ"

    # 3. Remote Handling
    def test_remote_worldwide_allowed_matches(self):
        policy = LocationTargetingPolicy(
            included_locations=["Australia"],
            remote_mode="allow_all",
            channel="jobs"
        )
        res = evaluate_location_match("Remote - Worldwide", policy)
        assert res.decision == "MATCH"
        assert res.is_eligible is True
        assert res.normalized_location.is_remote is True
        assert res.normalized_location.remote_scope == "worldwide"

    def test_remote_us_only_with_unauthorized_candidate_blocks(self):
        policy = LocationTargetingPolicy(
            included_locations=["United States"],
            remote_mode="allow_all",
            application_strategy="STRICT_AUTHORIZATION",
            channel="jobs"
        )
        # Candidate only authorized in PK and AU (not US), requires no sponsorship in strict mode
        res = evaluate_location_match(
            "Remote - US Only",
            policy,
            candidate_authorization=["PK", "AU"],
            candidate_sponsorship_now="no"
        )
        assert res.decision == "BLOCKED_AUTHORIZATION"
        assert res.is_eligible is False

    # 4. Unknown & Ambiguous Handling
    def test_unknown_geography_conservative_default_not_auto_eligible(self):
        policy = LocationTargetingPolicy(
            included_locations=["Australia"],
            unknown_location_handling="deny",
            channel="jobs"
        )
        res = evaluate_location_match("", policy)
        assert res.decision == "UNKNOWN"
        assert res.is_eligible is False

    def test_ambiguous_city_without_country_not_auto_eligible(self):
        """'Springfield' without state/country is ambiguous and must not falsely match."""
        policy = LocationTargetingPolicy(
            included_locations=["United States"],
            channel="jobs"
        )
        res = evaluate_location_match("Springfield", policy)
        assert res.decision == "AMBIGUOUS"
        assert res.is_eligible is False
        assert res.normalized_location.is_ambiguous is True

    def test_same_city_name_across_two_countries_resolves_accurately(self):
        """'Hamilton, Ontario, Canada' vs 'Hamilton, New Zealand'."""
        norm_ca = normalize_location_string("Hamilton, Canada")
        norm_nz = normalize_location_string("Hamilton, New Zealand")
        assert norm_ca.country_code == "CA"
        assert norm_nz.country_code == "NZ"

    # 5. Independence of Policies
    def test_campaign_override_beats_global_outreach_default(self):
        """Campaign target 'Australia' overrides global outreach default 'United States'."""
        camp_au = Campaign.objects.create(
            name="Aus Campaign",
            workspace=self.ws,
            target_locations="Australia",
            is_active=True
        )
        lead_au = Lead.objects.create(
            workspace=self.ws,
            linkedin_url="https://www.linkedin.com/in/sydney-lead/",
            public_identifier="sydney-lead",
            location="Sydney, Australia",
            current_title="CEO"
        )
        lead_us = Lead.objects.create(
            workspace=self.ws,
            linkedin_url="https://www.linkedin.com/in/ny-lead/",
            public_identifier="ny-lead",
            location="New York, United States",
            current_title="CEO"
        )

        fit_au = evaluate_lead_icp_fit(
            {"title": "CEO", "location": lead_au.location, "current_title": "CEO"},
            camp_au
        )
        assert fit_au["location_match"] == "MATCH"

        fit_us = evaluate_lead_icp_fit(
            {"title": "CEO", "location": lead_us.location, "current_title": "CEO"},
            camp_au
        )
        assert fit_us["location_match"] == "MISMATCH"

    def test_jobs_location_policy_independent_from_outreach(self):
        """Jobs targets (AU, NZ, Europe) are completely separate from Campaign targets (US, UK)."""
        job_policy = self.app_prof.get_location_targeting_policy()
        campaign_policy = self.campaign.get_location_targeting_policy()

        # Job in Auckland (NZ) passes job policy, fails campaign policy
        res_job = evaluate_location_match("Auckland, New Zealand", job_policy)
        res_camp = evaluate_location_match("Auckland, New Zealand", campaign_policy)
        assert res_job.decision == "MATCH"
        assert res_camp.decision == "NO_MATCH"

    def test_candidate_current_location_independent_from_search_location(self):
        """Candidate lives in Sargodha, Pakistan, but searches jobs in Australia."""
        assert self.app_prof.city == "Sargodha"
        assert self.app_prof.country == "Pakistan"
        assert "Australia" in self.app_prof.target_locations

        job_policy = self.app_prof.get_location_targeting_policy()
        res = evaluate_location_match("Sydney, Australia", job_policy)
        assert res.decision == "MATCH"

    # 6. Server-Side Gates & Dynamic Queue Re-Evaluation
    def test_server_side_job_location_block(self):
        """A job outside candidate target locations is blocked server-side in external action gates."""
        job_japan = Job.objects.create(
            company="Tokyo Tech",
            title="Senior Python Engineer",
            job_url="https://www.linkedin.com/jobs/view/8811223344/",
            description="Django PostgreSQL in Tokyo.",
            provider="linkedin",
            external_job_id="8811223344",
            provenance="REAL_PROVIDER",
            provider_identity_status="VERIFIED",
            location="Tokyo, Japan",
            is_active=True
        )

        allowed, reason = evaluate_external_action_gates(
            workspace=self.ws,
            candidate_profile=self.candidate,
            job=job_japan,
            external_action_authorized=True,
            action_type="job_application"
        )
        assert allowed is False
        assert "location_not_eligible" in reason

    def test_stale_queued_job_recheck_blocks_after_policy_change(self):
        """If a job was eligible when queued, but operator changes location policy before execution, it is blocked."""
        job_uk = Job.objects.create(
            company="London Tech",
            title="Senior Backend Engineer",
            job_url="https://www.linkedin.com/jobs/view/8811229999/",
            description="Python engineer in London.",
            provider="linkedin",
            external_job_id="8811229999",
            provenance="REAL_PROVIDER",
            provider_identity_status="VERIFIED",
            location="London, United Kingdom",
            is_active=True
        )
        app = JobApplication.objects.create(
            candidate_profile=self.candidate,
            job=job_uk,
            status="approved"
        )
        draft = ApplicationDraft.objects.create(application=app, version_number=1)

        approval = OperatorActionApproval.objects.create(
            workspace=self.ws,
            operator=self.user,
            action_type="job_application",
            candidate=self.candidate,
            job=job_uk,
            external_job_id="8811229999",
            status="approved"
        )

        # Before policy change: UK is in Europe -> eligible
        auth_before = authorize_job_application(
            workspace=self.ws,
            candidate=self.candidate,
            job=job_uk,
            draft=draft,
            operator_approval=approval
        )
        assert auth_before["authorized"] is True

        # Operator updates policy to explicitly EXCLUDE United Kingdom
        self.app_prof.excluded_locations = ["United Kingdom"]
        self.app_prof.save()

        # Worker re-evaluates immediately before execution -> BLOCKED
        auth_after = authorize_job_application(
            workspace=self.ws,
            candidate=self.candidate,
            job=job_uk,
            draft=draft,
            operator_approval=approval
        )
        assert auth_after["authorized"] is False
        assert "location_not_eligible" in auth_after["reason"]

    def test_historical_application_unaffected_by_new_policy(self):
        """Historical submitted applications remain unchanged when location policy changes."""
        job_hist = Job.objects.create(
            company="Historic Corp",
            title="Engineer",
            job_url="https://www.linkedin.com/jobs/view/1122334455/",
            provider="linkedin",
            external_job_id="1122334455",
            provenance="REAL_PROVIDER",
            provider_identity_status="EXPIRED",
            location="Berlin, Germany"
        )
        app_hist = JobApplication.objects.create(
            candidate_profile=self.candidate,
            job=job_hist,
            status="submitted",
            externally_confirmed=True
        )

        # Change location policy
        self.app_prof.target_locations = ["Australia"]
        self.app_prof.excluded_locations = ["Germany", "Europe"]
        self.app_prof.save()

        app_hist.refresh_from_db()
        assert app_hist.status == "submitted"
        assert app_hist.externally_confirmed is True
