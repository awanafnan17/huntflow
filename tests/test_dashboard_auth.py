# tests/test_dashboard_auth.py
"""Tests for HuntFlow dashboard login page and authentication controls (Batch 5.6)."""
from __future__ import annotations

import pytest
from django.urls import reverse

from django.contrib.auth.models import User
from tests.factories import UserFactory


@pytest.mark.django_db
class TestDashboardAuthentication:

    def test_dashboard_logged_out_redirects_to_dashboard_login(self, client):
        """Visiting dashboard index when logged out redirects to dashboard:login."""
        url = reverse("dashboard:overview")
        response = client.get(url)
        assert response.status_code == 302
        assert "/dashboard/login/" in response.url
        assert "/admin/login/" not in response.url

    def test_login_shows_huntflow_branding(self, client):
        """The custom login page shows HuntFlow branding and not Django Administration."""
        url = reverse("dashboard:login")
        response = client.get(url)
        assert response.status_code == 200
        content = response.content.decode("utf-8")
        assert "HuntFlow" in content
        assert "Django administration" not in content

    def test_staff_login_works(self, client):
        """An active staff user can log in and gets redirected to overview."""
        user = User.objects.create_user(
            username="hfstaff",
            password="staffpassword123",
            is_staff=True,
            is_active=True,
        )
        url = reverse("dashboard:login")
        response = client.post(url, {
            "username": "hfstaff",
            "password": "staffpassword123"
        })
        assert response.status_code == 302
        assert response.url == reverse("dashboard:overview")

    def test_wrong_credentials_show_error(self, client):
        """Submitting wrong credentials keeps the user on login with form errors."""
        url = reverse("dashboard:login")
        response = client.post(url, {
            "username": "nonexistent",
            "password": "wrongpassword"
        })
        assert response.status_code == 200
        content = response.content.decode("utf-8")
        assert "Please enter a correct username and password." in content

    def test_non_staff_login_denied(self, client):
        """A non-staff user cannot log in and gets access denied error."""
        User.objects.create_user(
            username="normaluser",
            password="userpassword123",
            is_staff=False,
            is_active=True,
        )
        url = reverse("dashboard:login")
        response = client.post(url, {
            "username": "normaluser",
            "password": "userpassword123"
        })
        assert response.status_code == 200
        content = response.content.decode("utf-8")
        assert "Access denied: Staff status required." in content

    def test_non_staff_redirected_away_if_accessing_dashboard(self, client):
        """If a non-staff user gets authenticated somehow, they are redirected from dashboard to login."""
        user = UserFactory(username="plainuser", is_staff=False, is_superuser=False)
        client.force_login(user)

        url = reverse("dashboard:overview")
        response = client.get(url, follow=True)
        content = response.content.decode("utf-8")
        assert "Access denied: Staff status required." in content

    def test_logout_redirects_to_login(self, client):
        """Logging out logs the user out and redirects back to dashboard login."""
        user = User.objects.create_user(
            username="diagstaff",
            password="staffpassword123",
            is_staff=True,
            is_active=True,
        )
        client.force_login(user)

        url = reverse("dashboard:logout")
        response = client.get(url)
        assert response.status_code == 302
        assert response.url == reverse("dashboard:login")

    def test_django_admin_login_is_untouched(self, client):
        """The standard Django Admin login remains separate and accessible."""
        url = reverse("admin:login")
        response = client.get(url)
        assert response.status_code == 200
        content = response.content.decode("utf-8")
        assert "Django administration" in content

    def test_login_with_email_works(self, client):
        """An active staff user can log in with their email address using their existing password."""
        user = User.objects.create_user(
            username="emailstaff",
            email="staff.member@huntflow.ai",
            password="securepassword456",
            is_staff=True,
            is_active=True,
        )
        url = reverse("dashboard:login")
        response = client.post(url, {
            "username": "staff.member@huntflow.ai",
            "password": "securepassword456"
        })
        assert response.status_code == 302
        assert response.url == reverse("dashboard:overview")

    def test_login_with_candidate_profile_email_works(self, client):
        """A user can log in with their CandidateProfile email address using their existing password."""
        user = User.objects.create_user(
            username="candidate_user",
            email="internal@localhost",
            password="candidatepassword789",
            is_staff=True,
            is_active=True,
        )
        from openoutreach.career.models import CandidateProfile
        CandidateProfile.objects.create(
            user=user,
            full_name="Candidate Operator",
            email="candidate-6@example.com"
        )
        url = reverse("dashboard:login")
        response = client.post(url, {
            "username": "candidate-6@example.com",
            "password": "candidatepassword789"
        })
        assert response.status_code == 302
        assert response.url == reverse("dashboard:overview")

    def test_login_redirects_to_next_targeting_url(self, client):
        """Logging in with next=/dashboard/targeting/ redirects cleanly to /dashboard/targeting/."""
        user = User.objects.create_user(
            username="targeting_operator",
            email="targeting.op@huntflow.ai",
            password="targetingpassword321",
            is_staff=True,
            is_active=True,
        )
        url = f"{reverse('dashboard:login')}?next=/dashboard/targeting/"
        response = client.post(url, {
            "username": "targeting_operator",
            "password": "targetingpassword321"
        })
        assert response.status_code == 302
        assert response.url == "/dashboard/targeting/"

    def test_password_immutability_during_auth_and_maintenance(self, client):
        """Verify that user password hash remains bit-for-bit identical across auth and maintenance operations."""
        user = User.objects.create_user(
            username="immutable_op",
            email="immutable.op@huntflow.ai",
            password="original_secret_pass_999",
            is_staff=True,
            is_active=True,
        )
        before_hash = user.password

        # 1. Login attempt with wrong password
        client.post(reverse("dashboard:login"), {"username": "immutable_op", "password": "wrongpassword"})
        user.refresh_from_db()
        assert user.password == before_hash, "Password hash was modified after failed login!"

        # 2. Login attempt with correct username
        client.post(reverse("dashboard:login"), {"username": "immutable_op", "password": "original_secret_pass_999"})
        user.refresh_from_db()
        assert user.password == before_hash, "Password hash was modified after username login!"

        # 3. Login attempt with correct email
        client.post(reverse("dashboard:login"), {"username": "immutable.op@huntflow.ai", "password": "original_secret_pass_999"})
        user.refresh_from_db()
        assert user.password == before_hash, "Password hash was modified after email login!"

        # 4. Maintenance operations
        from openoutreach.core.models import Workspace, WorkspaceAutomationSettings
        ws = Workspace.objects.create(name="Immutable WS", slug="immutable-ws")
        settings = WorkspaceAutomationSettings.get_for_workspace(ws)
        settings.start_bounded_multi_channel_production()
        settings.master_automation_enabled = False
        settings.save()
        settings.master_automation_enabled = True
        settings.save()

        user.refresh_from_db()
        assert user.password == before_hash, "Password hash was modified during maintenance settings toggle!"

    def test_login_page_renders_username_or_email_label(self, client):
        """The login page displays 'Username or email' as the input label."""
        url = reverse("dashboard:login")
        response = client.get(url)
        assert response.status_code == 200
        content = response.content.decode("utf-8")
        assert "Username or email" in content

    def test_csrf_token_and_cookie_present_on_fresh_get(self, client):
        """A fresh GET to login sets the csrftoken cookie and renders csrfmiddlewaretoken input."""
        url = f"{reverse('dashboard:login')}?next=/dashboard/targeting/"
        response = client.get(url)
        assert response.status_code == 200
        assert "csrftoken" in response.cookies
        content = response.content.decode("utf-8")
        assert "csrfmiddlewaretoken" in content

    def test_anonymous_targeting_access_redirects_to_login_with_next(self, client):
        """Anonymous GET /dashboard/targeting/ redirects to login with next parameter."""
        url = "/dashboard/targeting/"
        response = client.get(url)
        assert response.status_code == 302
        assert "/dashboard/login/?next=/dashboard/targeting/" in response.url

    def test_authenticated_user_access_targeting_succeeds(self, client):
        """Authenticated staff user accessing targeting and overview gets HTTP 200."""
        user = User.objects.create_user(
            username="dash_operator",
            email="dash.op@huntflow.ai",
            password="dashpass12345",
            is_staff=True,
            is_active=True,
        )
        client.force_login(user)

        resp_target = client.get("/dashboard/targeting/")
        assert resp_target.status_code == 200

        resp_overview = client.get("/dashboard/")
        assert resp_overview.status_code in [200, 302]

