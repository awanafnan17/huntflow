# openoutreach/linkedin/management/commands/linkedin_task_preflight.py
"""Management command for performing a production read-only task preflight resolution pass."""
from __future__ import annotations

import json
from django.core.management.base import BaseCommand
from django.db import transaction
from openoutreach.core.models import Campaign, Task, Workspace
from openoutreach.crm.models import Lead
from openoutreach.core.task_factory import create_external_task
from openoutreach.linkedin.models import LinkedInProfile
from openoutreach.linkedin.vnc_service import _get_durable_session_file


class Command(BaseCommand):
    help = "Perform read-only production preflight resolution for a LinkedIn campaign task."

    def add_arguments(self, parser):
        parser.add_argument("--campaign-id", type=int, default=3, help="Campaign ID to run preflight for (default: 3)")
        parser.add_argument("--task-type", type=str, default="connect", help="Task type (e.g. connect, follow_up)")
        parser.add_argument("--lead-id", type=int, default=1, help="Lead ID for target task")
        parser.add_argument("--non-persistent-fixture", action="store_true", help="Run preflight in non-persistent fixture mode")

    def handle(self, *args, **options):
        campaign_id = options.get("campaign_id")
        task_type = options.get("task_type", "connect")
        lead_id = options.get("lead_id", 1)
        non_persistent = options.get("non_persistent_fixture", False)

        if non_persistent:
            self._run_non_persistent(task_type, lead_id)
            return

        camp = Campaign.objects.filter(pk=campaign_id).first()
        if not camp:
            self.stdout.write(json.dumps({
                "command": "manage.py linkedin_task_preflight",
                "campaign_id": campaign_id,
                "result": "CAMPAIGN_NOT_FOUND",
                "error": f"Campaign #{campaign_id} does not exist"
            }, indent=2))
            return

        self._execute_preflight(camp, task_type, lead_id)

    def _execute_preflight(self, camp: Campaign, task_type: str, lead_id: int):
        campaign_bound_profile_id = camp.linkedin_profile_id

        lead = Lead.objects.filter(pk=lead_id).first()
        if not lead and camp and hasattr(camp, "workspace") and camp.workspace:
            lead = Lead.objects.filter(workspace=camp.workspace).first()
            if lead:
                lead_id = lead.pk

        # Call production task factory resolver
        task = create_external_task(
            task_type=task_type,
            campaign_id=camp.pk,
            lead_id=lead_id if lead else None,
        )

        task_profile_id = task.payload.get("linkedin_profile_id") if task and task.payload else None
        worker_resolved_profile_id = task_profile_id

        session_eligible = False
        identity_eligible = False
        browser_lease_file = None
        browser_lease_profile_id = None

        if worker_resolved_profile_id:
            prof = LinkedInProfile.objects.filter(pk=worker_resolved_profile_id).first()
            if prof:
                session_eligible = (prof.session_status == "valid")
                identity_eligible = (prof.identity_validation_status == "VALID_IDENTITY_MATCH")
                if prof.can_execute():
                    browser_lease_profile_id = prof.pk
                    browser_lease_file = _get_durable_session_file(prof.pk)

        idempotency_key = task.payload.get("idempotency_key") if task and task.payload else None

        if not worker_resolved_profile_id or not session_eligible or not identity_eligible:
            result_status = "WAITING_ACCOUNT_SELECTION"
        else:
            result_status = "READY"

        preflight_data = {
            "command": "manage.py linkedin_task_preflight",
            "production_resolvers": ["create_external_task", "LinkedInProfile.can_execute", "_get_durable_session_file"],
            "workspace_id": camp.workspace_id,
            "campaign_id": camp.pk,
            "task_workspace_id": camp.workspace_id if task else None,
            "campaign_workspace_id": camp.workspace_id,
            "task_profile_id": task_profile_id,
            "campaign_bound_profile_id": campaign_bound_profile_id,
            "worker_resolved_profile_id": worker_resolved_profile_id,
            "browser_lease_profile_id": browser_lease_profile_id,
            "browser_lease_file": browser_lease_file,
            "session_eligible": session_eligible,
            "identity_eligible": identity_eligible,
            "idempotency_key": idempotency_key,
            "fallback_used": False,
            "external_request_sent": False,
            "result": result_status,
        }

        self.stdout.write(json.dumps(preflight_data, indent=2))

    def _run_non_persistent(self, task_type: str, lead_id: int):
        try:
            with transaction.atomic():
                ws, _ = Workspace.objects.get_or_create(name="Preflight Temp WS")
                camp = Campaign.objects.create(name="Preflight Temp Campaign", workspace=ws)
                lead = Lead.objects.create(workspace=ws, public_identifier="preflight-lead-1")
                self._execute_preflight(camp, task_type, lead.pk)
                raise RuntimeError("ROLLBACK_PREFLIGHT_FIXTURE")
        except RuntimeError as exc:
            if str(exc) != "ROLLBACK_PREFLIGHT_FIXTURE":
                raise
