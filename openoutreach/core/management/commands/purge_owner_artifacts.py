from django.core.management.base import BaseCommand
from django.db import transaction
from openoutreach.core.models import Campaign, Task


class Command(BaseCommand):
    help = "Purge owner/freemium campaigns and tasks pointing to deleted or nonexistent campaigns."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print what would be deleted without actually deleting.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]

        # 1. Identify target campaigns to delete
        # Strictly delete campaigns named "Freemium Outreach", or is_freemium=True campaigns
        # but NEVER delete the default/user campaign named "LinkedIn Outreach".
        target_campaigns = Campaign.objects.filter(is_freemium=True) | Campaign.objects.filter(name="Freemium Outreach")
        target_campaigns = target_campaigns.exclude(name="LinkedIn Outreach")

        campaign_pks = list(target_campaigns.values_list("pk", flat=True))
        campaign_names = list(target_campaigns.values_list("name", flat=True))

        self.stdout.write(f"Found {len(campaign_pks)} target owner/freemium campaign(s) to delete: {campaign_names}")

        # Campaigns we keep, but we must make sure their is_freemium is set to False
        campaigns_to_keep_but_reset = Campaign.objects.filter(is_freemium=True).exclude(pk__in=campaign_pks)
        reset_names = list(campaigns_to_keep_but_reset.values_list("name", flat=True))
        self.stdout.write(f"Campaigns that will be preserved but marked is_freemium=False: {reset_names}")

        # 2. Identify target tasks (pointing to target campaigns OR pointing to campaigns that don't exist at all)
        all_campaign_pks = set(Campaign.objects.values_list("pk", flat=True))
        # Exclude campaigns that will be deleted from the set of existing campaigns
        remaining_campaign_pks = all_campaign_pks - set(campaign_pks)

        all_tasks = Task.objects.all()
        tasks_to_delete = []

        for task in all_tasks:
            campaign_id = task.payload.get("campaign_id")
            if campaign_id is not None:
                if campaign_id in campaign_pks or campaign_id not in remaining_campaign_pks:
                    tasks_to_delete.append(task.pk)

        self.stdout.write(f"Found {len(tasks_to_delete)} tasks pointing to deleted/nonexistent campaigns.")

        if dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN: No database changes were made."))
            return

        with transaction.atomic():
            # Delete tasks
            deleted_tasks_count, _ = Task.objects.filter(pk__in=tasks_to_delete).delete()
            self.stdout.write(f"Deleted {deleted_tasks_count} task(s).")

            # Delete campaigns
            campaign_count = target_campaigns.count()
            total_deleted, detail_dict = target_campaigns.delete()
            self.stdout.write(f"Deleted {campaign_count} campaign(s) (total cascade items deleted: {total_deleted}).")


            # Reset is_freemium to False on preserved user campaigns
            updated_campaigns_count = campaigns_to_keep_but_reset.update(is_freemium=False)
            self.stdout.write(f"Updated {updated_campaigns_count} preserved campaign(s) to is_freemium=False.")

        self.stdout.write(self.style.SUCCESS("Purge complete."))
