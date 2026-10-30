# openoutreach/core/management/commands/backfill_profile_identity.py
"""Management command to backfill Lead profile identity fields from existing stored data (Batch 5.9).

Fills first_name, last_name, and company from contact_info JSON or
profile_summary facts without making LinkedIn or LLM calls.
"""
from __future__ import annotations

import logging
from django.core.management.base import BaseCommand
from django.db.models import Q


class Command(BaseCommand):
    help = "Backfill Lead profile identity fields from existing stored data."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be updated without saving database changes.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=50,
            help="Maximum number of leads to process.",
        )
    def handle(self, *args, **options):
        from openoutreach.crm.models import Lead
        from openoutreach.crm.models.lead import extract_name_company_from_facts

        dry_run = options["dry_run"]
        limit = options["limit"]

        self.stdout.write("Profile Identity Backfill Utility")
        self.stdout.write("---------------------------------")
        if dry_run:
            self.stdout.write("MODE: DRY RUN (no database updates will be saved)\n")

        # Select leads with any empty identity fields
        leads = Lead.objects.filter(
            Q(first_name="") | Q(last_name="") | Q(company="") | Q(current_title="")
        ).order_by("creation_date")[:limit]

        total_processed = 0
        total_updated = 0

        for lead in leads:
            total_processed += 1
            
            # Extract from existing reliable sources
            ex_first = ""
            ex_last = ""
            ex_comp = ""
            
            # 1. contact_info JSON
            if lead.contact_info:
                ex_first = lead.contact_info.get("first_name") or ""
                ex_last = lead.contact_info.get("last_name") or ""
                ex_comp = lead.contact_info.get("company") or ""
            
            # 2. facts
            deal = lead.deal_set.first()
            if deal and deal.profile_summary and "facts" in deal.profile_summary:
                facts = deal.profile_summary["facts"]
                fn, ln, co = extract_name_company_from_facts(facts)
                if fn and not ex_first:
                    ex_first = fn
                if ln and not ex_last:
                    ex_last = ln
                if co and not ex_comp:
                    ex_comp = co

            # Determine proposed updates
            updates = {}
            if ex_first and not lead.first_name:
                updates["first_name"] = ex_first
            if ex_last and not lead.last_name:
                updates["last_name"] = ex_last
            if ex_comp and not lead.company:
                updates["company"] = ex_comp

            if updates:
                total_updated += 1
                self.stdout.write(f"Lead: {lead.public_identifier}")
                for field, val in updates.items():
                    self.stdout.write(f"  * Update {field}: '' -> '{val}'")
                
                if not dry_run:
                    for field, val in updates.items():
                        setattr(lead, field, val)
                    lead.save(update_fields=list(updates.keys()))
            else:
                self.stdout.write(f"Lead: {lead.public_identifier} - No new reliable identity data found in DB.")

        self.stdout.write("\nSummary")
        self.stdout.write("-------")
        self.stdout.write(f"Total processed: {total_processed}")
        self.stdout.write(f"Total updated: {total_updated}")
