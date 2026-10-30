# openoutreach/core/management/commands/operator_inbox_status.py
"""Operator inbox status management command."""
from __future__ import annotations

import json
from django.core.management.base import BaseCommand
from openoutreach.core.models import OperatorAlert
from openoutreach.core.operator_alerts import serialize_operator_alert, mask_alert_payload


class Command(BaseCommand):
    help = "Display summaries and event timelines of local inbox alerts."

    def add_arguments(self, parser):
        parser.add_argument(
            "--json",
            action="store_true",
            help="Output alert details as a credential-safe JSON structure.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=10,
            help="Limit the number of alerts printed.",
        )
        parser.add_argument(
            "--status",
            type=str,
            default="",
            help="Filter alerts by status (active, acknowledged, snoozed, dismissed, resolved).",
        )
        parser.add_argument(
            "--severity",
            type=str,
            default="",
            help="Filter alerts by severity (hard, warning, info).",
        )
        parser.add_argument(
            "--source",
            type=str,
            default="",
            help="Filter alerts by source.",
        )
        parser.add_argument(
            "--include-events",
            action="store_true",
            help="Include detailed history event logs for each alert.",
        )

    def handle(self, *args, **options):
        use_json = options["json"]
        limit = options["limit"]
        status_filter = options["status"].strip()
        severity_filter = options["severity"].strip()
        source_filter = options["source"].strip()
        include_events = options["include_events"]

        qs = OperatorAlert.objects.all()

        if status_filter:
            qs = qs.filter(status=status_filter)
        if severity_filter:
            qs = qs.filter(severity=severity_filter)
        if source_filter:
            qs = qs.filter(source=source_filter)

        alerts_qs = qs.prefetch_related("events")[:limit]

        serialized_alerts = []
        for alert in alerts_qs:
            data = serialize_operator_alert(alert)
            if include_events:
                events_list = []
                for evt in alert.events.all():
                    events_list.append({
                        "event_type": evt.event_type,
                        "operator": evt.operator.username if evt.operator else "System",
                        "note": evt.note,
                        "metadata": mask_alert_payload(evt.metadata or {}),
                        "created_at": evt.created_at.isoformat() if evt.created_at else None,
                    })
                data["events"] = events_list
            serialized_alerts.append(data)

        # Recursively mask the whole container
        clean_alerts = mask_alert_payload(serialized_alerts)

        if use_json:
            self.stdout.write(json.dumps(clean_alerts, default=str, indent=2))
        else:
            self.stdout.write("=============================================================")
            self.stdout.write(f"OPERATOR INBOX ALERTS LIST (Limit: {limit})")
            self.stdout.write("=============================================================")
            
            for alert in clean_alerts:
                self.stdout.write(f"[{alert['severity'].upper()}] {alert['title']}")
                self.stdout.write(f"  ID        : {alert['id']}")
                self.stdout.write(f"  Status    : {alert['status'].upper()}")
                self.stdout.write(f"  Source    : {alert['source']}")
                self.stdout.write(f"  Occurred  : {alert['occurrence_count']} times")
                self.stdout.write(f"  Last Seen : {alert['last_seen_at']}")
                if alert['fix_link']:
                    self.stdout.write(f"  Fix Link  : {alert['fix_link']}")
                
                if include_events and "events" in alert:
                    self.stdout.write("  Events Timeline:")
                    for e in alert["events"]:
                        self.stdout.write(f"    - [{e['event_type'].upper()}] by {e['operator']} at {e['created_at']}")
                        if e['note']:
                            self.stdout.write(f"      Note: {e['note']}")
                            
                self.stdout.write("-------------------------------------------------------------")
            self.stdout.write("Inbox alert status output complete.")
