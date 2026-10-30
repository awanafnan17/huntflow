# openoutreach/core/management/commands/backfill_bettercontact.py
"""Management command to backfill BetterContact email lookup for qualified deals (Batch 5.7).

Identifies deals in the QUALIFIED state with no resolved email, runs the
BetterContact workflow, updates matching deal states to READY_TO_EMAIL on hits,
and outputs a summary. Never prints or leaks credentials.
"""
from __future__ import annotations

import logging
from django.core.management.base import BaseCommand
from django.db.models import Q


class Command(BaseCommand):
    help = "Backfill BetterContact email resolution for existing qualified deals."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be checked without making API calls or saving changes.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=50,
            help="Maximum number of deals to look up.",
        )
        parser.add_argument(
            "--campaign-id",
            type=int,
            default=None,
            help="Optional campaign ID to filter by.",
        )
        parser.add_argument(
            "--only-unresolved",
            type=str,
            default="true",
            help="Only process leads with empty api_email (default true).",
        )
        parser.add_argument(
            "--state",
            type=str,
            default="QUALIFIED",
            choices=["QUALIFIED", "PENDING", "READY_TO_CONNECT", "ALL"],
            help="Filter deals by state (default QUALIFIED). Use ALL to check all states.",
        )

    def handle(self, *args, **options):
        from openoutreach.crm.models import Deal, DealState
        from openoutreach.core.db.deals import set_profile_state

        dry_run = options["dry_run"]
        limit = options["limit"]
        campaign_id = options["campaign_id"]
        only_unresolved = options["only_unresolved"].lower() == "true"
        state_filter = options["state"]

        self.stdout.write("BetterContact Backfill Utility")
        self.stdout.write("------------------------------")
        if dry_run:
            self.stdout.write("MODE: DRY RUN (no API calls or database updates will be made)\n")

        state_map = {
            "QUALIFIED": DealState.QUALIFIED,
            "PENDING": DealState.PENDING,
            "READY_TO_CONNECT": DealState.READY_TO_CONNECT,
        }

        deals = Deal.objects.all()
        if state_filter != "ALL":
            mapped_state = state_map.get(state_filter)
            if mapped_state:
                deals = deals.filter(state=mapped_state)

        if only_unresolved:
            deals = deals.filter(Q(lead__api_email__isnull=True) | Q(lead__api_email=""))

        if campaign_id is not None:
            deals = deals.filter(campaign_id=campaign_id)

        deals = deals.select_related("lead", "campaign").order_by("creation_date")[:limit]

        total_checked = 0
        total_resolved = 0
        total_miss = 0
        total_error = 0

        for deal in deals:
            lead = deal.lead
            total_checked += 1
            self.stdout.write(f"Checking Deal #{deal.pk} | Lead: {lead.public_identifier} ({lead.linkedin_url})")
            
            if dry_run:
                # Mock evaluation check
                fields = lead.get_bettercontact_fields()
                self.stdout.write(f"  [DRY] Fields: first_name={fields['first_name']!r}, last_name={fields['last_name']!r}, company={fields['company']!r}")
                continue

            # Call the resolver
            success = lead.resolve_api_email()
            
            if success is True:
                total_resolved += 1
                self.stdout.write(f"  -> SUCCESS: Resolved email: {lead.api_email}")
                # Transition deal state to READY_TO_EMAIL
                from dataclasses import dataclass
                @dataclass
                class DummySession:
                    campaign: object
                session = DummySession(campaign=deal.campaign)
                set_profile_state(session, lead.public_identifier, DealState.READY_TO_EMAIL)
                self.stdout.write("  -> Transitioned Deal state to READY_TO_EMAIL")
            elif success is False:
                total_miss += 1
                self.stdout.write("  -> MISS: No email found.")
            else:
                total_error += 1
                self.stdout.write("  -> ERROR/UNAVAILABLE: Lookup failed or key missing.")

        self.stdout.write("\nSummary")
        self.stdout.write("-------")
        self.stdout.write(f"Total checked: {total_checked}")
        self.stdout.write(f"Total resolved: {total_resolved}")
        self.stdout.write(f"Total no email: {total_miss}")
        self.stdout.write(f"Total errors/unavailable: {total_error}")

        if total_checked == 0:
            self.stdout.write("\nNo deals matched the current filter criteria.")
            self.stdout.write(f"Current state filter: {state_filter}")
            
            self.stdout.write("\nUnresolved deals in database by state:")
            has_other_states = False
            for state_val, state_label in DealState.choices:
                count = Deal.objects.filter(state=state_val).filter(Q(lead__api_email__isnull=True) | Q(lead__api_email="")).count()
                self.stdout.write(f"  {state_val}: {count}")
                if count > 0 and state_val != state_filter:
                    has_other_states = True
            
            if has_other_states:
                self.stdout.write("\nSuggested command if you want to include PENDING leads:")
                self.stdout.write("  python manage.py backfill_bettercontact --dry-run --state PENDING --limit 5")
