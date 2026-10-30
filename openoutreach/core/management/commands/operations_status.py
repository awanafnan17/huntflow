# openoutreach/core/management/commands/operations_status.py
"""Operations status monitor command (Batch 13).

Structures operations snapshot values cleanly, preventing secret key exposure.
"""
from __future__ import annotations

import json
from django.core.management.base import BaseCommand
from openoutreach.core.operations import get_operations_snapshot, mask_sensitive_payload_recursive


class Command(BaseCommand):
    help = "Display live operational snapshot, health state, alerts, and recent timelines."

    def add_arguments(self, parser):
        parser.add_argument(
            "--json",
            action="store_true",
            help="Output the operations snapshot as a formatted JSON structure.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=5,
            help="Limit the number of recent event entries printed.",
        )
        parser.add_argument(
            "--include-events",
            action="store_true",
            help="Include recent completions, failures, LinkedIn logs, and manual events.",
        )
        parser.add_argument(
            "--include-alerts",
            action="store_true",
            help="Include detailed operator alerts list.",
        )

    def handle(self, *args, **options):
        use_json = options["json"]
        limit = options["limit"]
        include_events = options["include_events"]
        include_alerts = options["include_alerts"]

        snapshot = get_operations_snapshot()

        # Slice recent lists based on limit option
        for list_name in [
            "recent_completed_tasks", "recent_failed_tasks", "recent_action_logs", 
            "recent_lead_events", "recent_queue_events", "recent_launch_events"
        ]:
            if list_name in snapshot:
                snapshot[list_name] = snapshot[list_name][:limit]

        # Apply recursive secret masking to all elements in the snapshot to prevent leaks
        snapshot = mask_sensitive_payload_recursive(snapshot)

        # Clear event lists from the snapshot if --include-events is not requested (but keep count keys)
        if not include_events and not use_json:
            for list_name in [
                "recent_completed_tasks", "recent_failed_tasks", "recent_action_logs", 
                "recent_lead_events", "recent_queue_events", "recent_launch_events"
            ]:
                snapshot[list_name] = []

        if use_json:
            # Mask and serialize
            self.stdout.write(json.dumps(snapshot, default=str, indent=2))
        else:
            self.stdout.write("=============================================================")
            self.stdout.write("HUNTFLOW OPERATIONS MONITOR CONSOLE")
            self.stdout.write("=============================================================")
            
            health = snapshot["daemon_health"]
            status_text = "RUNNING" if health["is_running_actual"] else (
                "UNKNOWN" if health["process_state"] == "unknown" else "STOPPED"
            )
            self.stdout.write(f"Daemon Loop Status  : {status_text} (PID: {health['pid'] or '-'})")
            self.stdout.write(f"Last Loop Heartbeat : {health['last_heartbeat'] or '-'}")
            self.stdout.write(f"Active Account      : {health['active_profile_username'] or '-'}")
            self.stdout.write(f"Readiness Status    : {snapshot['readiness_summary']['overall_status'].upper()}")
            
            run = snapshot["latest_launch_run"]
            if run:
                self.stdout.write(f"Latest Launch Run   : Run #{run['id']} (Status: {run['status'].upper()}, Op: {run['operator']})")
                if run["stop_reason"]:
                    self.stdout.write(f"  Stop/End Reason   : {run['stop_reason']}")
            
            self.stdout.write("-------------------------------------------------------------")
            self.stdout.write(f"Running Tasks Count : {len(snapshot['running_tasks'])}")
            self.stdout.write(f"Overdue Tasks Count : {snapshot['overdue_count']}")
            self.stdout.write(f"Pending Executable  : {snapshot['pending_executable_count']}")
            self.stdout.write(f"Failed Tasks Count  : {snapshot['failed_count']}")
            self.stdout.write(f"Cancelled Count     : {snapshot['cancelled_count']}")
            
            next_t = snapshot["next_task"]
            if next_t:
                self.stdout.write(f"Next Task In Queue  : Task #{next_t['id']} [{next_t['task_type'].upper()}]")
                self.stdout.write(f"  Scheduled Time    : {next_t['scheduled_at']}")
            else:
                self.stdout.write("Next Task In Queue  : None")
            self.stdout.write("=============================================================")
            self.stdout.write(f"Next Operator Action: {snapshot['next_recommended_action']['title']}")
            self.stdout.write(f"  Detail            : {snapshot['next_recommended_action']['description']}")
            self.stdout.write("=============================================================")

            if include_alerts:
                self.stdout.write("\nACTIVE ALERTS:")
                if snapshot["alerts"]:
                    for i, alert in enumerate(snapshot["alerts"], 1):
                        self.stdout.write(f"  {i}. [{alert['severity'].upper()}] {alert['title']}")
                        self.stdout.write(f"     Description: {alert['description']}")
                        self.stdout.write(f"     Dashboard  : {alert['fix_link']}")
                        self.stdout.write("  -----------------------------------------------------------")
                else:
                    self.stdout.write("  No active alerts.")
                self.stdout.write("=============================================================")

            if include_events:
                self.stdout.write("\nRECENT TIMELINE ACTIONS:")
                
                if snapshot["recent_completed_tasks"]:
                    self.stdout.write("\n  COMPLETED TASKS:")
                    for t in snapshot["recent_completed_tasks"]:
                        self.stdout.write(f"    - Task #{t['id']} [{t['task_type'].upper()}] at {t['completed_at']}")
                
                if snapshot["recent_failed_tasks"]:
                    self.stdout.write("\n  FAILED TASKS:")
                    for t in snapshot["recent_failed_tasks"]:
                        self.stdout.write(f"    - Task #{t['id']} [{t['task_type'].upper()}] at {t['completed_at']}")
                        
                if snapshot["recent_action_logs"]:
                    self.stdout.write("\n  LINKEDIN ACTIVITY LOGS:")
                    for log in snapshot["recent_action_logs"]:
                        self.stdout.write(f"    - [{log['action_type'].upper()}] by {log['linkedin_profile']} ({log['campaign_name']}) at {log['created_at']}")
                        
                if snapshot["recent_lead_events"]:
                    self.stdout.write("\n  MANUAL LEAD PIPELINE ACTIONS:")
                    for evt in snapshot["recent_lead_events"]:
                        self.stdout.write(f"    - {evt['action']} on {evt['lead_identifier']} ({evt['previous_state']} -> {evt['new_state']}) by {evt['operator']} at {evt['created_at']}")
                        
                if snapshot["recent_queue_events"]:
                    self.stdout.write("\n  TASK QUEUE EVENTS:")
                    for evt in snapshot["recent_queue_events"]:
                        self.stdout.write(f"    - {evt['action']} Task #{evt['task_id']} by {evt['operator']} at {evt['created_at']}")
                        
                if snapshot["recent_launch_events"]:
                    self.stdout.write("\n  DAEMON LAUNCH EVENTS:")
                    for evt in snapshot["recent_launch_events"]:
                        self.stdout.write(f"    - Run #{evt['launch_run_id']} event: {evt['event_type']} by {evt['operator']} at {evt['created_at']}")
                        
                self.stdout.write("\n=============================================================")

            self.stdout.write("Operations status output complete.")
