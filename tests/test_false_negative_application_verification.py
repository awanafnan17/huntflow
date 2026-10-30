"""
Tests for False-Negative Application Verification, Multi-Source Evidence Precedence,
LinkedIn Email Confirmation Parsing, and Xorora Submission Truth Reconciliation.
"""
import pytest
from unittest.mock import MagicMock
from django.utils import timezone
from django.contrib.auth import get_user_model

from openoutreach.core.models import Workspace, WorkspaceMembership, Task
from openoutreach.core.services.entitlements import get_or_create_workspace_entitlement
from openoutreach.career.models import (
    CandidateProfile, CandidateProfileVersion, CandidateIdentityVerification,
    CandidateFact, Job, JobApplication, ApplicationDraft, CandidateApplicationProfile
)
from openoutreach.career.services.job_application_engine import (
    prepare_job_application, execute_job_application, enqueue_job_application_task,
    record_external_confirmation
)
from openoutreach.career.services.confirmation_evidence import (
    ConfirmationEvidenceType, VerificationStatus, VerificationResult,
    verify_linkedin_application_email, can_downgrade_confirmation
)
from openoutreach.career.services.ats_analyzer import (
    extract_jd_requirements, calculate_ats_match_score, classify_keywords_against_candidate
)
from openoutreach.linkedin.browser.easy_apply import (
    verify_linkedin_job_applied, LinkedInEasyApplyAdapter
)

User = get_user_model()


class MockLocator:
    def __init__(self, elements=None):
        self._elements = elements or []

    def count(self):
        return len(self._elements)

    def nth(self, i):
        return self._elements[i] if 0 <= i < len(self._elements) else MockElement(visible=False)

    @property
    def first(self):
        return self._elements[0] if self._elements else MockElement(visible=False)

    def all(self):
        return self._elements

    def is_visible(self):
        return len(self._elements) > 0 and self._elements[0]._visible


class MockElement:
    def __init__(self, tag="div", attrs=None, text="", visible=True):
        self._tag = tag
        self._attrs = attrs or {}
        self._text = text
        self._visible = visible

    def get_attribute(self, attr):
        return self._attrs.get(attr, "")

    def inner_text(self):
        return self._text

    def is_visible(self):
        return self._visible

    def count(self):
        return 1

    def evaluate(self, expr):
        return self._tag

    def locator(self, sel):
        return MockLocator([])


class MockPlaywrightPage:
    def __init__(self, url="https://www.linkedin.com/", dom_map=None):
        self.url = url
        self.dom_map = dom_map or {}

    def goto(self, url, **kwargs):
        self.url = url

    def wait_for_timeout(self, ms):
        pass

    def locator(self, selector):
        for k, v in self.dom_map.items():
            if k in selector or selector in k:
                return v
        return MockLocator([])


