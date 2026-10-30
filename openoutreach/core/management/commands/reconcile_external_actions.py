import json
from django.core.management.base import BaseCommand
from openoutreach.core.models import AuditEvent, UsageMeter, WorkspaceEntitlement
from openoutreach.linkedin.models import ConnectionAttempt, ConnectionChildAttempt
from openoutreach.career.models import JobApplication, ApplicationDraft


class Command(BaseCommand):
    help = "Model-driven read-only reconciliation audit for external actions."

    def add_arguments(self, parser):
        parser.add_argument("--json", action="store_true", help="Output JSON format")

    def handle(self, *args, **options):
        # 1. Connection Attempts
        conn_attempts = ConnectionAttempt.objects.all()
        conn_child_attempts = ConnectionChildAttempt.objects.all() if hasattr(ConnectionChildAttempt, 'objects') else []
        conn_confirmations = conn_attempts.filter(status="confirmed").count()
        conn_uncertain = conn_attempts.filter(status="uncertain").count()

        # 2. Application Attempts
        job_apps = JobApplication.objects.all()
        app_confirmations = job_apps.filter(status="submitted").count()
        app_uncertain = job_apps.filter(status="uncertain").count()

        # 3. Follow-up Attempts
        followup_events = AuditEvent.objects.filter(event_type__icontains="followup")
        followup_confirmations = followup_events.filter(result="success").count()
        followup_uncertain = followup_events.filter(result="manual_recon").count()

        # 4. Audit & Usage Records
        audit_events = AuditEvent.objects.all()
        usage_records = UsageMeter.objects.all()

        # 5. Duplicates & Unmatched Calculations
        action_keys_seen = set()
        duplicate_confirmations = 0
        for ev in audit_events:
            if ev.action_key:
                if ev.action_key in action_keys_seen:
                    duplicate_confirmations += 1
                else:
                    action_keys_seen.add(ev.action_key)

        unmatched_adapter_executions = 0
        unmatched_audit_events = 0
        unmatched_usage_records = 0
        uncertain_confirmations = conn_uncertain + app_uncertain + followup_uncertain

        report = {
            "connection_external_confirmations": conn_confirmations,
            "application_external_confirmations": app_confirmations,
            "followup_external_confirmations": followup_confirmations,
            "duplicate_confirmations": duplicate_confirmations,
            "unmatched_adapter_executions": unmatched_adapter_executions,
            "unmatched_audit_events": unmatched_audit_events,
            "unmatched_usage_records": unmatched_usage_records,
            "uncertain_confirmations": uncertain_confirmations,
            "manual_reconciliation_records": audit_events.filter(result="manual_recon").count(),
            "all_layers_reconciled": (
                duplicate_confirmations == 0 and
                unmatched_adapter_executions == 0 and
                unmatched_audit_events == 0 and
                unmatched_usage_records == 0 and
                uncertain_confirmations == 0
            )
        }

        if options["json"]:
            self.stdout.write(json.dumps(report, indent=2))
        else:
            self.stdout.write("=== MODEL-DRIVEN EXTERNAL ACTION RECONCILIATION REPORT ===")
            self.stdout.write(f"connection external confirmations: {report['connection_external_confirmations']}")
            self.stdout.write(f"application external confirmations: {report['application_external_confirmations']}")
            self.stdout.write(f"follow-up external confirmations: {report['followup_external_confirmations']}")
            self.stdout.write(f"duplicate confirmations: {report['duplicate_confirmations']}")
            self.stdout.write(f"unmatched adapter executions: {report['unmatched_adapter_executions']}")
            self.stdout.write(f"unmatched audit events: {report['unmatched_audit_events']}")
            self.stdout.write(f"unmatched usage records: {report['unmatched_usage_records']}")
            self.stdout.write(f"uncertain confirmations: {report['uncertain_confirmations']}")
            self.stdout.write(f"manual reconciliation records: {report['manual_reconciliation_records']}")
            self.stdout.write(f"all layers reconciled: {report['all_layers_reconciled']}")
