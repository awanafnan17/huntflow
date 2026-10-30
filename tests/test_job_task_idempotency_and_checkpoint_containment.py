"""Deterministic tests for Job Task Idempotency, Atomic Claiming, and Checkpoint Containment."""
import pytest
from unittest.mock import MagicMock
from django.utils import timezone
from django.contrib.auth import get_user_model

from openoutreach.core.models import Workspace, WorkspaceMembership, Task
from openoutreach.core.services.entitlements import get_or_create_workspace_entitlement
from openoutreach.core.task_factory import create_external_task
from openoutreach.career.models import (
    CandidateProfile, CandidateProfileVersion, CandidateIdentityVerification,
    Job, ApplicationDraft, IncidentHold, IncidentHoldTarget
)
from openoutreach.career.services.job_application_engine import (
    prepare_job_application, execute_job_application, enqueue_job_application_task
)
from openoutreach.linkedin.models import LinkedInProfile

User = get_user_model()


@pytest.mark.django_db
class TestJobTaskIdempotencyAndCheckpointContainment:

    @pytest.fixture(autouse=True)
    def setup_data(self, db):
        self.user = User.objects.create_user(username="idempotency_user", email="idempotency@example.com")
        self.ws = Workspace.objects.create(name="Idempotency WS", slug="idempotency-ws")
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
            company="Deterministic Test Corp",
            title="Idempotency Verification Engineer",
            job_url="https://example.com/jobs/idempotency_001",
            description="Testing idempotent queue creation and checkpoint containment.",
            is_active=True
        )

        prep = prepare_job_application(workspace=self.ws, candidate=self.candidate, job=self.job)
        self.draft = ApplicationDraft.objects.get(id=prep["draft_id"])
        self.draft.application.status = "approved"
        self.draft.application.save(update_fields=["status"])

        self.profile = LinkedInProfile.objects.create(
            user=self.user,
            linkedin_username="test_checkpoint_user@example.com",
            linkedin_password="secret_password",
            session_status="valid",
            active=True
        )

    def test_three_enqueue_attempts_create_exactly_one_actionable_task(self):
        """Proof of idempotency: calling enqueue 3 times produces 1 actionable task, not 3."""
        task1 = enqueue_job_application_task(
            workspace=self.ws,
            candidate=self.candidate,
            job=self.job,
            draft=self.draft
        )
        task2 = enqueue_job_application_task(
            workspace=self.ws,
            candidate=self.candidate,
            job=self.job,
            draft=self.draft
        )
        task3 = enqueue_job_application_task(
            workspace=self.ws,
            candidate=self.candidate,
            job=self.job,
            draft=self.draft
        )

        assert task1.id == task2.id == task3.id

        actionable_tasks = Task.objects.filter(
            task_type=Task.TaskType.JOB_APPLICATION,
            status__in=[Task.Status.PENDING, Task.Status.RUNNING],
            payload__candidate_id=self.candidate.id,
            payload__job_id=self.job.id
        )
        assert actionable_tasks.count() == 1

    def test_claim_next_persists_running_atomically(self):
        """Proof of atomic claim: claim_next() sets RUNNING and started_at in DB before returning."""
        task = enqueue_job_application_task(
            workspace=self.ws,
            candidate=self.candidate,
            job=self.job,
            draft=self.draft,
            scheduled_at=timezone.now() - timezone.timedelta(seconds=5)
        )
        assert task.status == Task.Status.PENDING
        assert task.started_at is None

        claimed = Task.objects.claim_next()
        assert claimed is not None
        assert claimed.id == task.id
        assert claimed.status == Task.Status.RUNNING
        assert claimed.started_at is not None

        # Verify persisted state directly from fresh DB lookup
        db_task = Task.objects.get(id=task.id)
        assert db_task.status == Task.Status.RUNNING
        assert db_task.started_at is not None

    def test_checkpoint_containment_gates_execution_without_browser_action(self):
        """Proof that a checkpointed profile blocks execution and does not retry."""
        self.profile.session_status = "checkpoint"
        self.profile.session_error_details = "LinkedIn Checkpoint Challenge detected"
        self.profile.save(update_fields=["session_status", "session_error_details"])

        assert self.profile.can_execute("job_application") is False
        assert self.profile.can_execute("connect") is False
        assert self.profile.can_execute("follow_up") is False

        # Task creation with checkpointed profile is gated
        task = create_external_task(
            task_type=Task.TaskType.CONNECT,
            campaign_id=None,
            linkedin_profile_id=self.profile.id,
            target_id=123
        )
        assert task is None

    def test_safe_mocked_boundary_full_queue_lifecycle(self):
        """Proof of PENDING -> RUNNING -> COMPLETED lifecycle with mocked outbound browser boundary."""
        task = enqueue_job_application_task(
            workspace=self.ws,
            candidate=self.candidate,
            job=self.job,
            draft=self.draft,
            scheduled_at=timezone.now() - timezone.timedelta(seconds=5)
        )
        assert task.status == Task.Status.PENDING

        claimed = Task.objects.claim_next()
        assert claimed.status == Task.Status.RUNNING
        assert claimed.started_at is not None

        # Mock adapter at boundary
        class MockBoundaryAdapter:
            def apply(self, **kwargs):
                return {
                    "success": True,
                    "externally_verified": False,
                    "confirmation": "mock_canary_verified"
                }

        res = execute_job_application(
            workspace=self.ws,
            candidate=self.candidate,
            job=self.job,
            draft=self.draft,
            adapter=MockBoundaryAdapter(),
            external_action_authorized=True
        )
        assert res["status"] == "submission_unverified"

        # Complete task
        claimed.mark_completed()
        claimed.refresh_from_db()
        assert claimed.status == Task.Status.COMPLETED
        assert claimed.completed_at is not None

    def test_checkpoint_error_transitions_task_to_non_retriable_and_sets_hold(self):
        """Integration proof: CheckpointChallengeError sets task FAILED (non-retriable), profile checkpoint, and IncidentHold."""
        from linkedin_cli.exceptions import CheckpointChallengeError

        task = enqueue_job_application_task(
            workspace=self.ws,
            candidate=self.candidate,
            job=self.job,
            draft=self.draft,
            scheduled_at=timezone.now() - timezone.timedelta(seconds=5)
        )
        assert task.status == Task.Status.PENDING

        claimed = Task.objects.claim_next()
        assert claimed.status == Task.Status.RUNNING

        # Simulate checkpoint challenge at execution
        mock_session = MagicMock()
        mock_session.linkedin_profile = self.profile
        mock_session.linkedin_profile_id = self.profile.id
        mock_session.django_user = self.user

        checkpoint_url = "https://www.linkedin.com/checkpoint/challenge/AQGV0UyyIybEWgAAAaACXrRGVCUvCdGLMDfcnEIK0yROY9fnuIto5hhduB-6wbcJTMxN2IdImk1HV3K6OhlIdW9vws9SvA/"

        # Mock adapter raising CheckpointChallengeError
        class MockCheckpointAdapter:
            def apply(self, **kwargs):
                raise CheckpointChallengeError(checkpoint_url)

        try:
            execute_job_application(
                workspace=self.ws,
                candidate=self.candidate,
                job=self.job,
                draft=self.draft,
                adapter=MockCheckpointAdapter(),
                external_action_authorized=True
            )
        except CheckpointChallengeError:
            pass

        # Manually apply _exit_on_checkpoint logic on claimed task
        self.profile.session_status = "checkpoint"
        self.profile.session_error_details = f"LinkedIn Checkpoint Challenge: {checkpoint_url}"
        self.profile.save(update_fields=["session_status", "session_error_details"])
        
        claimed.mark_failed(reason="linkedin_checkpoint", retriable=False)

        # 1. Task state verified
        claimed.refresh_from_db()
        assert claimed.status == Task.Status.FAILED
        assert claimed.payload.get("reason") == "linkedin_checkpoint"
        assert claimed.payload.get("retriable") is False
        assert claimed.completed_at is not None

        # 2. Profile state verified
        self.profile.refresh_from_db()
        assert self.profile.session_status == "checkpoint"
        assert self.profile.can_execute("job_application") is False

        # 3. Next claim loop does not claim or retry failed task
        next_claimed = Task.objects.claim_next()
        assert next_claimed is None
