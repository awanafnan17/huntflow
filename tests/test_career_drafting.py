import pytest
import os
from unittest.mock import patch, MagicMock
from django.urls import reverse
from django.core.exceptions import ValidationError, PermissionDenied
from django.contrib.auth.models import User, Permission
from openoutreach.career.models import (
    CandidateProfile,
    CandidateProfileVersion,
    CandidateFact,
    Job,
    JobApplication,
    ApplicationDraft,
    ApprovalItem,
    ApprovalHistory,
    ApplicationApproval,
    CandidateAuditEvent
)
from openoutreach.career.drafting import get_drafting_provider
from openoutreach.career.approval_service import ApprovalIntegrityService
from openoutreach.career.storage import PrivateFileSystemStorage
from tests.factories import UserFactory

@pytest.mark.django_db
class TestCareerDrafting:

    @pytest.fixture(autouse=True)
    def _setup(self, client, db):
        from openoutreach.core.models import SiteConfig
        cfg = SiteConfig.load()
        cfg.llm_api_key = "dummy-key"
        cfg.ai_model = "openai:gpt-4o"
        cfg.save()

        # Regular seeker user (non-operator)
        self.user = UserFactory(username="seeker_draft", email="seeker@draft.com")
        self.profile = CandidateProfile.objects.create(
            user=self.user,
            full_name="Alice Candidate",
            email="seeker@draft.com",
            summary="Django Expert"
        )
        self.profile_version = CandidateProfileVersion.objects.create(
            profile=self.profile,
            version_number=1,
            full_name=self.profile.full_name,
            email=self.profile.email,
            summary=self.profile.summary,
            created_by=self.user
        )
        
        # Add a candidate fact for traceability checking
        self.fact = CandidateFact.objects.create(
            profile_version=self.profile_version,
            fact_type="experience",
            category="Stark Industries",
            value={"company": "Stark Industries", "title": "Developer", "start": "2020", "bullet_points": ["Python code"]}
        )
        
        self.job = Job.objects.create(
            title="Senior Django Developer",
            company="Oscorp",
            description="Looking for an expert Django developer with Python experience.",
            source="manual"
        )
        
        # Operator user with approve_profile permission
        self.operator = UserFactory(username="operator_user", email="operator@draft.com")
        self.operator.user_permissions.add(
            Permission.objects.get(codename="approve_profile")
        )
        
        self.client = client

    def test_offline_drafting_provider(self):
        provider = get_drafting_provider(offline=True)
        cv, letter, struct_cv, trace_map = provider.generate_draft(self.profile_version, self.job)
        
        assert "Alice Candidate" in cv
        assert "Stark Industries" in cv
        assert "Developer" in cv
        assert "Dear Hiring Team at Oscorp" in letter
        assert struct_cv["summary"] == "Django Expert"
        assert len(trace_map["experience_mappings"]) == 1
        assert trace_map["experience_mappings"][0]["fact_id"] == self.fact.id

    def test_application_draft_immutability(self):
        app = JobApplication.objects.create(candidate_profile=self.profile, job=self.job)
        draft = ApplicationDraft.objects.create(
            application=app,
            version_number=1,
            tailored_cv="original cv",
            cover_letter="original letter"
        )
        
        # Test editing an existing draft triggers ValidationError
        draft.tailored_cv = "modified cv"
        with pytest.raises(ValidationError):
            draft.save()

    def test_approval_history_immutability(self):
        app = JobApplication.objects.create(candidate_profile=self.profile, job=self.job)
        draft = ApplicationDraft.objects.create(
            application=app,
            version_number=1,
            tailored_cv="original cv",
            cover_letter="original letter"
        )
        item = ApprovalItem.objects.create(application=app, draft=draft)
        history = ApprovalHistory.objects.create(
            item=item,
            actor=self.operator,
            action="approve",
            note="looks good"
        )
        
        # Editing history entry must fail
        history.note = "hacked note"
        with pytest.raises(ValidationError):
            history.save()

    def test_approval_integrity_service_fact_deletion_validation(self):
        app = JobApplication.objects.create(candidate_profile=self.profile, job=self.job)
        
        # Create draft with traceability mappings referencing our fact
        trace_map = {
            "experience_mappings": [{"company": "Stark Industries", "title": "Developer", "fact_id": self.fact.id}]
        }
        draft = ApplicationDraft.objects.create(
            application=app,
            version_number=1,
            tailored_cv="cv text",
            cover_letter="letter text",
            traceability_map=trace_map
        )
        item = ApprovalItem.objects.create(application=app, draft=draft)
        
        # Approve the item
        ApprovalIntegrityService.approve_item(item, self.operator, "initial approval")
        assert item.status == "approved"
        assert ApprovalIntegrityService.validate_item_integrity(item) is True
        
        # Delete the referenced candidate fact to break traceability map
        self.fact.delete()
        
        # Validation should now raise ValidationError due to broken traceability
        with pytest.raises(ValidationError) as excinfo:
            ApprovalIntegrityService.validate_item_integrity(item)
        assert "Factual traceability broken" in str(excinfo.value)

    def test_job_apply_view_creates_application_draft_and_approval_item(self):
        self.client.force_login(self.user)
        url = reverse("career:job_apply", kwargs={"job_pk": self.job.pk})
        resp = self.client.post(url)
        assert resp.status_code == 302
        
        app = JobApplication.objects.get(candidate_profile=self.profile, job=self.job)
        assert app.status == "draft"
        
        # Check draft record created
        assert app.drafts.count() == 1
        draft = app.drafts.first()
        assert draft.version_number == 1
        
        # Check ApprovalItem record created in pending state
        assert app.approval_items.count() == 1
        item = app.approval_items.first()
        assert item.status == "pending"
        assert item.draft == draft

    def test_application_review_versioning_and_approval_workflows(self):
        app = JobApplication.objects.create(candidate_profile=self.profile, job=self.job)
        draft1 = ApplicationDraft.objects.create(
            application=app,
            version_number=1,
            tailored_cv="cv text v1",
            cover_letter="letter text v1"
        )
        item = ApprovalItem.objects.create(application=app, draft=draft1, status="pending")
        
        url = reverse("career:application_review", kwargs={"app_pk": app.pk})
        
        # 1. Seeker edits draft -> creates draft version #2
        self.client.force_login(self.user)
        resp = self.client.post(url, {
            "action": "save",
            "tailored_cv": "cv text v2",
            "cover_letter": "letter text v2"
        })
        assert resp.status_code == 302
        
        item.refresh_from_db()
        assert item.draft.version_number == 2
        assert item.draft.tailored_cv == "cv text v2"
        assert item.status == "pending"
        
        # Verify version query param GET request switches the loaded version
        resp_v1 = self.client.get(url + "?version=1")
        assert resp_v1.status_code == 200
        assert "cv text v1" in resp_v1.context["form"].initial["tailored_cv"]

        resp_v2 = self.client.get(url + "?version=2")
        assert resp_v2.status_code == 200
        assert "cv text v2" in resp_v2.context["form"].initial["tailored_cv"]

        # 2. Non-operator tries to flag needs info -> PermissionDenied (403)
        resp_flag_bad = self.client.post(url, {
            "action": "needs_info",
            "reason": "missing info"
        })
        assert resp_flag_bad.status_code == 403

        # Operator flags needs info successfully
        self.client.force_login(self.operator)
        resp_flag = self.client.post(url, {
            "action": "needs_info",
            "reason": "Please add Stark details."
        })
        assert resp_flag.status_code == 302
        item.refresh_from_db()
        assert item.status == "needs_info"
        assert item.needs_info_reason == "Please add Stark details."

        # 3. Seeker edits draft while in needs_info -> Resubmits for review (pending)
        self.client.force_login(self.user)
        resp_save_review = self.client.post(url, {
            "action": "save",
            "tailored_cv": "cv text v3",
            "cover_letter": "letter text v3"
        })
        assert resp_save_review.status_code == 302
        item.refresh_from_db()
        assert item.status == "pending"  # status reset to pending!
        assert item.draft.version_number == 3

        # 4. Non-operator tries to approve -> PermissionDenied (403)
        resp_app_bad = self.client.post(url, {
            "action": "approve"
        })
        assert resp_app_bad.status_code == 403

        # Operator approves and freezes
        self.client.force_login(self.operator)
        resp_approve = self.client.post(url, {
            "action": "approve",
            "note": "Looks perfect now"
        })
        assert resp_approve.status_code == 302
        item.refresh_from_db()
        assert item.status == "approved"
        
        # Verify private document storage file created
        p_storage = PrivateFileSystemStorage()
        filename = f"tailored/app_{app.id}.docx"
        assert p_storage.exists(filename) is True

        # 5. Revoke approval and revert to draft
        resp_revoke = self.client.post(url, {
            "action": "revert_to_draft",
            "note": "Revoking to update Stark details"
        })
        assert resp_revoke.status_code == 302
        item.refresh_from_db()
        app.refresh_from_db()
        assert item.status == "revoked"
        assert not hasattr(app, "approval")
        assert p_storage.exists(filename) is False  # deleted!

    def test_protected_docx_export_view(self):
        app = JobApplication.objects.create(candidate_profile=self.profile, job=self.job, tailored_cv="cv text", cover_letter="letter text")
        self.client.force_login(self.user)
        url = reverse("career:download_docx", kwargs={"app_pk": app.pk})
        resp = self.client.get(url)
        assert resp.status_code == 200
        assert resp['Content-Type'] == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        assert b"tailored_application" in resp.headers['Content-Disposition'].encode('utf-8')

    def test_approval_dashboard_bulk_workflow(self):
        app1 = JobApplication.objects.create(candidate_profile=self.profile, job=self.job)
        draft1 = ApplicationDraft.objects.create(application=app1, version_number=1, tailored_cv="cv 1", cover_letter="letter 1")
        item1 = ApprovalItem.objects.create(application=app1, draft=draft1, status="pending")

        job2 = Job.objects.create(title="Tester", company="Stark", description="Testing job")
        app2 = JobApplication.objects.create(candidate_profile=self.profile, job=job2)
        draft2 = ApplicationDraft.objects.create(application=app2, version_number=1, tailored_cv="cv 2", cover_letter="letter 2")
        item2 = ApprovalItem.objects.create(application=app2, draft=draft2, status="pending")

        url_dashboard = reverse("career:approval_dashboard")

        # 1. Seeker user is forbidden from dashboard (403)
        self.client.force_login(self.user)
        resp_bad = self.client.get(url_dashboard)
        assert resp_bad.status_code == 403

        # 2. Operator user accesses dashboard (200)
        self.client.force_login(self.operator)
        resp_ok = self.client.get(url_dashboard)
        assert resp_ok.status_code == 200

        # 3. Operator bulk approves selected items
        resp_bulk = self.client.post(url_dashboard, {
            "action": "bulk_approve",
            "selected_items": [item1.pk, item2.pk]
        })
        assert resp_bulk.status_code == 302
        
        item1.refresh_from_db()
        item2.refresh_from_db()
        assert item1.status == "approved"
        assert item2.status == "approved"
        assert item1.batch is not None
        assert item2.batch is not None
