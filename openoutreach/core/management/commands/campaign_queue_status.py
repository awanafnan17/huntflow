# openoutreach/core/management/commands/campaign_queue_status.py
from django.core.management.base import BaseCommand
from openoutreach.core.models import Campaign, Task
from openoutreach.crm.models import DealState

class Command(BaseCommand):
    help = "Displays the queue status and task counts for all campaigns."

    def handle(self, *args, **options):
        self.stdout.write("HuntFlow Campaign Queue Status")
        self.stdout.write("=" * 60)

        campaigns = Campaign.objects.all().order_by("name")
        all_pending = list(Task.objects.filter(status=Task.Status.PENDING))
        all_failed = list(Task.objects.filter(status=Task.Status.FAILED))

        for c in campaigns:
            connect_pending = sum(
                1 for t in all_pending
                if t.payload.get("campaign_id") == c.id and t.task_type == Task.TaskType.CONNECT
            )
            followup_pending = sum(
                1 for t in all_pending
                if t.payload.get("campaign_id") == c.id and t.task_type == Task.TaskType.FOLLOW_UP
            )
            check_pending = sum(
                1 for t in all_pending
                if t.payload.get("campaign_id") == c.id and t.task_type == Task.TaskType.CHECK_PENDING
            )
            failed_count = sum(1 for t in all_failed if t.payload.get("campaign_id") == c.id)
            ignored_pending = (connect_pending + followup_pending + check_pending) if not c.is_active else 0
            
            connected_count = c.deals.filter(state=DealState.CONNECTED).count()

            self.stdout.write(f"Campaign: {c.name}")
            self.stdout.write(f"  Status: {'Active' if c.is_active else 'Paused'}")
            self.stdout.write(f"  Daily Connect Cap: {c.daily_connect_limit if c.daily_connect_limit is not None else 'Unlimited'}")
            self.stdout.write(f"  Daily Follow-up Cap: {c.daily_follow_up_limit if c.daily_follow_up_limit is not None else 'Unlimited'}")
            self.stdout.write(f"  Pending Connect: {connect_pending}")
            self.stdout.write(f"  Pending Follow-up: {followup_pending}")
            self.stdout.write(f"  Pending Check: {check_pending}")
            self.stdout.write(f"  Ignored Pending: {ignored_pending}")
            self.stdout.write(f"  Failed Tasks: {failed_count}")
            self.stdout.write(f"  Connected Leads: {connected_count}")
            self.stdout.write("-" * 60)
