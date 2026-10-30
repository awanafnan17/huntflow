# openoutreach/core/management/commands/launch_readiness.py
"""Launch readiness check management command (Batch 11).

Calls unified launch-readiness service, structures outputs, supports scoping,
strict exit modes, JSON outputs, and ensures zero credential leak.
"""
from __future__ import annotations

import sys
import json
from django.core.management.base import BaseCommand
from openoutreach.core.readiness import check_launch_readiness


class Command(BaseCommand):
    help = "Analyze and print the launch readiness check report for outreach."

    def add_arguments(self, parser):
        parser.add_argument(
            "--json",
            action="store_true",
            help="Output the checklist metadata as a formatted JSON string to stdout.",
        )
        parser.add_argument(
            "--campaign-id",
            type=int,
            default=None,
            help="Scope lead pipeline and task queue analysis to a specific campaign.",
        )
        parser.add_argument(
            "--strict",
            action="store_true",
            help="Exit with code 2 if warnings exist but no blockers.",
        )
        parser.add_argument(
            "--include-warnings",
            action="store_true",
            help="Include detailed warnings inside the stdout text report.",
        )

    def handle(self, *args, **options):
        campaign_id = options["campaign_id"]
        use_json = options["json"]
        strict = options["strict"]
        include_warnings = options["include_warnings"]

        # Run checklist
        res = check_launch_readiness(campaign_id=campaign_id)

        if use_json:
            # Output raw JSON representation to stdout
            self.stdout.write(json.dumps(res, indent=2))
        else:
            self.stdout.write("=============================================================")
            self.stdout.write("HUNTFLOW LAUNCH READINESS CHECK")
            if res["campaign_scoped"]:
                self.stdout.write(f"Scope: Campaign-Scoped (ID: {res['scoped_campaign_id']})")
            else:
                self.stdout.write("Scope: Global Status")
            self.stdout.write("=============================================================")
            
            # Funnel status
            status_map = {
                "blocked": "BLOCKED (Hard blockers exist. Start is disabled.)",
                "warning": "WARNING (Operational concerns detected.)",
                "ready": "READY TO LAUNCH",
            }
            self.stdout.write(f"Overall Status: {status_map.get(res['overall_status'], 'UNKNOWN')}")
            self.stdout.write(f"Blockers Count: {len(res['hard_blockers'])}")
            self.stdout.write(f"Warnings Count: {len(res['warnings'])}")
            self.stdout.write("-------------------------------------------------------------")
            self.stdout.write(f"Next Action   : {res['next_action']['title']}")
            self.stdout.write(f"Detail        : {res['next_action']['description']}")
            self.stdout.write("=============================================================")

            # Print Blockers
            if res["hard_blockers"]:
                self.stdout.write("\nHARD BLOCKERS DETECTED:")
                for i, b in enumerate(res["hard_blockers"], 1):
                    self.stdout.write(f"  {i}. {b['title']}")
                    self.stdout.write(f"     Description: {b['description']}")
                    self.stdout.write(f"     Action Link: {b['fix_link']}")
                self.stdout.write("")

            # Print Warnings if requested or present
            if include_warnings and res["warnings"]:
                self.stdout.write("\nOPERATIONAL WARNINGS:")
                for i, w in enumerate(res["warnings"], 1):
                    self.stdout.write(f"  {i}. {w['title']}")
                    self.stdout.write(f"     Description: {w['description']}")
                    self.stdout.write(f"     Action Link: {w['fix_link']}")
                self.stdout.write("")

            self.stdout.write("Launch readiness check complete.")

        # Determine exit code
        if res["overall_status"] == "blocked":
            sys.exit(1)
        elif res["overall_status"] == "warning" and strict:
            sys.exit(2)
        else:
            sys.exit(0)
