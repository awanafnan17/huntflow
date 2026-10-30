"""Tests for Application Package Durability, Submitted Freeze, and Secure Downloads."""
import os
import pytest
from unittest.mock import MagicMock, patch
from django.utils import timezone
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse
from django.core.exceptions import PermissionDenied

from openoutreach.core.models import Workspace, WorkspaceMembership
from openoutreach.career.models import (
    CandidateProfile, CandidateProfileVersion, CandidateFact, Job, JobApplication, ApplicationDraft
)
from openoutreach.career.services.job_application_engine import (
    prepare_job_application,
)

User = get_user_model()


@pytest.mark.django_db
class TestDurableApplicationPackages:

    @pytest.fixture(autouse=True)
    def setup_data(self, db):
        self.user_a = User.objects.create_user(username="user_a_durable", password="password123", email="user_a@example.com")
        self.ws_a = Workspace.objects.create(name="Workspace A", slug="ws-durable-a")
        WorkspaceMembership.objects.create(workspace=self.ws_a, user=self.user_a, role="owner", status="active")

        self.candidate_a = CandidateProfile.objects.create(
            user=self.user_a,
            full_name="Alex Morgan",
            email="awancandidate17@example.com",
            phone="+1-202-555-0101"
        )
        self.cpv_a = CandidateProfileVersion.objects.create(
            profile=self.candidate_a,
            version_number=1,
            state="approved",
            full_name=self.candidate_a.full_name,
            summary="Python Django Engineer",
            fact_snapshot=[{"category": "Skills", "value": "Python"}, {"category": "Skills", "value": "Django"}]
        )
        self.candidate_a.active_version = self.cpv_a
        self.candidate_a.save()
        CandidateFact.objects.create(profile_version=self.cpv_a, fact_type="skill", category="Skills", value="Python", verified=True)
        CandidateFact.objects.create(profile_version=self.cpv_a, fact_type="skill", category="Skills", value="Django", verified=True)

        # User B (different workspace)
        self.user_b = User.objects.create_user(username="user_b_intruder", password="password123", email="user_b@example.com")
        self.ws_b = Workspace.objects.create(name="Workspace B", slug="ws-durable-b")
        WorkspaceMembership.objects.create(workspace=self.ws_b, user=self.user_b, role="owner", status="active")

        self.job = Job.objects.create(
            company="Veloce AI",
            title="Senior Python Engineer",
            description="Looking for Senior Python and Django developers.",
            is_active=True
        )

    def test_persistence_audit_all_13_items_stored_durably(self):
        """Audit that all 13 required artifact items are persisted in ApplicationDraft.structured_cv."""
        prep_res = prepare_job_application(workspace=self.ws_a, candidate=self.candidate_a, job=self.job)
        assert prep_res["prepared"] is True

        app = JobApplication.objects.get(candidate_profile=self.candidate_a, job=self.job)
        draft = app.drafts.first()
        assert draft is not None
        s_cv = draft.structured_cv

        # Verify all 13 items
        assert "jd_snapshot" in s_cv
        assert "content_hash" in s_cv["jd_snapshot"]
        assert "match_analysis" in s_cv
        assert "keyword_mapping" in s_cv
        assert len(draft.tailored_cv) > 50
        assert "resume_docx_path" in s_cv and os.path.exists(s_cv["resume_docx_path"])
        assert "resume_pdf_path" in s_cv and os.path.exists(s_cv["resume_pdf_path"])
        assert "resume_hash" in s_cv and len(s_cv["resume_hash"]) == 64
        assert len(draft.cover_letter) > 50
        assert "cover_letter_docx_path" in s_cv and os.path.exists(s_cv["cover_letter_docx_path"])
        assert "cover_letter_pdf_path" in s_cv and os.path.exists(s_cv["cover_letter_pdf_path"])
        assert "cover_letter_hash" in s_cv and len(s_cv["cover_letter_hash"]) == 64
        assert "answers" in s_cv
        assert "document_qa_result" in s_cv
        assert s_cv["document_qa_result"]["passed"] is True

    def test_submitted_version_freeze_and_regeneration_creates_new_version(self):
        """Submitted applications remain frozen and immutable; subsequent regeneration creates draft v2."""
        prep_res = prepare_job_application(workspace=self.ws_a, candidate=self.candidate_a, job=self.job)
        app = JobApplication.objects.get(candidate_profile=self.candidate_a, job=self.job)
        draft_v1 = app.drafts.get(version_number=1)
        original_resume_hash = draft_v1.structured_cv["resume_hash"]
        original_cl_hash = draft_v1.structured_cv["cover_letter_hash"]

        # Simulate provider submission
        app.status = "submitted"
        app.externally_confirmed = True
        app.submission_date = timezone.now()
        app.save()

        # Attempt to prepare again (regeneration)
        prep_res_2 = prepare_job_application(
            workspace=self.ws_a,
            candidate=self.candidate_a,
            job=self.job,
            allow_regeneration=True
        )

        # Refresh v1 from DB and verify it is 100% untouched
        draft_v1.refresh_from_db()
        assert draft_v1.version_number == 1
        assert draft_v1.structured_cv["resume_hash"] == original_resume_hash
        assert draft_v1.structured_cv["cover_letter_hash"] == original_cl_hash

        # Verify draft v2 was created
        draft_v2 = app.drafts.filter(version_number=2).first()
        assert draft_v2 is not None
        assert draft_v2.version_number == 2

    def test_download_security_blocks_cross_user_and_cross_workspace_access(self):
        """User B (intruder) cannot download User A's tailored DOCX, PDF, or Cover Letters."""
        prepare_job_application(workspace=self.ws_a, candidate=self.candidate_a, job=self.job)
        app = JobApplication.objects.get(candidate_profile=self.candidate_a, job=self.job)

        client_b = Client()
        client_b.force_login(self.user_b)

        # 1. DOCX Download
        url_docx = reverse("career:download_docx", kwargs={"app_pk": app.pk})
        resp_docx = client_b.get(url_docx)
        assert resp_docx.status_code == 403

        # 2. PDF Download
        url_pdf = reverse("career:download_pdf", kwargs={"app_pk": app.pk})
        resp_pdf = client_b.get(url_pdf)
        assert resp_pdf.status_code == 403

        # 3. Cover Letter DOCX Download
        url_cl_docx = reverse("career:download_cover_docx", kwargs={"app_pk": app.pk})
        resp_cl_docx = client_b.get(url_cl_docx)
        assert resp_cl_docx.status_code == 403

        # 4. Cover Letter PDF Download
        url_cl_pdf = reverse("career:download_cover_pdf", kwargs={"app_pk": app.pk})
        resp_cl_pdf = client_b.get(url_cl_pdf)
        assert resp_cl_pdf.status_code == 403

        # 5. Owner (User A) gets 200 attachment
        client_a = Client()
        client_a.force_login(self.user_a)
        resp_a_docx = client_a.get(url_docx)
        assert resp_a_docx.status_code == 200
        assert "attachment" in resp_a_docx["Content-Disposition"]
        assert len(resp_a_docx.content) > 100
