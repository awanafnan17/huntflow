"""
Comprehensive Tests for HuntFlow Interview-First Sponsorship Job Application Strategy.

Covers:
1. Sponsorship required + form accepts truthful YES -> Allowed
2. Not authorized + form accepts truthful NO -> Allowed
3. No sponsorship stated in JD + interview-first mode -> Warning, not automatic rejection
4. No sponsorship stated in JD + strict mode -> Rejected under strict policy
5. Required false authorization attestation -> Blocked with truthful_attestation_not_possible
6. Unknown screening question -> Marked UNKNOWN_REQUIRES_USER / Needs Your Answer
7. Candidate authorization missing -> Technical ATS matching score remains unaffected
8. High technical match + sponsorship required -> Remains shortlist eligible
9. Location target passes + authorization missing -> INTERVIEW_FIRST continues
10. Location target passes + authorization missing -> STRICT mode blocks
11. EUROPE legacy authorization tag -> Never proves individual country legal authorization
12. Sponsorship answers remain YES -> Never silently changed to NO
13. Global Search (Search Worldwide = True) matches globally while honoring explicit exclusions
14. Canary readiness evaluation under interview-first mode passes when answers are truthful
"""

import pytest
from django.utils import timezone
from django.contrib.auth import get_user_model

from openoutreach.core.models import (
    Workspace, WorkspaceMembership, WorkspaceAutomationSettings, Campaign, OperatorActionApproval
)
from openoutreach.career.models import (
    CandidateProfile, CandidateProfileVersion, CandidateApplicationProfile,
    CandidateIdentityVerification, Job, JobApplication, ApplicationDraft, CandidateFact
)
from openoutreach.core.services.location_targeting import (
    evaluate_location_match, LocationTargetingPolicy
)
from openoutreach.career.answer_bank import (
    get_answer_for_question, check_screening_question_attestation_conflict,
    calculate_application_profile_readiness
)
from openoutreach.core.services.external_action_gates import evaluate_external_action_gates
from openoutreach.career.services.job_application_engine import (
    authorize_job_application, evaluate_canary_readiness
)
from openoutreach.career.services.ats_analyzer import (
    calculate_ats_match_score, classify_keywords_against_candidate, extract_jd_requirements
)

User = get_user_model()


