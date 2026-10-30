import pytest
from django.urls import reverse
from django.contrib.auth.models import Permission
from openoutreach.career.models import JobApplication, ApplicationDraft, ApprovalItem
from tests.factories import UserFactory

@pytest.mark.django_db
class TestApprovalPermissions:
    @pytest.fixture(autouse=True)
    def _setup(self, client, db):
        self.client = client
        self.user = UserFactory(username="seeker_user", email="seeker@draft.com")
        self.operator = UserFactory(username="operator_user", email="operator@draft.com")
        self.operator.user_permissions.add(
            Permission.objects.get(codename="approve_profile")
        )
        
        # Candidate Profile
        from openoutreach.career.models import CandidateProfile
        self.profile = CandidateProfile.objects.create(
            user=self.user,
            full_name="Alice Candidate",
            email="seeker@draft.com"
        )
        
        # Job
        from openoutreach.career.models import Job
        self.job = Job.objects.create(
            title="Developer",
            company="Stark Industries"
        )
        
        self.app = JobApplication.objects.create(
            candidate_profile=self.profile,
            job=self.job
        )
        
        self.draft = ApplicationDraft.objects.create(
            application=self.app,
            version_number=1,
            tailored_cv="cv content",
            cover_letter="cover letter",
            created_by=self.user
        )
        self.item = ApprovalItem.objects.create(
            application=self.app,
            draft=self.draft,
            status="pending"
        )

    def test_non_operator_cannot_approve(self):
        self.client.force_login(self.user)
        url = reverse("career:application_review", kwargs={"app_pk": self.app.pk})
        resp = self.client.post(url, {
            "action": "approve",
            "note": "approved"
        })
        assert resp.status_code == 403

    def test_operator_can_approve(self):
        self.client.force_login(self.operator)
        url = reverse("career:application_review", kwargs={"app_pk": self.app.pk})
        resp = self.client.post(url, {
            "action": "approve",
            "note": "approved"
        })
        assert resp.status_code == 302
        self.item.refresh_from_db()
        assert self.item.status == "approved"
