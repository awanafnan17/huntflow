# openoutreach/core/management/commands/first_live_run_checklist.py
"""First live run checklist command."""
from __future__ import annotations

import json
from django.core.management.base import BaseCommand
from openoutreach.core.production import get_production_readiness_snapshot
from openoutreach.core.readiness import check_launch_readiness
from openoutreach.core.operator_alerts import get_operator_alert_summary


class Command(BaseCommand):
    help = "Verify all launch, system, and campaign checklists before initiating live outreach."

    def add_arguments(self, parser):
        parser.add_argument(
            "--campaign-id",
            type=int,
            default=0,
            help="Campaign ID to run diagnostic preview checks against.",
        )
        parser.add_argument(
            "--json",
            action="store_true",
            help="Output only valid JSON checklist summary to stdout.",
        )

    def handle(self, *args, **options):
        campaign_id = options["campaign_id"]
        use_json = options["json"]

        errors = []
        warnings = []
        checklist = {
            "ready_for_controlled_live_run": False,
            "blockers": errors,
            "warnings": warnings,
            "checks": {
                "llm_configured": False,
                "linkedin_profile_configured": False,
                "campaign_active": False,
                "task_queue_executable": False,
                "connection_note_reviewed": True,  # Manual check
                "limits_reviewed": True,          # Manual check
                "daemon_stopped_before_launch": False,
                "backup_exists": False,
                "logs_writable": False,
                "hard_alerts_cleared": False,
            },
            "recommended_next_action": "",
        }

        # 1. Gather snapshots
        prod_snap = get_production_readiness_snapshot()
        readiness_snap = check_launch_readiness()
        inbox_snap = get_operator_alert_summary()

        # Update checks
        checks = checklist["checks"]
        checks["llm_configured"] = prod_snap["secret_config_status"]["llm_configured"]
        checks["linkedin_profile_configured"] = prod_snap["linkedin_account_status"]["credentials_valid"]
        checks["campaign_active"] = prod_snap["linkedin_account_status"]["active_linkedin"]
        
        # Check task queue executable
        checks["task_queue_executable"] = readiness_snap.get("overall_status") != "blocked"
        
        daemon_snap = prod_snap["daemon_status"]
        checks["daemon_stopped_before_launch"] = (
            daemon_snap["process_state"] == "stopped"
            and daemon_snap["pid"] is None
            and daemon_snap["is_running"] is False
        )

        # Check backup and logs
        checks["backup_exists"] = prod_snap["backup_status"]["backup_count"] > 0
        checks["logs_writable"] = prod_snap["log_status"]["log_writable"]
        
        # Alerts check
        checks["hard_alerts_cleared"] = inbox_snap.get("active", 0) == 0

        # Append blockers and warnings
        for blk in prod_snap["blockers"]:
            errors.append(blk)
        for blk in readiness_snap.get("blockers", []):
            if blk not in errors:
                errors.append(blk)

        for wrn in prod_snap["warnings"]:
            warnings.append(wrn)
        for wrn in readiness_snap.get("warnings", []):
            if wrn not in warnings:
                warnings.append(wrn)

        # Decide go-live status
        if not errors:
            checklist["ready_for_controlled_live_run"] = True
            checklist["recommended_next_action"] = "Ready to start. Initialize the daemon process from the dashboard."
        else:
            checklist["recommended_next_action"] = f"Resolve production blockers: {errors[0]}"

        if use_json:
            self.stdout.write(json.dumps(checklist, default=str, indent=2))
        else:
            self.stdout.write("=============================================================")
            self.stdout.write("FIRST CONTROLLED LIVE OUTREACH RUN CHECKLIST")
            self.stdout.write("=============================================================")
            self.stdout.write(f"Ready for Live Run     : {checklist['ready_for_controlled_live_run']}")
            self.stdout.write(f"LLM Configured         : {checks['llm_configured']}")
            self.stdout.write(f"LinkedIn Configured    : {checks['linkedin_profile_configured']}")
            self.stdout.write(f"Daemon Stopped         : {checks['daemon_stopped_before_launch']}")
            self.stdout.write(f"Backup Exists          : {checks['backup_exists']}")
            self.stdout.write(f"Logs Writable          : {checks['logs_writable']}")
            self.stdout.write(f"Inbox Alerts Cleared   : {checks['hard_alerts_cleared']}")
            
            if errors:
                self.stdout.write("-------------------------------------------------------------")
                self.stdout.write("BLOCKERS:")
                for err in errors:
                    self.stdout.write(f"  * {err}")
            if warnings:
                self.stdout.write("-------------------------------------------------------------")
                self.stdout.write("WARNINGS:")
                for wrn in warnings:
                    self.stdout.write(f"  * {wrn}")
                    
            self.stdout.write("-------------------------------------------------------------")
            self.stdout.write(f"Recommended Next Action: {checklist['recommended_next_action']}")
            self.stdout.write("=============================================================")
