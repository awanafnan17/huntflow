"""Deterministic Tests for Job-Specific Applied Verification & False Positive Prevention."""
import pytest
from unittest.mock import MagicMock
from django.utils import timezone
from django.contrib.auth import get_user_model

from openoutreach.core.models import Workspace, WorkspaceMembership, Task
from openoutreach.core.services.entitlements import get_or_create_workspace_entitlement
from openoutreach.career.models import (
    CandidateProfile, CandidateProfileVersion, CandidateIdentityVerification,
    Job, JobApplication, ApplicationDraft
)
from openoutreach.career.services.job_application_engine import (
    prepare_job_application, execute_job_application
)
from openoutreach.linkedin.browser.easy_apply import (
    extract_linkedin_job_id, verify_linkedin_job_applied, LinkedInEasyApplyAdapter
)

User = get_user_model()


class MockLocator:
    """Mock Playwright Locator for deterministic DOM state testing."""
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
    """Mock Playwright Element."""
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
    """Mock Playwright Page with custom DOM locator behaviors."""
    def __init__(self, url="https://www.linkedin.com/jobs/view/4440058610/", dom_map=None):
        self.url = url
        self._dom_map = dom_map or {}

    def goto(self, url, **kwargs):
        self.url = url

    def wait_for_timeout(self, ms):
        pass

    def locator(self, selector):
        sub_selectors = [s.strip() for s in selector.split(",")]
        matched_elems = []
        for sub in sub_selectors:
            for k, loc in self._dom_map.items():
                if k == sub or k in sub or sub in k:
                    matched_elems.extend(loc.all())
        if matched_elems:
            return MockLocator(matched_elems)
        return MockLocator([])


