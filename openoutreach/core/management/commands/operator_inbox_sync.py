# openoutreach/core/management/commands/operator_inbox_sync.py
"""Operator inbox sync management command."""
from __future__ import annotations

import json
from django.core.management.base import BaseCommand
from openoutreach.core.operator_alerts import sync_operator_alerts, mask_alert_payload


class Command(BaseCommand):
    help = "Synchronizes current operations alerts snapshot into local OperatorAlert notification logs."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Simulate the synchronization process without modifying database state.",
        )
        parser.add_argument(
            "--json",
            action="store_true",
            help="Format synchronization metrics output as a credential-safe JSON structure.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        use_json = options["json"]

        # Default operations-console sync source
        metrics = sync_operator_alerts(source="operations", dry_run=dry_run)
        
        # Ensure credentials are recursively masked in the output
        clean_metrics = mask_alert_payload(metrics)

        if use_json:
            self.stdout.write(json.dumps(clean_metrics, default=str, indent=2))
        else:
            self.stdout.write("=============================================================")
            self.stdout.write(f"OPERATOR INBOX SYNC COMPLETE (Dry-Run: {dry_run})")
            self.stdout.write("=============================================================")
            self.stdout.write(f"Created Alerts   : {clean_metrics['created_count']}")
            self.stdout.write(f"Updated Alerts   : {clean_metrics['updated_count']}")
            self.stdout.write(f"Reopened Alerts  : {clean_metrics['reopened_count']}")
            self.stdout.write(f"Resolved Alerts  : {clean_metrics['resolved_count']}")
            self.stdout.write("-------------------------------------------------------------")
            self.stdout.write(f"Total Active     : {clean_metrics['active_count']}")
            self.stdout.write(f"Total Acknowledged: {clean_metrics['acknowledged_count']}")
            self.stdout.write(f"Total Snoozed    : {clean_metrics['snoozed_count']}")
            self.stdout.write(f"Total Dismissed  : {clean_metrics['dismissed_count']}")
            self.stdout.write("=============================================================")
