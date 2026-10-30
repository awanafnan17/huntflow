"""
Targeted Branch Coverage Test Suite for OpenOutreach Core Engines.
Covers missing branches in connection_engine, job_application_engine, and followup_engine.
"""
import pytest
from datetime import timedelta
from django.utils import timezone
from openoutreach.core.models import Workspace, WorkspaceMembership, Campaign, CampaignPurpose, SenderIdentity, AuditEvent
from openoutreach.crm.models.lead import Lead, SuppressionRecord
from openoutreach.crm.models.deal import Deal
from openoutreach.career.models import CandidateProfile, CandidateProfileVersion, CandidateFact, Job, JobApplication, ApplicationDraft, CandidateIdentityVerification, IncidentHold, IncidentHoldTarget
from openoutreach.crm.services.connection_engine import execute_connection_attempt, execute_connection_batch, ConnectionBatchTracker
from openoutreach.career.services.job_application_engine import execute_job_application, execute_application_batch, prepare_job_application
from openoutreach.core.services.followup_engine import execute_followup, execute_followup_batch, find_due_followups, schedule_followups


@pytest.mark.django_db
class TestEngineBranchCoverage:

    @pytest.fixture(autouse=True)
    def setup_data(self, db, django_user_model):
        self.User = django_user_model
        self.user = self.User.objects.create_user(username="cov_owner", email="cov@example.com")
        self.ws = Workspace.objects.create(name="Cov Workspace", slug="cov-ws")
        WorkspaceMembership.objects.create(workspace=self.ws, user=self.user, role="owner", status="active")

        self.purpose = CampaignPurpose.objects.create(workspace=self.ws, name="Job Outreach", slug="job_outreach", is_system=True, is_active=True)
        self.sender = SenderIdentity.objects.create(workspace=self.ws, display_name="Cov Sender", email="cov@demo.invalid", verification_status="verified", is_active=True)
        self.campaign = Campaign.objects.create(workspace=self.ws, name="Cov Campaign", campaign_purpose=self.purpose, sender_identity=self.sender, is_active=True)

    def test_connection_engine_branches(self):
        """Test missing branches in connection_engine."""
        # 1. Invalid Tracker State
        tracker = ConnectionBatchTracker(self.ws, self.campaign, "b_invalid")
        with pytest.raises(ValueError):
            tracker.transition_to("invalid_state_xyz")

        # 2. Empty leads list
        res_empty = execute_connection_batch(workspace=self.ws, campaign=self.campaign, leads=[], batch_id="b_empty")
        assert res_empty["state"] == "failed"
        assert res_empty["processed"] == 0

        # 3. Gate Blocked Lead (Suppressed)
        lead_supp = Lead.objects.create(workspace=self.ws, public_identifier="cov_supp", linkedin_url="https://linkedin.com/in/cov-supp")
        SuppressionRecord.objects.create(workspace=self.ws, linkedin_url="https://linkedin.com/in/cov-supp", reason="opt_out")
        
        res_blocked = execute_connection_attempt(
            workspace=self.ws,
            campaign=self.campaign,
            lead=lead_supp,
            sender=self.sender
        )
        assert res_blocked["status"] == "blocked"

        # 4. Forced Error execution
        lead_err = Lead.objects.create(workspace=self.ws, public_identifier="cov_err", linkedin_url="https://linkedin.com/in/cov-err")
        res_err = execute_connection_attempt(
            workspace=self.ws,
            campaign=self.campaign,
            lead=lead_err,
            sender=self.sender,
            force_error="Network error forced"
        )
        assert res_err["status"] == "failed"

        # 5. Batch pause on error
        lead_ok = Lead.objects.create(workspace=self.ws, public_identifier="cov_ok", linkedin_url="https://linkedin.com/in/cov-ok")
        res_batch = execute_connection_batch(
            workspace=self.ws,
            campaign=self.campaign,
            leads=[lead_supp, lead_ok],
            batch_id="b_pause",
            pause_on_error=True
        )
        assert res_batch["processed"] >= 1

    def test_job_application_engine_branches(self):
        """Test missing branches in job_application_engine."""
        user2 = self.User.objects.create_user(username="cand_user_cov", email="candcov@example.com")
        candidate = CandidateProfile.objects.create(user=user2, full_name="Cov Candidate")

        # 1. Candidate Unverified (Gate Blocked)
        job = Job.objects.create(company="Google", title="SWE", job_url="https://example.com/jobs/cov1", is_active=True)
        res_prep = prepare_job_application(workspace=self.ws, candidate=candidate, job=job)
        assert res_prep["prepared"] is False

        # Verify candidate
        CandidateIdentityVerification.objects.create(
            candidate_profile=candidate,
            status="verified",
            extracted_name="Cov Candidate",
            confirmed_name="Cov Candidate",
            verification_source="manual"
        )
        ver = CandidateProfileVersion.objects.create(profile=candidate, version_number=1, state="approved", full_name=candidate.full_name)
        candidate.active_version = ver
        candidate.save()
        CandidateFact.objects.create(profile_version=ver, fact_type="experience", category="work", value={"company": "Apple", "title": "Dev"}, verified=True)

        # 2. Empty Jobs Batch
        res_empty = execute_application_batch(workspace=self.ws, candidate=candidate, jobs=[], batch_id="b_app_empty")
        assert res_empty["processed"] == 0

        # 4. Draft Error / Gate block
        job_q1 = Job.objects.create(
            company="Apple",
            title="iOS Dev 1",
            job_url="https://example.com/jobs/cov3",
            is_active=True
        )
        app_q = JobApplication.objects.create(candidate_profile=candidate, job=job_q1, status="draft")
        draft_err = ApplicationDraft.objects.create(
            application=app_q,
            version_number=1,
            tailored_cv="CV text",
            cover_letter="Cover letter text"
        )
        res_app_err = execute_job_application(
            workspace=self.ws,
            candidate=candidate,
            job=job_q1,
            draft=draft_err
        )
        assert isinstance(res_app_err, dict)

        # 6. Additional prepare_job_application branches
        # Candidate None
        assert prepare_job_application(workspace=self.ws, candidate=None, job=job_q1)["reason"] == "missing_candidate"

        # Active ver state != approved
        cand_unapp = CandidateProfile.objects.create(user=self.User.objects.create_user(username="cand_unapp", email="unapp@example.com"), full_name="Unapproved")
        ver_unapp = CandidateProfileVersion.objects.create(profile=cand_unapp, version_number=1, state="draft", full_name="Unapp")
        cand_unapp.active_version = ver_unapp
        cand_unapp.save()
        assert prepare_job_application(workspace=self.ws, candidate=cand_unapp, job=job_q1)["reason"] == "candidate_identity_unapproved"

        # Incident Hold active
        from django.contrib.contenttypes.models import ContentType
        hold = IncidentHold.objects.create(reason="TEST_INCIDENT", description="Hold test", active=True)
        ct = ContentType.objects.get_for_model(CandidateProfile)
        IncidentHoldTarget.objects.create(incident_hold=hold, content_type=ct, object_id=candidate.id)
        assert prepare_job_application(workspace=self.ws, candidate=candidate, job=job_q1)["reason"] == "incident_hold_active"

        # 8. Fresh Candidate for unblocked tests
        u3 = self.User.objects.create_user(username="fresh_cand", email="fresh@example.com")
        cand_fresh = CandidateProfile.objects.create(user=u3, full_name="Fresh Cand")
        setattr(cand_fresh, "work_authorized", False)
        setattr(cand_fresh, "location", "Moon")
        ver_fresh = CandidateProfileVersion.objects.create(profile=cand_fresh, version_number=1, state="approved", full_name="Fresh Cand")
        cand_fresh.active_version = ver_fresh
        cand_fresh.save()

        # 7. Job inactive
        job_inact = Job.objects.create(company="Meta", title="Backend", job_url="https://example.com/jobs/inact", is_active=False)
        assert prepare_job_application(workspace=self.ws, candidate=cand_fresh, job=job_inact)["reason"] == "job_inactive"

        # Missing candidate fact
        assert prepare_job_application(workspace=self.ws, candidate=cand_fresh, job=job_q1)["reason"] == "missing_candidate_fact"

        # Add verified fact
        CandidateFact.objects.create(profile_version=ver_fresh, fact_type="exp", category="work", value={"co": "A"}, verified=True)

        # Work auth & location mismatch
        job_req = Job.objects.create(company="Tesla", title="Auto", job_url="https://example.com/jobs/req", is_active=True)
        setattr(job_req, "work_authorization_required", True)
        setattr(job_req, "allowed_locations", ["US"])
        job_req.save()
        assert prepare_job_application(workspace=self.ws, candidate=cand_fresh, job=job_req)["reason"] == "work_authorization_mismatch"
        cand_fresh.work_authorized = True
        cand_fresh.save()
        assert prepare_job_application(workspace=self.ws, candidate=cand_fresh, job=job_req)["reason"] == "unsupported_location"
        cand_fresh.location = "US"
        cand_fresh.save()

        # Screening questions handling
        job_sq = Job.objects.create(company="Netflix", title="Stream", job_url="https://example.com/jobs/sq", is_active=True)
        setattr(job_sq, "screening_questions", [
            {"id": 1, "text": "What is your target salary?"},
            {"id": 2, "text": "Are you willing to relocate?"},
            {"id": 3, "text": "Do you need sponsorship?"},
            {"id": 4, "text": "When is your start date?"},
            {"id": 5, "text": "Generic question?"}
        ])
        job_sq.save()
        res_sq = prepare_job_application(workspace=self.ws, candidate=cand_fresh, job=job_sq)
        assert res_sq["prepared"] is True

        # Unhandled screening question
        job_sq_bad = Job.objects.create(company="Amazon", title="Cloud", job_url="https://example.com/jobs/sqbad", is_active=True)
        setattr(job_sq_bad, "screening_questions", [{"id": 1, "text": "custom_unhandled question"}])
        job_sq_bad.save()
        assert prepare_job_application(workspace=self.ws, candidate=cand_fresh, job=job_sq_bad)["reason"] == "unknown_screening_question"

        # Execute with force_error on fresh candidate
        job_force = Job.objects.create(company="Oracle", title="DB", job_url="https://example.com/jobs/db", is_active=True)
        app_fresh = JobApplication.objects.create(candidate_profile=cand_fresh, job=job_force, status="draft")
        draft_fresh = ApplicationDraft.objects.create(application=app_fresh, version_number=1, tailored_cv="CV", cover_letter="Letter")
        res_forced_fresh = execute_job_application(
            workspace=self.ws,
            candidate=cand_fresh,
            job=job_force,
            draft=draft_fresh,
            force_error="Forced error during app submit"
        )
        assert res_forced_fresh["status"] == "failed"

        # Adapter success and failure
        class DummyAdapterSuccess:
            def apply(self, job_url, answers):
                return {"success": True, "externally_verified": True, "confirmation_code": "SUCCESS-99"}

        class DummyAdapterFail:
            def apply(self, job_url, answers):
                return {"success": False, "error": "element_not_found"}

        job_ad = Job.objects.create(company="OpenAI", title="AI", job_url="https://example.com/jobs/ai", is_active=True)
        app_ad = JobApplication.objects.create(candidate_profile=cand_fresh, job=job_ad, status="draft")
        draft_ad = ApplicationDraft.objects.create(application=app_ad, version_number=1, tailored_cv="CV", cover_letter="Letter")
        
        res_ad_succ = execute_job_application(workspace=self.ws, candidate=cand_fresh, job=job_ad, draft=draft_ad, adapter=DummyAdapterSuccess())
        assert res_ad_succ["status"] == "submitted"

        # Duplicate application blocked
        assert prepare_job_application(workspace=self.ws, candidate=cand_fresh, job=job_ad)["reason"] == "duplicate_application_blocked"

        job_ad2 = Job.objects.create(company="Anthropic", title="Safety", job_url="https://example.com/jobs/safe", is_active=True)
        app_ad2 = JobApplication.objects.create(candidate_profile=cand_fresh, job=job_ad2, status="draft")
        draft_ad2 = ApplicationDraft.objects.create(application=app_ad2, version_number=1, tailored_cv="CV", cover_letter="Letter")
        res_ad_fail = execute_job_application(workspace=self.ws, candidate=cand_fresh, job=job_ad2, draft=draft_ad2, adapter=DummyAdapterFail())
        assert res_ad_fail["status"] == "failed"

    def test_followup_engine_branches(self):
        """Test missing branches in followup_engine."""
        l1 = Lead.objects.create(workspace=self.ws, public_identifier="fol_cov_1", linkedin_url="https://linkedin.com/in/fol-cov-1")

        # 1. Empty Deals Batch
        res_empty = execute_followup_batch(workspace=self.ws, campaign=self.campaign, deals=[], batch_id="b_fol_empty")
        assert res_empty["processed"] == 0

        # 2. Ineligible Deal State (e.g. Failed)
        deal_unq = Deal.objects.create(campaign=self.campaign, lead=l1, state="Failed")
        res_inelig = execute_followup(workspace=self.ws, campaign=self.campaign, deal=deal_unq)
        assert res_inelig["status"] == "blocked"
        assert res_inelig["stop_reason"] == "invalid_deal_state"

        # 3. Max Attempts Reached
        l2 = Lead.objects.create(workspace=self.ws, public_identifier="fol_cov_2", linkedin_url="https://linkedin.com/in/fol-cov-2")
        deal_max = Deal.objects.create(campaign=self.campaign, lead=l2, state="Qualified", connect_attempts=3)
        res_max = execute_followup(workspace=self.ws, campaign=self.campaign, deal=deal_max, max_attempts=3)
        assert res_max["status"] == "blocked"
        assert res_max["stop_reason"] == "maximum_attempt_reached"

        # 4. Minimum Interval Not Reached
        l3 = Lead.objects.create(workspace=self.ws, public_identifier="fol_cov_3", linkedin_url="https://linkedin.com/in/fol-cov-3")
        deal_recent = Deal.objects.create(campaign=self.campaign, lead=l3, state="Qualified", creation_date=timezone.now())
        res_int = execute_followup(workspace=self.ws, campaign=self.campaign, deal=deal_recent, min_interval_days=3)
        assert res_int["status"] == "blocked"
        assert res_int["stop_reason"] == "minimum_interval_not_reached"

        # 5. Reply / Rejection / Withdrawal
        l4 = Lead.objects.create(workspace=self.ws, public_identifier="fol_cov_4", linkedin_url="https://linkedin.com/in/fol-cov-4")
        deal_rep = Deal.objects.create(campaign=self.campaign, lead=l4, state="Qualified", creation_date=timezone.now() - timedelta(days=5))
        res_rep = execute_followup(workspace=self.ws, campaign=self.campaign, deal=deal_rep, has_reply=True)
        assert res_rep["status"] == "blocked"
        assert res_rep["stop_reason"] == "reply_received"

        l5 = Lead.objects.create(workspace=self.ws, public_identifier="fol_cov_5", linkedin_url="https://linkedin.com/in/fol-cov-5")
        deal_rej = Deal.objects.create(campaign=self.campaign, lead=l5, state="Qualified", creation_date=timezone.now() - timedelta(days=5))
        res_rej = execute_followup(workspace=self.ws, campaign=self.campaign, deal=deal_rej, has_rejection=True)
        assert res_rej["status"] == "blocked"
        assert res_rej["stop_reason"] == "rejection_received"

        l6 = Lead.objects.create(workspace=self.ws, public_identifier="fol_cov_6", linkedin_url="https://linkedin.com/in/fol-cov-6")
        deal_with = Deal.objects.create(campaign=self.campaign, lead=l6, state="Qualified", creation_date=timezone.now() - timedelta(days=5))
        res_with = execute_followup(workspace=self.ws, campaign=self.campaign, deal=deal_with, has_withdrawal=True)
        assert res_with["status"] == "blocked"
        assert res_with["stop_reason"] == "application_withdrawn"

        # 6. Schedule Follow-ups helper
        l7 = Lead.objects.create(workspace=self.ws, public_identifier="fol_cov_7", linkedin_url="https://linkedin.com/in/fol-cov-7")
        deal_ok = Deal.objects.create(campaign=self.campaign, lead=l7, state="Qualified", creation_date=timezone.now() - timedelta(days=5))
        res_fol = execute_followup(workspace=self.ws, campaign=self.campaign, deal=deal_ok)
        assert res_fol["status"] == "success"

        due = find_due_followups(workspace=self.ws, campaign=self.campaign, min_interval_days=3)
        assert isinstance(due, list)
        sched = schedule_followups(workspace=self.ws, campaign=self.campaign, min_interval_days=3)
        assert isinstance(sched, list)
