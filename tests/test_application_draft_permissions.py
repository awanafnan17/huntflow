import pytest
from django.urls import reverse
from django.core.exceptions import PermissionDenied
from openoutreach.career.models import JobApplication, ApplicationDraft, ApprovalItem
from tests.factories import UserFactory

@pytest.mark.django_db
class TestApplicationDraftPermissions:
    @pytest.fixture(autouse=True)
    def _setup(self, client, db):
        self.client = client
        self.user = UserFactory(username="seeker_user", email="seeker@draft.com")
        self.other_user = UserFactory(username="other_user", email="other@draft.com")
        
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

    def test_unauthenticated_blocked(self):
        url = reverse("career:application_review", kwargs={"app_pk": self.app.pk})
        resp = self.client.get(url)
        assert resp.status_code == 302  # redirect to login

    def test_other_user_blocked(self):
        self.client.force_login(self.other_user)
        url = reverse("career:application_review", kwargs={"app_pk": self.app.pk})
        resp = self.client.get(url)
        assert resp.status_code == 403  # PermissionDenied

    def test_owner_allowed(self):
        self.client.force_login(self.user)
        url = reverse("career:application_review", kwargs={"app_pk": self.app.pk})
        resp = self.client.get(url)
        assert resp.status_code == 200
