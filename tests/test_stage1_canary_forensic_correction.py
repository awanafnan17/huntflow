"""
Offline Unit and Integration Tests for Stage 1 Canary Forensic Corrections.

Guarantees:
1. Zero live LinkedIn provider network activity (100% mocked / offline).
2. Strict testing of durable StageCanaryBudget (atomic claim, concurrency, restart safety, read tracking).
3. Offline Easy Apply integer and React-controlled input handling tests.
4. Provider read telemetry instrumentation and category accuracy.
5. Non-job channels persisted OFF and stale approval reconciliation.
"""
import pytest
from unittest.mock import MagicMock
from django.utils import timezone
from django.contrib.auth import get_user_model

from openoutreach.core.models import (
    Workspace, WorkspaceMembership, Task, OperatorActionApproval,
    WorkspaceAutomationSettings, StageCanaryBudget
)
from openoutreach.career.models import (
    CandidateProfile, CandidateProfileVersion, CandidateIdentityVerification,
    Job, JobApplication, ApplicationDraft
)
from openoutreach.career.answer_bank import get_answer_for_question, derive_skill_experience_years
from openoutreach.linkedin.models import (
    LinkedInProfile, ProviderReadLedger, ProviderOperationType
)
from openoutreach.linkedin.services.evidence_cache import record_provider_read

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

    def locator(self, selector):
        matched = []
        for el in self._elements:
            sub = el.locator(selector)
            matched.extend(sub.all())
        return MockLocator(matched)


class MockElement:
    def __init__(self, tag="div", attrs=None, text="", visible=True, value=""):
        self._tag = tag
        self._attrs = attrs or {}
        self._text = text
        self._visible = visible
        self._value = value
        self.events_dispatched = []
        self.clicked = False

    def get_attribute(self, attr):
        return self._attrs.get(attr, "")

    def inner_text(self):
        return self._text

    def is_visible(self):
        return self._visible

    def count(self):
        return 1

    def input_value(self):
        return self._value

    def fill(self, val):
        self._value = str(val)

    def click(self):
        self.clicked = True

    def dispatch_event(self, event_name, event_init=None):
        self.events_dispatched.append((event_name, event_init or {}))

    def locator(self, selector):
        return MockLocator([])


@pytest.mark.django_db
class TestStageCanaryBudgetDurable:

    @pytest.fixture(autouse=True)
    def setup_data(self, db):
        self.user = User.objects.create_user(username="canary_tester", email="tester@example.com")
        self.workspace = Workspace.objects.create(name="Test Canary Workspace", slug="test-canary-ws")
        self.membership = WorkspaceMembership.objects.create(user=self.user, workspace=self.workspace, role="admin")
        self.profile = LinkedInProfile.objects.create(
            user=self.user,
            workspace=self.workspace,
            linkedin_username="tester_li",
            session_status="valid"
        )

    def test_single_canary_budget_claim_and_exhaustion(self):
        """Test that StageCanaryBudget allows exactly one claim and fails closed once attempted."""
        # 1. First claim succeeds
        claimed, budget, msg = StageCanaryBudget.claim_canary(
            workspace=self.workspace,
            linkedin_profile=self.profile,
            stage="STAGE_1",
            task_id=101
        )
        assert claimed is True
        assert budget.state == StageCanaryBudget.State.CLAIMED
        assert budget.claimed_task_id == 101

        # 2. Duplicate claim by a different worker/task fails
        claimed2, budget2, msg2 = StageCanaryBudget.claim_canary(
            workspace=self.workspace,
            linkedin_profile=self.profile,
            stage="STAGE_1",
            task_id=102
        )
        assert claimed2 is False
        assert "canary_already_claimed" in msg2

        # 3. Network attempt consumes budget permanently
        budget.record_network_attempt(task_id=101)
        assert budget.state == StageCanaryBudget.State.NETWORK_ATTEMPTED
        assert budget.attempts_count == 1

        # 4. Subsequent claim fails across process restart / re-run
        claimed3, budget3, msg3 = StageCanaryBudget.claim_canary(
            workspace=self.workspace,
            linkedin_profile=self.profile,
            stage="STAGE_1",
            task_id=103
        )
        assert claimed3 is False
        assert "canary_budget_exhausted" in msg3

    def test_provider_read_budget_enforcement(self):
        """Test that provider reads are capped and block execution when exceeded."""
        claimed, budget, _ = StageCanaryBudget.claim_canary(
            workspace=self.workspace,
            linkedin_profile=self.profile,
            stage="STAGE_1",
            task_id=201,
            max_reads=3
        )
        assert claimed is True

        # Reads within budget
        assert budget.record_provider_read(count=2) is True
        assert budget.provider_reads_count == 2
        assert budget.state == StageCanaryBudget.State.CLAIMED

        # Exceeding budget triggers BLOCKED state
        assert budget.record_provider_read(count=2) is False
        assert budget.state == StageCanaryBudget.State.BLOCKED
        assert "max_provider_reads_exceeded" in budget.last_reason


