"""Tests for Job Application Transport Boundary, Truth Verification, and Task Dispatch."""
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
from openoutreach.core.daemon import handle_job_application_task

User = get_user_model()


@pytest.mark.django_db
class TestJobApplicationTransportBoundary:

    @pytest.fixture(autouse=True)
    def setup_data(self, db):
        self.user = User.objects.create_user(username="cand_transport", email="cand_transport@example.com")
        self.ws = Workspace.objects.create(name="Transport WS", slug="transport-ws")
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
            company="Verified Tech",
            title="Python Backend Engineer",
            job_url="https://www.linkedin.com/jobs/view/9999901/",
            is_active=True
        )

    def test_01_no_adapter_fails_and_does_not_verify_externally(self):
        """Without a browser adapter, execute_job_application must fail and not set externally_confirmed."""
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
        assert res.get("externally_verified", False) is False
        app = JobApplication.objects.get(candidate_profile=self.candidate, job=self.job)
        assert app.status == "failed"
        assert app.externally_confirmed is False

    def test_02_adapter_without_external_verification_marks_unverified(self):
        """Adapter that succeeds internally but without verified external evidence marks submission_unverified."""
        class MockInternalAdapter:
            def apply(self, **kwargs):
                return {"success": True, "externally_verified": False, "confirmation": "internal_only"}

        prep = prepare_job_application(workspace=self.ws, candidate=self.candidate, job=self.job)
        draft = ApplicationDraft.objects.get(id=prep["draft_id"])

        res = execute_job_application(
            workspace=self.ws,
            candidate=self.candidate,
            job=self.job,
            draft=draft,
            adapter=MockInternalAdapter(),
            external_action_authorized=True
        )
        assert res["status"] == "submission_unverified"
        assert res["externally_verified"] is False
        app = JobApplication.objects.get(candidate_profile=self.candidate, job=self.job)
        assert app.status == "submission_unverified"
        assert app.externally_confirmed is False

    def test_03_adapter_with_external_evidence_marks_submitted_and_confirmed(self):
        """Adapter that confirms LinkedIn UI Applied state sets submitted and externally_confirmed=True."""
        class MockVerifiedAdapter:
            def apply(self, **kwargs):
                return {
                    "success": True,
                    "externally_verified": True,
                    "confirmation_type": "linkedin_ui_applied",
                    "evidence": "Applied"
                }

        prep = prepare_job_application(workspace=self.ws, candidate=self.candidate, job=self.job)
        draft = ApplicationDraft.objects.get(id=prep["draft_id"])

        res = execute_job_application(
            workspace=self.ws,
            candidate=self.candidate,
            job=self.job,
            draft=draft,
            adapter=MockVerifiedAdapter(),
            external_action_authorized=True
        )
        assert res["status"] == "submitted"
        assert res["externally_verified"] is True
        app = JobApplication.objects.get(candidate_profile=self.candidate, job=self.job)
        assert app.status == "submitted"
        assert app.externally_confirmed is True
        assert app.submission_date is not None

    def test_04_duplicate_application_blocked_when_already_submitted(self):
        """Attempting to prepare or execute for an already submitted job is blocked."""
        app = JobApplication.objects.create(
            candidate_profile=self.candidate,
            job=self.job,
            status="submitted",
            externally_confirmed=True
        )
        prep = prepare_job_application(workspace=self.ws, candidate=self.candidate, job=self.job)
        assert prep["prepared"] is False
        assert prep["reason"] == "duplicate_application_blocked"

    def test_05_full_daemon_dispatcher_job_application_task_flow(self, monkeypatch):
        """Test complete dispatcher task execution with mocked browser adapter at final boundary."""
        prep = prepare_job_application(workspace=self.ws, candidate=self.candidate, job=self.job)
        draft = ApplicationDraft.objects.get(id=prep["draft_id"])

        task = Task.objects.create(
            task_type=Task.TaskType.JOB_APPLICATION,
            status=Task.Status.RUNNING,
            payload={
                "candidate_id": self.candidate.pk,
                "job_id": self.job.pk,
                "draft_id": draft.pk,
                "external_action_authorized": True
            },
            scheduled_at=timezone.now()
        )

        class MockBrowserAdapter:
            def __init__(self, session=None):
                pass
            def apply(self, **kwargs):
                return {
                    "success": True,
                    "externally_verified": True,
                    "confirmation_type": "linkedin_ui_applied",
                    "evidence": "Applied"
                }

        monkeypatch.setattr(
            "openoutreach.linkedin.browser.easy_apply.LinkedInEasyApplyAdapter",
            MockBrowserAdapter
        )

        session = MagicMock()
        session.django_user = self.user
        handle_job_application_task(task=task, session=session, qualifiers={})

        task.refresh_from_db()
        assert task.status == Task.Status.COMPLETED
        app = JobApplication.objects.get(candidate_profile=self.candidate, job=self.job)
        assert app.status == "submitted"
        assert app.externally_confirmed is True