@pytest.mark.django_db
class TestJobSpecificAppliedVerification:

    @pytest.fixture(autouse=True)
    def setup_data(self, db):
        self.user = User.objects.create_user(username="cand_verif", email="cand_verif@example.com")
        self.ws = Workspace.objects.create(name="Verif WS", slug="verif-ws")
        WorkspaceMembership.objects.create(workspace=self.ws, user=self.user, role="owner", status="active")
        get_or_create_workspace_entitlement(self.ws)

        self.candidate = CandidateProfile.objects.create(
            user=self.user,
            full_name="Alex Morgan",
            email="awancandidate17@example.com"
        )
        self.cpv = CandidateProfileVersion.objects.create(
            profile=self.candidate,
            version_number=1,
            state="approved",
            full_name=self.candidate.full_name,
            fact_snapshot=[{"category": "Skills", "value": "Python"}]
        )
        self.candidate.active_version = self.cpv
        self.candidate.save()

        CandidateIdentityVerification.objects.create(
            candidate_profile=self.candidate,
            extracted_name=self.candidate.full_name,
            confirmed_name=self.candidate.full_name,
            verification_source="manual",
            status="verified",
            verified_at=timezone.now()
        )

        self.job = Job.objects.create(
            company="Ajaia | AI Consultancy",
            title="Full Stack Developer",
            job_url="https://www.linkedin.com/jobs/view/4440058610/",
            description="Experience building applied LLM systems, prompt engineering, and full stack web apps.",
            provenance="REAL_PROVIDER",
            provider="linkedin",
            external_job_id="4440058610",
            is_active=True
        )

    def test_extract_linkedin_job_id(self):
        """Test accurate numeric job ID extraction from multiple LinkedIn URL formats."""
        assert extract_linkedin_job_id("https://www.linkedin.com/jobs/view/4440058610/") == "4440058610"
        assert extract_linkedin_job_id("https://www.linkedin.com/jobs/search/?currentJobId=4440058610") == "4440058610"
        assert extract_linkedin_job_id("https://www.linkedin.com/jobs/view/987654321/?trackingId=abc") == "987654321"
        assert extract_linkedin_job_id("https://example.com/other") is None

    def test_false_positive_applied_text_in_job_description_not_applied(self):
        """CRITICAL: Description containing 'applied LLM systems' MUST NOT mark job as applied."""
        # Setup page where job description contains "applied LLM systems", but action container has an active Apply button
        dom = {
            ".jobs-s-apply button": MockLocator([
                MockElement(tag="button", attrs={"aria-label": "LinkedIn Apply to this job"}, text="Apply")
            ]),
            ".jobs-s-apply .artdeco-inline-feedback": MockLocator([])
        }
        mock_page = MockPlaywrightPage(url="https://www.linkedin.com/jobs/view/4440058610/", dom_map=dom)

        is_app, ev_type, meta = verify_linkedin_job_applied(
            session=None,
            external_job_id="4440058610",
            page=mock_page
        )
        assert is_app is False
        assert ev_type in ["not_applied_active_button_present", "not_applied"]

    def test_exact_applied_status_for_correct_job_id(self):
        """Signal 1: Scoped top-card Applied feedback badge without active Apply button confirms applied."""
        dom = {
            ".jobs-s-apply button": MockLocator([]),
            ".jobs-s-apply .artdeco-inline-feedback--success": MockLocator([
                MockElement(tag="div", attrs={"class": "artdeco-inline-feedback--success"}, text="Applied")
            ])
        }
        mock_page = MockPlaywrightPage(url="https://www.linkedin.com/jobs/view/4440058610/", dom_map=dom)

        is_app, ev_type, meta = verify_linkedin_job_applied(
            session=None,
            external_job_id="4440058610",
            page=mock_page
        )
        assert is_app is True
        assert ev_type == "linkedin_job_topcard_applied"
        assert meta["external_job_id"] == "4440058610"

    def test_applied_status_for_different_job_id_returns_false(self):
        """Signal 1 & 2: Verification for Job X fails if page or tracker has Job Y."""
        dom = {
            ".jobs-s-apply button": MockLocator([]),
            "a[href*=\"/jobs/view/4440058610\"]": MockLocator([])  # Not in tracker for 4440058610
        }
        # Page redirected to 9999999999
        mock_page = MockPlaywrightPage(url="https://www.linkedin.com/jobs/view/9999999999/", dom_map=dom)

        is_app, ev_type, meta = verify_linkedin_job_applied(
            session=None,
            external_job_id="4440058610",
            page=mock_page
        )
        assert is_app is False

    def test_applied_jobs_tracker_signal_2_confirmation(self):
        """Signal 2: Presence in LinkedIn Applied Jobs tracker confirms applied state."""
        dom = {
            ".jobs-s-apply button": MockLocator([]),
            "a[href*=\"/jobs/view/4440058610\"]": MockLocator([
                MockElement(tag="a", attrs={"href": "https://www.linkedin.com/jobs/view/4440058610/"}, text="Full Stack Developer")
            ])
        }
        mock_page = MockPlaywrightPage(url="https://www.linkedin.com/jobs/view/4440058610/", dom_map=dom)

        is_app, ev_type, meta = verify_linkedin_job_applied(
            session=None,
            external_job_id="4440058610",
            page=mock_page
        )
        assert is_app is True
        assert ev_type == "linkedin_applied_jobs_tracker"
        assert meta["external_job_id"] == "4440058610"

    def test_external_company_apply_link_not_easy_apply(self):
        """External ATS redirect is rejected by LinkedInEasyApplyAdapter."""
        dom = {
            ".jobs-s-apply button": MockLocator([
                MockElement(tag="button", attrs={"aria-label": "Apply to Full Stack Developer on company website"}, text="Apply")
            ])
        }
        mock_page = MockPlaywrightPage(url="https://www.linkedin.com/jobs/view/4440058610/", dom_map=dom)
        session = MagicMock()
        session.page = mock_page

        adapter = LinkedInEasyApplyAdapter(session=session)
        res = adapter.apply(job_url=self.job.job_url, candidate=self.candidate)
        assert res["success"] is False
        assert res["error"] == "not_easy_apply"

    def test_internal_adapter_success_without_external_evidence_sets_unverified(self):
        """Adapter succeeding without external proof results in submission_unverified and externally_confirmed=False."""
        class MockUnverifiedAdapter:
            def apply(self, **kwargs):
                return {"success": True, "externally_verified": False, "confirmation": "internal_only"}

        prep = prepare_job_application(workspace=self.ws, candidate=self.candidate, job=self.job)
        draft = ApplicationDraft.objects.get(id=prep["draft_id"])

        res = execute_job_application(
            workspace=self.ws,
            candidate=self.candidate,
            job=self.job,
            draft=draft,
            adapter=MockUnverifiedAdapter(),
            external_action_authorized=True
        )
        assert res["status"] == "submission_unverified"
        assert res["externally_verified"] is False
        app = JobApplication.objects.get(candidate_profile=self.candidate, job=self.job)
        assert app.status == "submission_unverified"
        assert app.externally_confirmed is False
