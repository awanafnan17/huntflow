# openoutreach/core/management/commands/launch_preview.py
"""Launch preview check management command (Batch 12).

Calls unified launch-preview service, structures output, and ensures zero credential leak.
"""
from __future__ import annotations

import json
from django.core.management.base import BaseCommand
from openoutreach.core.launch_preview import generate_launch_preview


class Command(BaseCommand):
    help = "Generate a read-only preview of outreach daemon task execution."

    def add_arguments(self, parser):
        parser.add_argument(
            "--campaign-id",
            type=int,
            default=None,
            help="Scope analysis and task list to a specific campaign.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=10,
            help="Limit the number of pending tasks printed.",
        )
        parser.add_argument(
            "--task-type",
            type=str,
            default=None,
            help="Filter preview tasks by a specific task type.",
        )
        parser.add_argument(
            "--include-future",
            action="store_true",
            help="Include future pending tasks printed separately.",
        )
        parser.add_argument(
            "--include-payload",
            action="store_true",
            help="Include task payloads with recursive secret masking.",
        )
        parser.add_argument(
            "--json",
            action="store_true",
            help="Output the preview metadata as a formatted JSON string.",
        )

    def handle(self, *args, **options):
        campaign_id = options["campaign_id"]
        limit = options["limit"]
        task_type = options["task_type"]
        use_json = options["json"]

        preview = generate_launch_preview(campaign_id=campaign_id, limit=limit, task_type=task_type)

        # Remove payload summaries unless --include-payload is passed
        if not options["include_payload"]:
            for list_name in ["next_executable_tasks", "executable_now_tasks", "future_executable_tasks"]:
                for t in preview.get(list_name, []):
                    t.pop("payload_summary", None)

        if use_json:
            self.stdout.write(json.dumps(preview, indent=2))
        else:
            self.stdout.write("=============================================================")
            self.stdout.write("HUNTFLOW OUTREACH LAUNCH PREVIEW")
            if preview["campaign_scoped"]:
                self.stdout.write(f"Scope: Campaign-Scoped (ID: {preview['scoped_campaign_id']})")
            else:
                self.stdout.write("Scope: Global Status")
            if preview["task_type_filter"]:
                self.stdout.write(f"Filter: Task Type = {preview['task_type_filter']}")
            self.stdout.write("=============================================================")
            self.stdout.write(f"Readiness Status    : {preview['readiness_status'].upper()}")
            self.stdout.write(f"Estimated Outcome   : {preview['estimated_outcome']}")
            self.stdout.write("-------------------------------------------------------------")
            self.stdout.write(f"Tasks Ready Now     : {preview['executable_now_count']}")
            self.stdout.write(f"Future Pending      : {preview['future_executable_count']}")
            self.stdout.write(f"Ignored (Paused)    : {preview['ignored_paused_count']}")
            self.stdout.write(f"Failed Tasks        : {preview['failed_tasks_count']}")
            self.stdout.write(f"Cancelled Tasks     : {preview['cancelled_tasks_count']}")
            self.stdout.write(f"Next Scheduled Time : {preview['next_scheduled_time']}")
            self.stdout.write("=============================================================")

            if preview["executable_now_tasks"]:
                self.stdout.write("\nEXECUTABLE-NOW TASKS:")
                for i, t in enumerate(preview["executable_now_tasks"], 1):
                    self.stdout.write(f"  {i}. Task #{t['id']} [{t['task_type'].upper()}]")
                    self.stdout.write(f"     Scheduled  : {t['scheduled_at']}")
                    self.stdout.write(f"     Campaign   : {t['campaign_name']} (ID: {t['campaign_id']})")
                    recip = t["recipient_summary"]
                    if recip["resolved"]:
                        self.stdout.write(f"     Target Lead: {recip['name']} (ID: {recip['lead_id']})")
                        self.stdout.write(f"     Title/Co   : {recip['title']} at {recip['company']}")
                        self.stdout.write(f"     LinkedIn   : {recip['masked_linkedin_url']}")
                        if recip["note_preview"]:
                            self.stdout.write(f"     Preview    : {recip['note_preview']}")
                    else:
                        self.stdout.write(f"     Target Lead: [UNRESOLVED] (Count: {recip['resolved_count']})")
                    if options["include_payload"]:
                        self.stdout.write(f"     Payload    : {t.get('payload_summary')}")
                    self.stdout.write("  -----------------------------------------------------------")
                self.stdout.write("")
            else:
                self.stdout.write("\nNo executable-now tasks found.")

            if options["include_future"]:
                if preview["future_executable_tasks"]:
                    self.stdout.write("\nFUTURE PENDING TASKS:")
                    for i, t in enumerate(preview["future_executable_tasks"], 1):
                        self.stdout.write(f"  {i}. Task #{t['id']} [{t['task_type'].upper()}]")
                        self.stdout.write(f"     Scheduled  : {t['scheduled_at']}")
                        self.stdout.write(f"     Campaign   : {t['campaign_name']} (ID: {t['campaign_id']})")
                        recip = t["recipient_summary"]
                        if recip["resolved"]:
                            self.stdout.write(f"     Target Lead: {recip['name']} (ID: {recip['lead_id']})")
                            self.stdout.write(f"     Title/Co   : {recip['title']} at {recip['company']}")
                            self.stdout.write(f"     LinkedIn   : {recip['masked_linkedin_url']}")
                            if recip["note_preview"]:
                                self.stdout.write(f"     Preview    : {recip['note_preview']}")
                        else:
                            self.stdout.write(f"     Target Lead: [UNRESOLVED] (Count: {recip['resolved_count']})")
                        if options["include_payload"]:
                            self.stdout.write(f"     Payload    : {t.get('payload_summary')}")
                        self.stdout.write("  -----------------------------------------------------------")
                    self.stdout.write("")
                else:
                    self.stdout.write("\nNo future pending tasks found.")

            self.stdout.write("Launch preview complete.")
