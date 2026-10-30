# openoutreach/urls.py
from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import path, include
from django.views.generic import RedirectView

from openoutreach.dashboard import saas_views
from openoutreach.career import views as career_views

urlpatterns = [
    path("health/live", saas_views.health_live_check, name="health_live_check"),
    path("health/ready", saas_views.health_ready_check, name="health_ready_check"),
    path("healthz/", saas_views.healthz_check, name="healthz_check"),
    path("health/", saas_views.health_check, name="root_health_check"),
    path("readiness/", saas_views.readiness_check, name="root_readiness_check"),
    path("admin/", admin.site.urls),
    path("settings/identity-resolution/", career_views.identity_resolution_settings, name="identity_resolution_settings_root"),
    path("dashboard/career/", include("openoutreach.career.urls")),
    path("dashboard/", include("openoutreach.dashboard.urls")),
    path("", RedirectView.as_view(url="/dashboard/", permanent=True)),
]

urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