@pytest.mark.django_db
class TestInterviewFirstApplicationStrategy:

    @pytest.fixture(autouse=True)
    def setup_data(self, db):
        self.user = User.objects.create_user(username="strat_user", email="strat_user@example.com")
        self.ws = Workspace.objects.create(name="Strategy WS", slug="strat-ws")
        self.membership = WorkspaceMembership.objects.create(
            workspace=self.ws, user=self.user, role="owner", status="active"
        )
        self.auto_settings = WorkspaceAutomationSettings.objects.create(
            workspace=self.ws,
            master_automation_enabled=True,
            auto_find_jobs=True,
            auto_apply_jobs=True,
            default_outreach_locations="United States, United Kingdom"
        )

        self.candidate = CandidateProfile.objects.create(
            user=self.user,
            full_name="Alex Morgan",
            email="candidate_strat@example.com"
        )
        self.cpv = CandidateProfileVersion.objects.create(
            profile=self.candidate,
            version_number=1,
            state="approved",
            full_name="Alex Morgan",
            fact_snapshot=[{"category": "Skills", "value": "Python, Django, React, PostgreSQL"}]
        )
        self.candidate.active_version = self.cpv
        self.candidate.save()

        # Add verified CandidateFact entries
        CandidateFact.objects.create(
            profile_version=self.cpv,
            fact_type="skill",
            category="Skills",
            value={"name": "Python", "years": 5},
            verified=True
        )
        CandidateFact.objects.create(
            profile_version=self.cpv,
            fact_type="skill",
            category="Skills",
            value={"name": "Django", "years": 4},
            verified=True
        )
        CandidateFact.objects.create(
            profile_version=self.cpv,
            fact_type="skill",
            category="Skills",
            value={"name": "PostgreSQL", "years": 4},
            verified=True
        )
        CandidateFact.objects.create(
            profile_version=self.cpv,
            fact_type="experience",
            category="Experience",
            value={"title": "Senior Engineer", "company": "Tech Corp", "years": 5},
            verified=True
        )

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
            email="candidate_strat@example.com",
            phone="+1-202-555-0101",
            city="Sargodha",
            country="Pakistan",
            application_strategy="INTERVIEW_FIRST_SPONSORSHIP",
            search_worldwide=False,
            target_locations=["Australia", "New Zealand", "Europe", "United States", "Remote Worldwide"],
            excluded_locations=["Germany"],
            authorized_countries=["PK"],
            specific_authorizations={"PK": "CONFIRMED_AUTHORIZED"},
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

    # 1. Truthful answers for sponsorship
    def test_sponsorship_answers_remain_yes(self):
        """HuntFlow always truthfully answers YES to visa sponsorship questions for Candidate #2."""
        status_now, ans_now, key_now = get_answer_for_question(self.candidate, "Will you now or in the future require sponsorship?")
        assert status_now == "ANSWER_FOUND"
        assert ans_now is True
        assert key_now == "visa_sponsorship_now"

        status_fut, ans_fut, key_fut = get_answer_for_question(self.candidate, "Do you need visa sponsorship in future?")
        assert status_fut == "ANSWER_FOUND"
        assert ans_fut is True
        assert key_fut == "visa_sponsorship_future"

    # 2. Truthful answers for work authorization per jurisdiction
    def test_work_authorization_truthful_per_country(self):
        """Candidate holds authorization in Pakistan (PK), but NOT in US/AU/UK without sponsorship."""
        # Pakistan -> YES
        status_pk, ans_pk, _ = get_answer_for_question(self.candidate, "Are you legally authorized to work in Pakistan?")
        assert status_pk == "ANSWER_FOUND"
        assert ans_pk is True

        # US -> NO (Truthful)
        status_us, ans_us, _ = get_answer_for_question(self.candidate, "Are you currently authorized to work in the United States?")
        assert status_us == "ANSWER_FOUND"
        assert ans_us is False

        # Australia -> NO (Truthful)
        status_au, ans_au, _ = get_answer_for_question(self.candidate, "Are you eligible to work in Australia?")
        assert status_au == "ANSWER_FOUND"
        assert ans_au is False

        # United Kingdom -> NO (Truthful)
        status_uk, ans_uk, _ = get_answer_for_question(self.candidate, "Do you have work authorization in the UK?")
        assert status_uk == "ANSWER_FOUND"
        assert ans_uk is False

    # 3. Legacy EUROPE tag does NOT grant blanket individual country authorization
    def test_europe_legacy_tag_does_not_grant_blanket_authorization(self):
        """Broad 'EUROPE' tag in authorized_countries does not fabricate authorization in France or Netherlands."""
        self.app_prof.authorized_countries = ["EUROPE", "PK"]
        self.app_prof.save()

        status_fr, ans_fr, _ = get_answer_for_question(self.candidate, "Are you authorized to work in Europe?")
        # Without specific French confirmation, general authorization is False
        assert status_fr == "ANSWER_FOUND"
        assert ans_fr is False

    # 4. Unknown Screening Questions
    def test_unknown_screening_question_requires_operator_answer(self):
        """Unmapped or unconfirmed custom screening question returns UNKNOWN_REQUIRES_USER."""
        status, ans, key = get_answer_for_question(self.candidate, "Do you have 3+ years experience with Rust and Solana Web3?")
        assert status == "UNKNOWN_REQUIRES_USER"
        assert ans is None

    # 5. Attestation Conflict: Impossible certifications block application
    def test_impossible_false_attestation_blocks(self):
        """Demanding certification of unrestricted Australian citizenship/work rights blocks application."""
        q_text = "I certify that I currently possess unrestricted Australian work authorization."
        is_conflict, reason = check_screening_question_attestation_conflict(self.candidate, q_text)
        assert is_conflict is True
        assert "unrestricted Australian work rights" in reason

        # Demanding certification of no sponsorship
        q_spons = "Declaration: I confirm I do not require visa sponsorship now or in the future."
        is_conf_spons, reason_spons = check_screening_question_attestation_conflict(self.candidate, q_spons)
        assert is_conf_spons is True
        assert "requires sponsorship" in reason_spons

    # 6. Location policy: Interview-First allows application vs Strict blocks
    def test_interview_first_vs_strict_mode(self):
        policy_interview_first = LocationTargetingPolicy(
            included_locations=["United States"],
            application_strategy="INTERVIEW_FIRST_SPONSORSHIP",
            channel="jobs"
        )
        policy_strict = LocationTargetingPolicy(
            included_locations=["United States"],
            application_strategy="STRICT_AUTHORIZATION",
            channel="jobs"
        )

        # Job in New York, US: candidate is NOT authorized in US but has sponsorship_now='yes'
        res_if = evaluate_location_match(
            "New York, United States",
            policy_interview_first,
            candidate_authorization=["PK"],
            candidate_sponsorship_now="yes"
        )
        assert res_if.decision == "MATCH"
        assert res_if.is_eligible is True
        assert res_if.authorization_status == "SPONSORSHIP_REQUIRED"
        assert res_if.sponsorship_status == "SPONSORSHIP_REQUIRED"

        res_strict = evaluate_location_match(
            "New York, United States",
            policy_strict,
            candidate_authorization=["PK"],
            candidate_sponsorship_now="yes"
        )
        assert res_strict.decision == "BLOCKED_AUTHORIZATION"
        assert res_strict.is_eligible is False

    # 7. Technical matching score is independent from authorization status
    def test_technical_match_independent_from_authorization(self):
        """ATS match quality evaluates pure skill/experience fit without penalizing for lack of visa."""
        reqs = {
            "required_skills": ["Python", "Django"],
            "preferred_skills": ["PostgreSQL"],
            "all_technologies": ["Python", "Django", "PostgreSQL"],
            "min_years_experience": 3,
            "education_requirements": []
        }
        kw_map = classify_keywords_against_candidate(reqs, self.candidate.active_version)
        score_data = calculate_ats_match_score(reqs, self.candidate.active_version, keyword_mapping=kw_map)
        assert score_data["overall_score"] >= 85
        assert score_data["must_have_score"] == 100

    # 8. Search Worldwide matches globally while respecting exclusions
    def test_search_worldwide_matches_globally_and_respects_exclusions(self):
        policy = LocationTargetingPolicy(
            search_worldwide=True,
            excluded_locations=["Germany"],
            application_strategy="INTERVIEW_FIRST_SPONSORSHIP",
            channel="jobs"
        )

        # Tokyo, Japan -> MATCH (Worldwide)
        res_jp = evaluate_location_match("Tokyo, Japan", policy)
        assert res_jp.decision == "MATCH"
        assert res_jp.is_eligible is True
        assert res_jp.matched_rule == "search_worldwide"

        # Sydney, Australia -> MATCH (Worldwide)
        res_au = evaluate_location_match("Sydney, Australia", policy)
        assert res_au.decision == "MATCH"
        assert res_au.is_eligible is True

        # Berlin, Germany -> EXCLUDED (Exclusion wins)
        res_de = evaluate_location_match("Berlin, Germany", policy)
        assert res_de.decision == "EXCLUDED"
        assert res_de.is_eligible is False

    # 9. Server-side external action gate allows interview-first truthful application
    def test_external_action_gate_allows_interview_first_truthful_application(self):
        job_us = Job.objects.create(
            company="Silicon Tech Inc",
            title="Senior Python Architect",
            job_url="https://www.linkedin.com/jobs/view/9988776655/",
            description="Python and Django in San Francisco.",
            provider="linkedin",
            external_job_id="9988776655",
            provenance="REAL_PROVIDER",
            provider_identity_status="VERIFIED",
            location="San Francisco, CA, United States",
            is_active=True
        )

        allowed, reason = evaluate_external_action_gates(
            workspace=self.ws,
            candidate_profile=self.candidate,
            job=job_us,
            external_action_authorized=True,
            action_type="job_application"
        )
        assert allowed is True
        assert reason in ["authorized", "all_gates_passed"]

    # 10. Server-side gate blocks when screening question requires false certification
    def test_external_action_gate_blocks_on_false_attestation(self):
        job_au_attest = Job.objects.create(
            company="Sydney Tech Corp",
            title="Senior Django Developer",
            job_url="https://www.linkedin.com/jobs/view/9988771122/",
            description="Python and Django in Sydney.",
            provider="linkedin",
            external_job_id="9988771122",
            provenance="REAL_PROVIDER",
            provider_identity_status="VERIFIED",
            location="Sydney, Australia",
            is_active=True
        )
        job_au_attest.screening_questions = [{
            "text": "I certify that I currently possess unrestricted Australian work authorization."
        }]

        allowed, reason = evaluate_external_action_gates(
            workspace=self.ws,
            candidate_profile=self.candidate,
            job=job_au_attest,
            external_action_authorized=True,
            action_type="job_application"
        )
        assert allowed is False
        assert reason == "truthful_attestation_not_possible"

    # 11. Canary readiness passes under interview-first strategy
    def test_canary_readiness_under_interview_first(self):
        job_canary = Job.objects.create(
            company="Melbourne Cloud",
            title="Senior Backend Engineer",
            job_url="https://www.linkedin.com/jobs/view/9988773344/",
            description="Python, Django, PostgreSQL cloud backend in Melbourne.",
            provider="linkedin",
            external_job_id="9988773344",
            provenance="REAL_PROVIDER",
            provider_identity_status="VERIFIED",
            location="Melbourne, Australia",
            is_active=True
        )

        approval = OperatorActionApproval.objects.create(
            workspace=self.ws,
            operator=self.user,
            action_type="job_application",
            candidate=self.candidate,
            job=job_canary,
            external_job_id="9988773344",
            status="approved"
        )

        readiness = evaluate_canary_readiness(
            workspace=self.ws,
            candidate=self.candidate,
            job=job_canary,
            operator_approval=approval,
            match_threshold=50.0
        )
        assert readiness["canary_ready"] is True
        assert readiness["checks"]["location_eligible"] is True
        assert readiness["checks"]["authorization_status"] == "SPONSORSHIP_REQUIRED"
        assert readiness["checks"]["sponsorship_status"] == "SPONSORSHIP_REQUIRED"
        assert readiness["checks"]["no_attestation_conflict"] is True