@pytest.mark.django_db
class TestFalseNegativeAndMultiSourceConfirmation:

    def setup_method(self):
        self.user = User.objects.create_user(username="test_fn_user", email="candidate-2@example.com")
        self.ws = Workspace.objects.create(name="Test FN WS")
        WorkspaceMembership.objects.create(user=self.user, workspace=self.ws, role="owner", status="active")
        get_or_create_workspace_entitlement(self.ws)

        self.candidate = CandidateProfile.objects.create(
            user=self.user,
            full_name="Alex Morgan",
            email="candidate-2@example.com"
        )
        self.cand_app_prof = CandidateApplicationProfile.objects.create(
            candidate_profile=self.candidate,
            email="candidate-2@example.com",
            phone="+1-202-555-0100",
            city="Islamabad",
            country="Pakistan",
            willing_to_relocate=False,
            requires_sponsorship_now=False,
            authorized_countries=["US", "PK"],
            minimum_salary=100000,
            notice_period="2 weeks"
        )
        self.version = CandidateProfileVersion.objects.create(
            profile=self.candidate,
            version_number=1,
            state="approved"
        )
        self.candidate.active_version = self.version
        self.candidate.save(update_fields=["active_version"])

        CandidateIdentityVerification.objects.create(
            candidate_profile=self.candidate,
            status="verified",
            extracted_name=self.candidate.full_name,
            confirmed_name=self.candidate.full_name,
            verification_source="passport",
            verified_at=timezone.now()
        )

        CandidateFact.objects.create(
            profile_version=self.version,
            fact_type="skill",
            category="skill",
            value={"name": "Python", "years": 5}
        )

        self.job = Job.objects.create(
            title="Senior Software Engineer (Full-Stack / AI)",
            company="Xorora",
            job_url="https://www.linkedin.com/jobs/view/4452269262/",
            description="We are seeking a Senior Software Engineer with Python and AI expertise.",
            is_active=True
        )

    def test_scenario_a_post_submit_confirmation_and_tracker_fails(self):
        """Scenario A: Post-submit modal confirmation captured, tracker fails later -> CONFIRMED."""
        app = JobApplication.objects.create(
            candidate_profile=self.candidate,
            job=self.job,
            status="draft"
        )
        # Record post-submit modal confirmation
        record_external_confirmation(
            application=app,
            provider="linkedin",
            external_job_id="4452269262",
            evidence_type=ConfirmationEvidenceType.POST_SUBMIT_CONFIRMATION,
            metadata={"dialog_header": "Application submitted"}
        )
        assert app.status == "submitted"
        assert app.externally_confirmed is True

        # Later tracker check returns UNKNOWN (0 cards / selector failure)
        dom = {
            ".jobs-s-apply button": MockLocator([]),
            "a[href*=\"/jobs/view/4452269262\"]": MockLocator([]) # 0 cards in tracker
        }
        mock_page = MockPlaywrightPage(url="https://www.linkedin.com/jobs/view/4452269262/", dom_map=dom)
        res = verify_linkedin_job_applied(session=None, external_job_id="4452269262", page=mock_page)
        
        assert res.status == VerificationStatus.UNKNOWN
        assert can_downgrade_confirmation(app, res.status, res.evidence_type) is False
        assert app.status == "submitted"
        assert app.externally_confirmed is True

    def test_scenario_b_confirmation_email_and_tracker_returns_zero(self):
        """Scenario B: Confirmation email validated, tracker returns 0 cards -> CONFIRMED."""
        app = JobApplication.objects.create(
            candidate_profile=self.candidate,
            job=self.job,
            status="draft"
        )
        email_meta = {
            "sender": "jobs-listings@linkedin.com",
            "subject": "Your application was sent to Xorora",
            "recipient": "candidate-2@example.com",
            "company": "Xorora",
            "external_job_id": "4452269262",
            "received_at": "2026-08-18T07:37:12Z",
            "message_id": "<test-msg-id-123@linkedin.com>"
        }
        valid, ev_type, meta = verify_linkedin_application_email(email_meta, self.candidate, self.job)
        assert valid is True
        assert ev_type == ConfirmationEvidenceType.LINKEDIN_EMAIL
        assert meta["company"] == "Xorora"

        record_external_confirmation(
            application=app,
            provider="linkedin",
            external_job_id="4452269262",
            evidence_type=ev_type,
            metadata=meta
        )
        assert app.status == "submitted"
        assert app.externally_confirmed is True

        # Tracker returns zero
        dom = {
            ".jobs-s-apply button": MockLocator([]),
            "a[href*=\"/jobs/view/4452269262\"]": MockLocator([])
        }
        mock_page = MockPlaywrightPage(url="https://www.linkedin.com/jobs/view/4452269262/", dom_map=dom)
        res = verify_linkedin_job_applied(session=None, external_job_id="4452269262", page=mock_page)
        
        assert res.status == VerificationStatus.UNKNOWN
        assert can_downgrade_confirmation(app, res.status, res.evidence_type) is False

    def test_scenario_c_no_positive_evidence_tracker_unavailable(self):
        """Scenario C: No positive evidence, tracker unavailable -> UNKNOWN (NOT not_applied)."""
        dom = {
            ".jobs-s-apply button": MockLocator([]),
            "a[href*=\"/jobs/view/4452269262\"]": MockLocator([])
        }
        mock_page = MockPlaywrightPage(url="https://www.linkedin.com/jobs/view/4452269262/", dom_map=dom)
        res = verify_linkedin_job_applied(session=None, external_job_id="4452269262", page=mock_page)

        assert res.status == VerificationStatus.UNKNOWN
        assert res.is_applied is False
        assert res.evidence_type == "tracker_unmatched_or_unavailable"

    def test_scenario_d_exact_job_evidence_shows_apply_available(self):
        """Scenario D: Exact job-specific evidence shows Apply still available -> NOT_APPLIED."""
        dom = {
            ".jobs-s-apply button": MockLocator([
                MockElement(tag="button", attrs={"aria-label": "Apply to Xorora"}, text="Apply")
            ])
        }
        mock_page = MockPlaywrightPage(url="https://www.linkedin.com/jobs/view/4452269262/", dom_map=dom)
        res = verify_linkedin_job_applied(session=None, external_job_id="4452269262", page=mock_page)

        assert res.status == VerificationStatus.NOT_APPLIED
        assert res.is_applied is False
        assert res.evidence_type == "not_applied_active_button_present"

    def test_scenario_e_generic_applied_text_must_not_confirm(self):
        """Scenario E: Generic text 'applied LLM systems' in description must NOT confirm."""
        # Generic text is in body/description, but top card feedback is absent
        dom = {
            ".jobs-s-apply button": MockLocator([
                MockElement(tag="button", attrs={"aria-label": "Apply to Xorora"}, text="Apply")
            ]),
            ".jobs-description": MockLocator([
                MockElement(tag="p", text="Candidate will build applied LLM systems.")
            ])
        }
        mock_page = MockPlaywrightPage(url="https://www.linkedin.com/jobs/view/4452269262/", dom_map=dom)
        res = verify_linkedin_job_applied(session=None, external_job_id="4452269262", page=mock_page)

        assert res.is_applied is False
        assert res.status == VerificationStatus.NOT_APPLIED

    def test_scenario_f_unrelated_linkedin_email_must_not_confirm(self):
        """Scenario F: Unrelated LinkedIn application email for different company must NOT confirm target job."""
        email_meta = {
            "sender": "jobs-listings@linkedin.com",
            "subject": "Your application was sent to OtherCorp Inc",
            "recipient": "candidate-2@example.com",
            "company": "OtherCorp Inc",
            "external_job_id": "9999999999",
            "received_at": "2026-08-18T07:37:12Z",
            "message_id": "<other-msg-id@linkedin.com>"
        }
        valid, ev_type, meta = verify_linkedin_application_email(email_meta, self.candidate, self.job)
        assert valid is False
        assert ev_type == "job_metadata_mismatch"

    def test_scenario_g_positive_evidence_cannot_be_downgraded_by_weak_evidence(self):
        """Scenario G: Positive evidence cannot be downgraded by weak negative / UNKNOWN evidence."""
        app = JobApplication.objects.create(
            candidate_profile=self.candidate,
            job=self.job,
            status="submitted",
            externally_confirmed=True,
            confirmation_provider="linkedin",
            confirmation_evidence_type=ConfirmationEvidenceType.LINKEDIN_EMAIL,
            confirmation_checked_at=timezone.now()
        )

        assert can_downgrade_confirmation(app, VerificationStatus.UNKNOWN, "tracker_unmatched_or_unavailable") is False
        assert can_downgrade_confirmation(app, VerificationStatus.NOT_APPLIED, "not_applied_active_button_present") is False
        # Only explicit manual operator audit rejection can downgrade
        assert can_downgrade_confirmation(app, VerificationStatus.NOT_APPLIED, "manual_operator_rejection") is True

    def test_scenario_h_xorora_reapply_attempt_blocked(self):
        """Scenario H: Future scheduler/daemon attempt to reapply for Xorora is BLOCKED."""
        # Ensure application is in submitted & externally_confirmed state
        app, _ = JobApplication.objects.get_or_create(
            candidate_profile=self.candidate,
            job=self.job,
            defaults={"status": "submitted", "externally_confirmed": True}
        )
        app.status = "submitted"
        app.externally_confirmed = True
        app.confirmation_evidence_type = ConfirmationEvidenceType.LINKEDIN_EMAIL
        app.save()

        # 1. prepare_job_application blocks duplicate
        prep_res = prepare_job_application(workspace=self.ws, candidate=self.candidate, job=self.job)
        assert prep_res["prepared"] is False
        assert prep_res["reason"] == "duplicate_application_blocked"

        # 2. enqueue_job_application_task returns None (blocked)
        draft = ApplicationDraft.objects.create(
            application=app,
            version_number=1,
            tailored_cv="CV",
            cover_letter="CL"
        )
        task = enqueue_job_application_task(
            workspace=self.ws,
            candidate=self.candidate,
            job=self.job,
            draft=draft
        )
        assert task is None

    def test_scenario_i_ats_score_separated_from_submission_truth(self):
        """Scenario I: ATS quality analysis (39% Low Match) remains separate from submission truth (submitted)."""
        app, _ = JobApplication.objects.get_or_create(
            candidate_profile=self.candidate,
            job=self.job,
            defaults={"status": "submitted", "externally_confirmed": True}
        )
        app.status = "submitted"
        app.externally_confirmed = True
        app.save()
        assert app.status == "submitted"
        assert app.externally_confirmed is True

        # ATS match score recomputed from JD & candidate facts
        facts = list(self.candidate.active_version.facts.all())
        jd_reqs = extract_jd_requirements(self.job.description)
        kw_map = classify_keywords_against_candidate(jd_reqs, facts)
        score = calculate_ats_match_score(jd_reqs, kw_map)

        # Quality fact: score is Low Match / Reject (not auto-apply candidate)
        assert score["priority_band"] == "Low Match / Reject"
        # Submission fact: status is submitted and externally confirmed
        assert app.status == "submitted"
        assert app.externally_confirmed is True
