# openoutreach/core/management/commands/task_queue_status.py
from __future__ import annotations

from django.core.management.base import BaseCommand
from django.utils import timezone
from django.db.models import Q
from openoutreach.core.models import Task, Campaign, TaskQueueEvent
from openoutreach.crm.models import Lead, LeadPipelineEvent
from openoutreach.linkedin.models import ActionLog


def mask_sensitive_payload(payload):
    if not isinstance(payload, dict):
        return payload
    masked = {}
    sensitive_keywords = ["password", "api_key", "token", "secret", "cookie", "authorization", "credential", "smtp", "app_password"]
    for k, v in payload.items():
        k_lower = k.lower()
        if any(kw in k_lower for kw in sensitive_keywords):
            masked[k] = "********"
        elif isinstance(v, dict):
            masked[k] = mask_sensitive_payload(v)
        elif isinstance(v, list):
            masked[k] = [mask_sensitive_payload(item) if isinstance(item, dict) else item for item in v]
        else:
            masked[k] = v
    return masked


class Command(BaseCommand):
    help = "Analyze and summarize open task queue items, status counts, and overdue durations."

    def add_arguments(self, parser):
        parser.add_argument("--status", type=str, help="Filter by task status")
        parser.add_argument("--task-type", type=str, help="Filter by task type")
        parser.add_argument("--campaign-id", type=int, help="Filter by campaign ID")
        parser.add_argument("--overdue-only", action="store_true", help="Only show overdue pending tasks")
        parser.add_argument("--limit", type=int, default=10, help="Max number of matching tasks to display")
        parser.add_argument("--include-payload", action="store_true", help="Include masked payload summaries in the detailed output")

    def handle(self, *args, **options):
        # Overall totals
        now = timezone.now()
        
        all_tasks = Task.objects.all()
        total_pending = all_tasks.filter(status=Task.Status.PENDING).count()
        total_running = all_tasks.filter(status=Task.Status.RUNNING).count()
        total_completed = all_tasks.filter(status=Task.Status.COMPLETED).count()
        total_failed = all_tasks.filter(status=Task.Status.FAILED).count()
        total_cancelled = all_tasks.filter(status=Task.Status.CANCELLED).count()
        total_overdue = all_tasks.filter(status=Task.Status.PENDING, scheduled_at__lt=now).count()

        # Apply command filters
        qs = all_tasks.order_by("-scheduled_at")
        
        if options["status"]:
            qs = qs.filter(status=options["status"])
        if options["task_type"]:
            qs = qs.filter(task_type=options["task_type"])
        if options["overdue_only"]:
            qs = qs.filter(status=Task.Status.PENDING, scheduled_at__lt=now)

        # In-memory filter for campaign_id (stored in payload json)
        campaign_id = options["campaign_id"]
        tasks_to_process = []
        
        campaign_cache = {c.id: c for c in Campaign.objects.all()}
        
        for task in qs:
            t_camp_id = task.payload.get("campaign_id")
            if campaign_id is not None and t_camp_id != campaign_id:
                continue
            tasks_to_process.append(task)

        # Output Summary
        self.stdout.write("=" * 60)
        self.stdout.write("TASK QUEUE STATE SUMMARY")
        self.stdout.write("=" * 60)
        self.stdout.write(f"  Pending    : {total_pending} (Overdue: {total_overdue})")
        self.stdout.write(f"  Running    : {total_running}")
        self.stdout.write(f"  Completed  : {total_completed}")
        self.stdout.write(f"  Failed     : {total_failed}")
        self.stdout.write(f"  Cancelled  : {total_cancelled}")
        self.stdout.write("-" * 60)
        self.stdout.write(f"Total matching tasks filtered: {len(tasks_to_process)}")
        self.stdout.write("=" * 60)

        # Print detailed list
        limit = options["limit"]
        for task in tasks_to_process[:limit]:
            t_camp_id = task.payload.get("campaign_id")
            campaign = campaign_cache.get(t_camp_id) if t_camp_id else None
            
            # Resolve lead public identifier
            lead_public_id = task.payload.get("lead_public_id") or task.payload.get("public_id") or task.payload.get("lead_id")
            lead = None
            if lead_public_id:
                lead = Lead.objects.filter(Q(public_identifier=lead_public_id) | Q(pk=lead_public_id) if str(lead_public_id).isdigit() else Q(public_identifier=lead_public_id)).first()
                
            if not lead and t_camp_id and task.status in (Task.Status.COMPLETED, Task.Status.FAILED):
                log_evt = LeadPipelineEvent.objects.filter(campaign_id=t_camp_id, created_at__gte=task.started_at or task.created_at).first()
                if log_evt:
                    lead = log_evt.lead
                else:
                    act_log = ActionLog.objects.filter(campaign_id=t_camp_id, created_at__gte=task.started_at or task.created_at).first()
                    if act_log and act_log.metadata:
                        lead_pub = act_log.metadata.get("lead_public_id")
                        if lead_pub:
                            lead = Lead.objects.filter(public_identifier=lead_pub).first()

            # Overdue status
            is_overdue = task.status == Task.Status.PENDING and task.scheduled_at < now
            overdue_str = "No"
            if is_overdue:
                diff = now - task.scheduled_at
                overdue_str = f"Yes (by {int(diff.total_seconds() // 60)}m)"
                if diff.total_seconds() >= 3600:
                    overdue_str = f"Yes (by {int(diff.total_seconds() // 3600)}h {int((diff.total_seconds() % 3600) // 60)}m)"

            # Last Event
            last_event = TaskQueueEvent.objects.filter(task=task).order_by("-created_at").first()
            last_event_str = f"{last_event.action} by {last_event.operator.username if last_event.operator else 'System'} ({last_event.note})" if last_event else "None"

            self.stdout.write(f"Task #{task.id}")
            self.stdout.write(f"  Type          : {task.task_type}")
            self.stdout.write(f"  Status        : {task.status}")
            self.stdout.write(f"  Scheduled (UTC): {task.scheduled_at}")
            self.stdout.write(f"  Campaign      : {campaign.name if campaign else 'None'}")
            self.stdout.write(f"  Lead          : {lead.public_identifier if lead else 'None'}")
            self.stdout.write(f"  Overdue       : {overdue_str}")
            self.stdout.write(f"  Last Event    : {last_event_str}")
            
            if options["include_payload"]:
                masked_payload = mask_sensitive_payload(task.payload)
                self.stdout.write(f"  Payload       : {masked_payload}")
            
            self.stdout.write("-" * 60)
