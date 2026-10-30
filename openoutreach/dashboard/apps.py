# openoutreach/dashboard/apps.py
from django.apps import AppConfig


class DashboardConfig(AppConfig):
    name = "openoutreach.dashboard"
    label = "dashboard"
    default_auto_field = "django.db.models.BigAutoField"
