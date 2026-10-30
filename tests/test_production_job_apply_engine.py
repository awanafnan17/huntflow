"""Production Job Apply Engine & Safety Guard Tests."""
import pytest
from unittest.mock import MagicMock
from django.utils import timezone
from django.contrib.auth import get_user_model

from openoutreach.core.models import Workspace, WorkspaceMembership, Task
from openoutreach.core.services.entitlements import get_or_create_workspace_entitlement
from openoutreach.career.models import (
    CandidateProfile, CandidateProfileVersion, CandidateIdentityVerification, CandidateFact,
    Job, JobApplication, ApplicationDraft
)
from openoutreach.career.services.job_application_engine import (
    prepare_job_application, execute_job_application, enqueue_job_application_task
)
from openoutreach.linkedin.browser.easy_apply import (
    extract_linkedin_job_id, verify_linkedin_job_applied, LinkedInEasyApplyAdapter
)
from openoutreach.linkedin.models import LinkedInProfile
from openoutreach.linkedin.transport_guard import is_qa_synthetic_transport_allowed

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
    def __init__(self, url="https://www.linkedin.com/jobs/view/5555555555/", dom_map=None):
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
class TestProductionJobApplyEngine:

    @pytest.fixture(autouse=True)
    def setup_data(self, db):
        self.user = User.objects.create_user(username="prod_app_user", email="prod_app@example.com")
        self.ws = Workspace.objects.create(name="Production WS", slug="prod-ws")
        WorkspaceMembership.objects.create(workspace=self.ws, user=self.user, role="owner", status="active")
        get_or_create_workspace_entitlement(self.ws)

        self.candidate = CandidateProfile.objects.create(
            user=self.user,
            full_name="Alex Morgan",
            email="awancandidate17@example.com",
            phone="+1-202-555-0101"
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
            company="Tech Corp Inc",
            title="Senior Python Engineer",
            job_url="https://www.linkedin.com/jobs/view/5555555555/",
            description="We build distributed web services and applied data applications.",
            is_active=True
        )
        self.job.screening_questions = [
            {"id": "q1", "text": "What is your target salary?"},
            {"id": "q2", "text": "Are you willing to relocate?"}
        ]

    def test_real_job_with_no_adapter_fails_and_does_not_claim_success(self):
        """1. Real job application execution without a browser adapter fails and does not claim success."""
        prep = prepare_job_application(workspace=self.ws, candidate=self.candidate, job=self.job)
        draft = ApplicationDraft.objects.get(id=prep["draft_id"])

        res = execute_job_application(
            workspace=self.ws,
            candidate=self.candidate,
            job=self.job,
            draft=draft,
            adapter=None,
            external_action_authorized=True
        )
        assert res["status"] == "failed"
        assert "no_browser_adapter" in res["reason"]
        app = JobApplication.objects.get(candidate_profile=self.candidate, job=self.job)
        assert app.status == "failed"
        assert app.externally_confirmed is False

    def test_real_application_without_external_confirmation_does_not_set_externally_confirmed(self):
        """2. Application without job-specific external confirmation sets unverified and externally_confirmed=False."""
        class MockUnverifiedAdapter:
            def apply(self, **kwargs):
                return {"success": True, "externally_verified": False, "confirmation": "unverified"}

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

    def test_job_specific_applied_verification_passes_only_for_correct_job(self):
        """3. Job-specific applied verification passes for target job ID 5555555555 and fails for mismatch."""
        dom = {
            ".jobs-s-apply button": MockLocator([]),
            ".jobs-s-apply .artdeco-inline-feedback--success": MockLocator([
                MockElement(tag="div", attrs={"class": "artdeco-inline-feedback--success"}, text="Applied")
            ])
        }
        mock_page = MockPlaywrightPage(url="https://www.linkedin.com/jobs/view/5555555555/", dom_map=dom)

        # Match job ID
        is_app, ev_type, meta = verify_linkedin_job_applied(session=None, external_job_id="5555555555", page=mock_page)
        assert is_app is True
        assert meta["external_job_id"] == "5555555555"

        # Mismatched job redirect
        mock_page_mismatch = MockPlaywrightPage(url="https://www.linkedin.com/jobs/view/9999999999/", dom_map={
            ".jobs-s-apply button": MockLocator([MockElement(tag="button", text="Apply")])
        })
        is_app_diff, _, _ = verify_linkedin_job_applied(session=None, external_job_id="5555555555", page=mock_page_mismatch)
        assert is_app_diff is False

    def test_job_description_containing_word_applied_does_not_confirm_application(self):
        """4. Job description containing 'applied data applications' with active Apply button returns False."""
        dom = {
            ".jobs-s-apply button": MockLocator([
                MockElement(tag="button", attrs={"aria-label": "Apply to Senior Python Engineer"}, text="Easy Apply")
            ]),
            ".jobs-s-apply .artdeco-inline-feedback": MockLocator([])
        }
        mock_page = MockPlaywrightPage(url="https://www.linkedin.com/jobs/view/5555555555/", dom_map=dom)

        is_app, ev_type, _ = verify_linkedin_job_applied(session=None, external_job_id="5555555555", page=mock_page)
        assert is_app is False
        assert ev_type in ["not_applied_active_button_present", "not_applied"]

    def test_duplicate_application_tasks_prevented(self):
        """5. Multiple enqueue calls create exactly 1 actionable Task with deterministic idempotency key."""
        prep = prepare_job_application(workspace=self.ws, candidate=self.candidate, job=self.job)
        draft = ApplicationDraft.objects.get(id=prep["draft_id"])

        t1 = enqueue_job_application_task(workspace=self.ws, candidate=self.candidate, job=self.job, draft=draft)
        t2 = enqueue_job_application_task(workspace=self.ws, candidate=self.candidate, job=self.job, draft=draft)
        assert t1.id == t2.id
        expected_key = f"job_application:{self.candidate.id}:{self.job.id}"
        assert t1.payload.get("idempotency_key") == expected_key or getattr(t1, "idempotency_key", None) == expected_key

    def test_unknown_required_question_stops_and_flags_needs_answer(self):
        """6. Unknown required question blocks auto-preparation and returns unknown_screening_question."""
        job_unknown = Job.objects.create(
            company="Custom Corp",
            title="Custom Specialist",
            job_url="https://www.linkedin.com/jobs/view/7777777777/",
            description="Specialized role.",
            is_active=True
        )
        job_unknown.screening_questions = [
            {"id": "q_unknown", "text": "What is your custom_unhandled clearance code?"}
        ]
        prep = prepare_job_application(workspace=self.ws, candidate=self.candidate, job=job_unknown)
        assert prep["prepared"] is False
        assert prep["reason"] == "unknown_screening_question"

    def test_known_factual_questions_answered_from_approved_candidate(self):
        """7. Known factual questions (salary, relocation) are auto-filled from approved profile."""
        prep = prepare_job_application(workspace=self.ws, candidate=self.candidate, job=self.job)
        assert prep["prepared"] is True
        answers = prep["answers"]
        assert "q1" in answers
        assert "q2" in answers
        assert answers["q2"] in ["Yes", "No", True, False]

    def test_checkpoint_containment_blocks_execution_safely(self):
        """8. Checkpointed profile blocks execution without attempting browser actions."""
        profile = LinkedInProfile.objects.create(
            user=self.user,
            linkedin_username="chk_user@example.com",
            session_status="checkpoint",
            active=True
        )
        assert profile.can_execute("job_application") is False
        assert profile.can_execute("connect") is False

    def test_real_tenant_never_receives_qa_synthetic_transport(self, monkeypatch):
        """9. Real workspace/job never receives QA synthetic transport even if flag is toggled."""
        monkeypatch.setenv("HUNTFLOW_QA_SYNTHETIC_TRANSPORT", "1")
        monkeypatch.setenv("DJANGO_ENV", "development")

        task = Task.objects.create(
            task_type=Task.TaskType.JOB_APPLICATION,
            scheduled_at=timezone.now(),
            payload={"job_id": self.job.id, "candidate_id": self.candidate.id}
        )

        allowed = is_qa_synthetic_transport_allowed(
            workspace=self.ws,
            task=task
        )
        assert allowed is False, "CRITICAL: Real tenant job was allowed to use QA synthetic transport!"
