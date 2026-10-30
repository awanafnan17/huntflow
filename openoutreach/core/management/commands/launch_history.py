# openoutreach/core/management/commands/launch_history.py
"""Launch history check management command (Batch 12).

Displays LaunchRun statistics, operator details, and execution parameters.
"""
from __future__ import annotations

import json
from django.core.management.base import BaseCommand
from openoutreach.core.models import LaunchRun
from openoutreach.core.launch_preview import mask_sensitive_payload_recursive


class Command(BaseCommand):
    help = "Display outreach daemon launch run history records."

    def add_arguments(self, parser):
        parser.add_argument(
            "--limit",
            type=int,
            default=10,
            help="Limit the number of history logs printed.",
        )
        parser.add_argument(
            "--status",
            type=str,
            default=None,
            help="Filter history logs by status (started, running, stopped, blocked, failed).",
        )
        parser.add_argument(
            "--json",
            action="store_true",
            help="Output history logs as a formatted JSON array.",
        )

    def handle(self, *args, **options):
        limit = options["limit"]
        status = options["status"]
        use_json = options["json"]

        runs = LaunchRun.objects.select_related("operator", "campaign").all()
        if status:
            runs = runs.filter(status=status)

        runs = runs[:limit]

        if use_json:
            data = []
            for r in runs:
                data.append({
                    "id": r.id,
                    "operator": r.operator.username if r.operator else None,
                    "status": r.status,
                    "source": r.source,
                    "mode": r.mode,
                    "campaign_id": r.campaign_id,
                    "daemon_pid": r.daemon_pid,
                    "readiness_status": r.readiness_status,
                    "blocker_count": r.blocker_count,
                    "warning_count": r.warning_count,
                    "started_at": str(r.started_at) if r.started_at else None,
                    "ended_at": str(r.ended_at) if r.ended_at else None,
                    "stop_reason": r.stop_reason,
                    "readiness_snapshot": mask_sensitive_payload_recursive(r.readiness_snapshot),
                    "queue_snapshot": mask_sensitive_payload_recursive(r.queue_snapshot),
                    "metadata": mask_sensitive_payload_recursive(r.metadata),
                })
            self.stdout.write(json.dumps(data, indent=2))
        else:
            self.stdout.write("=============================================================")
            self.stdout.write("HUNTFLOW LAUNCH RUN HISTORY")
            self.stdout.write("=============================================================")
            if not runs.exists():
                self.stdout.write("No launch runs found.")
            else:
                for r in runs:
                    self.stdout.write(f"Run #{r.id}:")
                    self.stdout.write(f"  Status        : {r.status.upper()}")
                    self.stdout.write(f"  Mode          : {r.mode}")
                    self.stdout.write(f"  Source        : {r.source}")
                    self.stdout.write(f"  Operator      : {r.operator.username if r.operator else 'System'}")
                    self.stdout.write(f"  PID           : {r.daemon_pid or '-'}")
                    self.stdout.write(f"  Started At    : {r.started_at or '-'}")
                    self.stdout.write(f"  Ended At      : {r.ended_at or '-'}")
                    self.stdout.write(f"  Readiness Status: {r.readiness_status}")
                    self.stdout.write(f"  Blockers Count: {r.blocker_count} | Warnings Count: {r.warning_count}")
                    if r.stop_reason:
                        self.stdout.write(f"  Stop Reason   : {r.stop_reason}")
                    self.stdout.write("-------------------------------------------------------------")
