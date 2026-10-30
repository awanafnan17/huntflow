# openoutreach/core/management/commands/reconcile_target_safety_hold.py
from django.core.management.base import BaseCommand
from openoutreach.core.models import Workspace, WorkspaceAutomationSettings, OperatorActionApproval
from openoutreach.crm.models import Deal, Lead, SuppressionRecord
from openoutreach.linkedin.models import LinkedInProfile
from openoutreach.linkedin.services.account_governor import (
    is_account_safe_for_provider_read,
    is_account_safe_for_provider_write,
)


class Command(BaseCommand):
    help = "Canonically reconcile a target-scoped redirect loop safety hold on a workspace."

    def add_arguments(self, parser):
        parser.add_argument(
            "--workspace-id",
            type=int,
            default=1,
            help="Workspace ID to reconcile (default: 1)",
        )
        parser.add_argument(
            "--workspace-slug",
            type=str,
            default="",
            help="Workspace slug to reconcile",
        )
        parser.add_argument(
            "--deal-id",
            type=int,
            default=None,
            help="Target Deal ID (optional, auto-detected if omitted)",
        )
        parser.add_argument(
            "--lead-id",
            type=int,
            default=None,
            help="Target Lead ID (optional, auto-detected if omitted)",
        )
        parser.add_argument(
            "--approval-id",
            type=int,
            default=None,
            help="Target OperatorActionApproval ID (optional, auto-detected if omitted)",
        )

    def handle(self, *args, **options):
        ws_id = options.get("workspace_id")
        ws_slug = options.get("workspace_slug")

        if ws_slug:
            ws = Workspace.objects.filter(slug=ws_slug).first()
        else:
            ws = Workspace.objects.filter(id=ws_id).first()

        if not ws:
            self.stderr.write(self.style.ERROR(f"Workspace not found (id={ws_id}, slug={ws_slug})"))
            return

        auto_st = WorkspaceAutomationSettings.get_for_workspace(ws)
        if not auto_st:
            self.stderr.write(self.style.ERROR(f"No WorkspaceAutomationSettings found for workspace {ws.id}"))
            return

        self.stdout.write(f"=== RECONCILING TARGET SAFETY HOLD FOR WORKSPACE #{ws.id} ({ws.name}) ===")
        self.stdout.write(f"Current soak_status: {auto_st.soak_status}")
        self.stdout.write(f"Current soak_stop_reason: {auto_st.soak_stop_reason}")
        self.stdout.write(f"Current provider_safety_state: {auto_st.provider_safety_state}")

        deal = Deal.objects.filter(id=options["deal_id"]).first() if options.get("deal_id") else None
        lead = Lead.objects.filter(id=options["lead_id"]).first() if options.get("lead_id") else None
        approval = (
            OperatorActionApproval.objects.filter(id=options["approval_id"]).first()
            if options.get("approval_id")
            else None
        )

        ok, reason = auto_st.reconcile_target_scoped_safety_hold(
            deal=deal,
            lead=lead,
            approval=approval,
        )

        if not ok:
            self.stderr.write(self.style.ERROR(f"Target safety hold reconciliation failed: {reason}"))
            return

        auto_st.refresh_from_db()
        self.stdout.write(self.style.SUCCESS(f"Reconciliation successful: {reason}"))
        self.stdout.write(f"New soak_status: {auto_st.soak_status}")
        self.stdout.write(f"New soak_stop_reason: '{auto_st.soak_stop_reason}'")
        self.stdout.write(f"Provider safety state: {auto_st.provider_safety_state}")

        # Check governor status
        prof = LinkedInProfile.objects.filter(workspace=ws, active=True).first()
        read_ok, read_reason = is_account_safe_for_provider_read(workspace=ws, linkedin_profile=prof)
        write_ok, write_reason = is_account_safe_for_provider_write(workspace=ws, linkedin_profile=prof)
        self.stdout.write(f"Governor Read Eligible: {read_ok} ({read_reason})")
        self.stdout.write(f"Governor Write Eligible: {write_ok} ({write_reason})")
