import pytest
from django.contrib.auth import get_user_model
from openoutreach.core.models import Campaign
from openoutreach.crm.models import Lead

User = get_user_model()

@pytest.mark.django_db
def test_dashboard_home_authenticated_get(client):
    """Authenticated GET /dashboard/ returns HTTP 200."""
    admin = User.objects.create_superuser(username="test_admin", password="password", email="admin@test.com")
    client.force_login(admin)

    response = client.get("/dashboard/", HTTP_HOST="127.0.0.1")
    assert response.status_code == 200


@pytest.mark.django_db
def test_integrations_authenticated_get(client):
    """Authenticated GET /dashboard/integrations/ returns HTTP 200."""
    admin = User.objects.create_superuser(username="test_admin_2", password="password", email="admin2@test.com")
    client.force_login(admin)

    response = client.get("/dashboard/integrations/", HTTP_HOST="127.0.0.1")
    assert response.status_code == 200


@pytest.mark.django_db
def test_primary_navigation_routes_smoke(client):
    """Primary portal navigation routes render HTTP 200."""
    admin = User.objects.create_superuser(username="test_admin_3", password="password", email="admin3@test.com")
    client.force_login(admin)

    routes = [
        "/dashboard/overview/",
        "/dashboard/campaigns/",
        "/dashboard/leads/",
        "/dashboard/inbox/",
        "/dashboard/settings/llm/",
        "/dashboard/settings/bettercontact/",
        "/dashboard/settings/system/",
        "/dashboard/settings/job-profile/",
        "/dashboard/readiness/",
        "/dashboard/operations/",
    ]

    for route in routes:
        resp = client.get(route, HTTP_HOST="127.0.0.1")
        assert resp.status_code == 200, f"Route {route} failed with status {resp.status_code}"