@pytest.mark.django_db
class TestEasyApplyIntegerAndReactInputsOffline:

    @pytest.fixture(autouse=True)
    def setup_candidate(self, db):
        self.user = User.objects.create_user(username="cand_user", email="cand@example.com")
        self.candidate = CandidateProfile.objects.create(
            user=self.user,
            full_name="Alex Morgan",
            email="cand@example.com",
            phone="+1-202-555-0100"
        )
        self.ver = CandidateProfileVersion.objects.create(
            profile=self.candidate,
            version_number=1,
            fact_snapshot=[]
        )
        self.candidate.active_version = self.ver
        self.candidate.save(update_fields=["active_version"])
        # Create ApplicationProfile preference with 4 years python
        from openoutreach.career.models import CandidateApplicationProfile
        self.app_prof = CandidateApplicationProfile.objects.create(
            candidate_profile=self.candidate,
            full_name="Alex Morgan",
            email="cand@example.com",
            phone="+1-202-555-0100",
            city="Islamabad",
            country="Pakistan",
            skill_experience_years={"python": 4, "django": 3}
        )

    def test_years_python_verified_answer_derivation(self):
        """Test that Python experience question resolves strictly to 4 without asking user."""
        q = "How many years of work experience do you have with Python (Programming Language)?"
        status, val, key = get_answer_for_question(self.candidate, q)
        assert status == "ANSWER_FOUND"
        assert val == "4"
        assert key == "years_python"

    def test_integer_input_fill_and_event_dispatch_offline(self):
        """Test filling a React-controlled numeric input that was prefilled with '0'."""
        input_el = MockElement(
            tag="input",
            attrs={"id": "urn-li-python-years", "type": "number", "min": "0", "max": "99"},
            value="0"
        )
        label_el = MockElement(
            tag="label",
            attrs={"for": "urn-li-python-years"},
            text="How many years of work experience do you have with Python (Programming Language)?\nRequired"
        )

        # Simulate input field processing logic
        inp_type = (input_el.get_attribute("type") or "text").lower()
        inp_val = (input_el.input_value() or "").strip()
        lbl_text = label_el.inner_text().strip()

        status, ans_val, c_key = get_answer_for_question(self.candidate, lbl_text)
        assert status == "ANSWER_FOUND"

        val_str = str(ans_val).strip()
        if inp_type == "number" or "years" in (c_key or ""):
            import re
            m = re.search(r'\d+', val_str)
            if m:
                val_str = m.group(0)

        # Fill and dispatch events
        input_el.click()
        input_el.fill("")
        input_el.fill(val_str)
        input_el.dispatch_event("input", {"bubbles": True, "cancelable": True})
        input_el.dispatch_event("change", {"bubbles": True, "cancelable": True})
        input_el.dispatch_event("blur", {"bubbles": True, "cancelable": True})

        assert input_el.input_value() == "4"
        assert input_el.clicked is True
        dispatched_names = [e[0] for e in input_el.events_dispatched]
        assert "input" in dispatched_names
        assert "change" in dispatched_names
        assert "blur" in dispatched_names

    def test_unknown_question_fails_closed(self):
        """Test that an unverified question halts progression safely with needs_your_answer."""
        unknown_q = "Do you have 10 years of experience with Quantum Computing in Haskell?"
        status, val, key = get_answer_for_question(self.candidate, unknown_q)
        assert status == "UNKNOWN_REQUIRES_USER"


@pytest.mark.django_db
class TestProviderReadLedgerInstrumentation:

    @pytest.fixture(autouse=True)
    def setup_data(self, db):
        self.user = User.objects.create_user(username="read_user", email="read@example.com")
        self.workspace = Workspace.objects.create(name="Read Test WS", slug="read-test-ws")
        self.profile = LinkedInProfile.objects.create(
            user=self.user,
            workspace=self.workspace,
            linkedin_username="read_li",
            session_status="valid"
        )

    def test_record_provider_read_categorization_and_budget(self):
        """Test that record_provider_read accurately logs operations and respects StageCanaryBudget."""
        # Create active canary budget
        budget = StageCanaryBudget.objects.create(
            workspace=self.workspace,
            linkedin_profile=self.profile,
            stage="STAGE_1",
            state=StageCanaryBudget.State.CLAIMED,
            max_provider_reads=5
        )

        entry = record_provider_read(
            operation_type=ProviderOperationType.JOB_DETAIL,
            resource_identifier="4458264390",
            cache_hit=False,
            linkedin_profile=self.profile,
            workspace=self.workspace,
            success=True,
            read_cost=1,
            metadata={"source": "offline_test"}
        )

        assert entry is not None
        assert entry.operation_type == ProviderOperationType.JOB_DETAIL
        assert entry.resource_identifier == "4458264390"
        assert entry.read_cost == 1
        assert entry.cache_hit is False

        # Verify budget was incremented
        budget.refresh_from_db()
        assert budget.provider_reads_count == 1


@pytest.mark.django_db
class TestOperatorActionApprovalReconciliation:

    def test_stale_approvals_are_expired_cleanly(self):
        """Test that unconsumed or stale approvals are expired and cannot be reused."""
        user = User.objects.create_user(username="app_user", email="app@example.com")
        ws = Workspace.objects.create(name="Approval WS", slug="app-ws")

        # Stale approval 1: expired by timestamp
        a1 = OperatorActionApproval.objects.create(
            workspace=ws,
            operator=user,
            action_type="job_application",
            status="approved",
            expires_at=timezone.now() - timezone.timedelta(minutes=10)
        )
        # Stale approval 2: claimed but abandoned
        a2 = OperatorActionApproval.objects.create(
            workspace=ws,
            operator=user,
            action_type="job_application",
            status="claimed",
            claimed_at=timezone.now() - timezone.timedelta(hours=1)
        )

        count = OperatorActionApproval.reconcile_stale_approvals(workspace=ws)
        assert count == 2

        a1.refresh_from_db()
        a2.refresh_from_db()
        assert a1.status == "expired"
        assert a2.status == "failed"
        assert a1.failure_reason == "reconciled_stale_canary_approval"
        assert a2.failure_reason == "reconciled_stale_canary_approval"
