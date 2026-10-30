# openoutreach/core/admin.py
from django.contrib import admin

from openoutreach.core.models import Campaign, SiteConfig, Task


@admin.register(SiteConfig)
class SiteConfigAdmin(admin.ModelAdmin):
    list_display = ("__str__", "ai_model", "llm_api_base")

    def has_add_permission(self, request):
        return not SiteConfig.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Campaign)
class CampaignAdmin(admin.ModelAdmin):
    list_display = ("name", "booking_link", "connection_note_enabled", "action_fraction")
    filter_horizontal = ("users",)
    fieldsets = (
        (None, {
            "fields": (
                "name", "users", "product_docs", "campaign_objective", "booking_link",
            ),
        }),
        ("Connection Notes", {
            "fields": (
                "connection_note_enabled",
                "connection_note_template",
                "connection_note_instructions",
            ),
            "classes": ("collapse",),
        }),
        ("Advanced", {
            "fields": ("action_fraction", "seed_public_ids"),
        }),
    )
    readonly_fields = ("is_freemium",)


@admin.register(Task)
class TaskAdmin(admin.ModelAdmin):
    list_display = ("task_type", "status", "scheduled_at", "payload", "created_at")
    list_filter = ("task_type", "status")
    readonly_fields = (
        "task_type", "status", "scheduled_at", "payload",
        "created_at", "started_at", "completed_at",
    )
    date_hierarchy = "scheduled_at"
