from django.test import TestCase
from django.contrib.auth import get_user_model
from openoutreach.career.models import CandidateProfile, CandidateApplicationProfile

User = get_user_model()


class JobProfileSettingsViewTests(TestCase):
    def setUp(self):
        self.user1 = User.objects.create_user(
            username="job_prof_user1",
            email="user1@example.com",
            password="Password123!",
            is_staff=True,
        )
        self.user2 = User.objects.create_user(
            username="job_prof_user2",
            email="user2@example.com",
            password="Password123!",
            is_staff=True,
        )

    def test_authenticated_get_returns_200(self):
        self.client.force_login(self.user1)
        response = self.client.get("/dashboard/settings/job-profile/")
        self.assertEqual(response.status_code, 200)

    def test_missing_job_profile_creates_and_returns_200(self):
        self.client.force_login(self.user1)
        CandidateProfile.objects.filter(user=self.user1).delete()

        response = self.client.get("/dashboard/settings/job-profile/")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(CandidateProfile.objects.filter(user=self.user1).exists())

    def test_existing_job_profile_returns_200(self):
        cp = CandidateProfile.objects.create(user=self.user1, full_name="Test User 1")
        CandidateApplicationProfile.objects.create(candidate_profile=cp, phone="123456789")

        self.client.force_login(self.user1)
        response = self.client.get("/dashboard/settings/job-profile/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "123456789")

    def test_partial_profile_and_missing_cv_does_not_crash(self):
        cp = CandidateProfile.objects.create(user=self.user1, full_name="Partial User")
        cap = CandidateApplicationProfile.objects.create(candidate_profile=cp)
        cap.authorized_countries = []
        cap.target_job_titles = []
        cap.work_modes = []
        cap.minimum_salary = None
        cap.save()

        self.client.force_login(self.user1)
        response = self.client.get("/dashboard/settings/job-profile/")
        self.assertEqual(response.status_code, 200)

    def test_confirm_resume_action_succeeds(self):
        cp = CandidateProfile.objects.create(user=self.user1, full_name="Test User 1")
        cap = CandidateApplicationProfile.objects.create(candidate_profile=cp, confirmed=False)

        self.client.force_login(self.user1)
        response = self.client.post("/dashboard/settings/job-profile/", {"action": "confirm_resume"})
        self.assertEqual(response.status_code, 302)

        cap.refresh_from_db()
        self.assertTrue(cap.confirmed)
        self.assertTrue(cap.document_confirmed)

    def test_valid_post_saves_expected_fields(self):
        self.client.force_login(self.user1)
        data = {
            "phone": "+1-555-0199",
            "city": "Austin",
            "country": "USA",
            "authorized_countries": "US, CA",
            "target_job_titles": "Backend Engineer, Python Developer",
            "work_modes": "remote, hybrid",
            "minimum_salary": "$120,000",
            "requires_sponsorship_now": "on",
            "willing_to_relocate": "on",
        }
        response = self.client.post("/dashboard/settings/job-profile/", data)
        self.assertEqual(response.status_code, 302)

        cp = CandidateProfile.objects.filter(user=self.user1).first() or CandidateProfile.objects.first()
        cap = CandidateApplicationProfile.objects.get(candidate_profile=cp)
        self.assertEqual(cap.phone, "+1-555-0199")
        self.assertEqual(cap.city, "Austin")
        self.assertEqual(cap.authorized_countries, ["US", "CA"])
        self.assertEqual(cap.minimum_salary, 120000)
        self.assertTrue(cap.requires_sponsorship_now)
        self.assertTrue(cap.willing_to_relocate)

    def test_unauthenticated_redirects_to_login(self):
        response = self.client.get("/dashboard/settings/job-profile/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/dashboard/login/", response.url)
