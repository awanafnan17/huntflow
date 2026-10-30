from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db.models import Q
from openoutreach.crm.models import Lead, Deal, DealState, LeadPipelineEvent
from openoutreach.linkedin.models import LinkedInProfile

class Command(BaseCommand):
    help = "Displays the Lead/Deal pipeline funnel counts and individual lead statuses."

    def add_arguments(self, parser):
        parser.add_argument("--campaign-id", type=int, help="Filter by campaign ID")
        parser.add_argument("--state", type=str, help="Filter by deal state")
        parser.add_argument("--failed-only", action="store_true", help="Filter by Failed state only")
        parser.add_argument("--unresolved-email-only", action="store_true", help="Filter by leads with unresolved emails only")
        parser.add_argument("--limit", type=int, default=100, help="Limit output list size")

    def handle(self, *args, **options):
        campaign_id = options.get("campaign_id")
        state = options.get("state")
        failed_only = options.get("failed_only")
        unresolved_email_only = options.get("unresolved_email_only")
        limit = options.get("limit")

        # Funnel count aggregates
        states = [choice[0] for choice in DealState.choices]
        counts = {}
        for s in states:
            q = Deal.objects.filter(state=s)
            if campaign_id:
                q = q.filter(campaign_id=campaign_id)
            counts[s] = q.count()

        self.stdout.write("=" * 60)
        self.stdout.write("CRITICAL CRM FUNNEL COUNT SUMMARY")
        self.stdout.write("=" * 60)
        for s, count in counts.items():
            self.stdout.write(f"  {s:<20}: {count}")
        self.stdout.write("-" * 60)

        # Filter the leads
        leads_qs = Lead.objects.all().order_by("-creation_date")

        if campaign_id:
            leads_qs = leads_qs.filter(deal__campaign_id=campaign_id)
        if state:
            leads_qs = leads_qs.filter(deal__state=state)
        if failed_only:
            leads_qs = leads_qs.filter(deal__state=DealState.FAILED)
        if unresolved_email_only:
            leads_qs = leads_qs.filter(Q(api_email="") | Q(api_email__isnull=True))

        total_matching = leads_qs.count()
        self.stdout.write(f"Total matching leads: {total_matching}")
        self.stdout.write("=" * 60)

        # Apply limit
        leads_qs = leads_qs[:limit]

        for lead in leads_qs:
            deal = lead.deal_set.first()
            campaign_name = deal.campaign.name if deal else "None"
            deal_state = deal.state if deal else "Discovered"
            email_status = lead.api_email if lead.api_email else "Unresolved"
            failure_outcome = f"{deal.outcome}: {deal.reason}" if (deal and (deal.outcome or deal.reason)) else "None"

            # Last manual event
            last_event = LeadPipelineEvent.objects.filter(lead=lead).order_by("-created_at").first()
            if last_event:
                operator_username = last_event.operator.username if last_event.operator else "System"
                last_event_str = f"{last_event.action} by {operator_username} on {last_event.created_at.strftime('%Y-%m-%d %H:%M:%S')} (State: {last_event.previous_state} -> {last_event.new_state})"
            else:
                last_event_str = "None"

            # Eligibility summary
            is_self_profile = LinkedInProfile.objects.filter(self_lead=lead).exists()
            is_disqualified = lead.disqualified or is_self_profile
            campaign_is_active = False
            if deal:
                campaign_is_active = deal.campaign.is_active

            el_list = []
            if bool(lead.api_email) and campaign_is_active and deal and deal.state == DealState.READY_TO_EMAIL and not is_disqualified:
                el_list.append("Email")
            if campaign_is_active and deal and (deal.state in (DealState.READY_TO_CONNECT, DealState.QUALIFIED) and not lead.api_email) and not is_disqualified:
                el_list.append("Connect")
            if campaign_is_active and deal and deal.state == DealState.CONNECTED and not is_disqualified:
                el_list.append("Follow-up")
            if campaign_is_active and deal and deal.state == DealState.FAILED and not is_disqualified:
                el_list.append("Retry")
            
            eligibility_str = ", ".join(el_list) if el_list else "None"
            
            self.stdout.write(f"Lead: {lead.public_identifier}")
            self.stdout.write(f"  Name: {lead.first_name} {lead.last_name}".strip() or "  Name: Not available")
            self.stdout.write(f"  Title: {lead.current_title or 'Not available'}")
            self.stdout.write(f"  Company: {lead.company or 'Not available'}")
            self.stdout.write(f"  Campaign: {campaign_name}")
            self.stdout.write(f"  Deal State: {deal_state}")
            self.stdout.write(f"  Email: {email_status}")
            self.stdout.write(f"  Failure/Outcome: {failure_outcome}")
            self.stdout.write(f"  Last Pipeline Event: {last_event_str}")
            self.stdout.write(f"  Eligibility: {eligibility_str}")
            self.stdout.write("-" * 60)
