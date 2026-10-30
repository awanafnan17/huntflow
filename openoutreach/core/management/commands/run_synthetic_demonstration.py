import json
import hashlib
import uuid
from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from openoutreach.core.models import (
    Workspace,
    WorkspaceMembership,
    Campaign,
    CampaignPurpose,
    CampaignPurposeSequenceTemplate,
    SenderIdentity,
    SenderIdentityAllowedPurpose,
    MessageSequenceStep,
    CampaignMessageDraft,
    AuditEvent,
    SyntheticDemonstrationRun,
)
from openoutreach.core.services.workspace_membership import create_workspace_with_owner
from openoutreach.core.services.campaign_wizard import create_campaign_from_wizard
from openoutreach.core.services.campaign_generator import (
    generate_draft_for_deal_step,
    review_campaign_message_draft,
)
from openoutreach.core.services.audit_service import record_audit_event
from openoutreach.crm.models.lead import (
    Lead,
    SuppressionRecord,
    TargetImport,
    TargetImportMapping,
    TargetImportRow,
)
from openoutreach.crm.models.deal import Deal, DealState

User = get_user_model()


class Command(BaseCommand):
    help = "Runs a real synthetic demonstration against the local development database."

    def add_arguments(self, parser):
        parser.add_argument(
            "--reset-synthetic-workspace",
            action="store_true",
            help="Purge existing synthetic demonstration run and recreate clean synthetic workspace.",
        )

    def handle(self, *args, **options):
        self.stdout.write("Running Synthetic Demonstration on Development Database...")

        # 1. Admin or demo user
        demo_user = User.objects.filter(is_superuser=True).first()
        if not demo_user:
            from django.core.management.base import CommandError
            raise CommandError("Create your own local superuser before running the synthetic demonstration.")

        ws = Workspace.objects.filter(slug="huntflow-synthetic-demo").first()

        if options.get("reset_synthetic_workspace") and ws:
            self.stdout.write("Resetting synthetic workspace...")
            AuditEvent.objects.filter(workspace=ws).delete()
            CampaignMessageDraft.objects.filter(workspace=ws).delete()
            Deal.objects.filter(campaign__workspace=ws).delete()
            Lead.objects.filter(workspace=ws).delete()
            Campaign.objects.filter(workspace=ws).delete()
            SenderIdentity.objects.filter(workspace=ws).delete()
            SyntheticDemonstrationRun.objects.filter(workspace=ws).delete()

        # 2. Idempotent workspace creation
        if not ws:
            ws = create_workspace_with_owner(
                name="HuntFlow Synthetic Demo",
                slug="huntflow-synthetic-demo",
                owner_user=demo_user,
            )

        run_key = f"synth_run_{int(timezone.now().timestamp())}_{uuid.uuid4().hex[:6]}"
        demo_run = SyntheticDemonstrationRun.objects.create(
            run_key=run_key,
            workspace=ws,
            status="running",
        )

        with transaction.atomic():
            # 3. Create Custom Campaign Purpose
            custom_purpose, _ = CampaignPurpose.objects.get_or_create(
                workspace=ws,
                slug="custom_demo_purpose",
                defaults={
                    "name": "Custom Demo Purpose",
                    "objective": "Demonstrate custom tenant campaign workflows",
                    "context_schema": {"required_fields": ["company_name", "audit_scope"]},
                },
            )
            if not custom_purpose.sequence_templates.exists():
                CampaignPurposeSequenceTemplate.objects.create(
                    campaign_purpose=custom_purpose,
                    sequence_number=1,
                    channel="linkedin",
                    step_type="connection_note",
                    delay_hours=0,
                    length_limit=300,
                )

            # 4. Pending preview-only Sender Identity
            sender = SenderIdentity.objects.filter(workspace=ws, email="demo_sender@synthetic-demo.invalid").first()
            if sender and not sender.is_synthetic:
                raise RuntimeError("Collision detected: Real customer sender identity matches synthetic email.")
            if not sender:
                sender = SenderIdentity.objects.create(
                    workspace=ws,
                    email="demo_sender@synthetic-demo.invalid",
                    display_name="Demo Pending Sender",
                    sender_type="individual",
                    verification_status="pending",
                    is_active=True,
                    is_synthetic=True,
                    synthetic_run=demo_run,
                )

            purposes = {
                "job_outreach": CampaignPurpose.objects.get(slug="job_outreach", is_system=True),
                "geosentry_mining": CampaignPurpose.objects.get(slug="geosentry_mining", is_system=True),
                "freelance_clients": CampaignPurpose.objects.get(slug="freelance_clients", is_system=True),
                "masters_scholarship": CampaignPurpose.objects.get(slug="masters_scholarship", is_system=True),
                "custom_demo_purpose": custom_purpose,
            }

            synthetic_contexts = {
                "job_outreach": {
                    "recipient_name": "Synthetic CEO",
                    "recipient_role": "Chief Executive Officer",
                    "company_name": "Synthetic Tech Ltd",
                    "reason_for_connecting": "Impressed by scaling AI infra",
                    "verified_candidate_facts": ["10 years backend engineering"],
                },
                "geosentry_mining": {
                    "recipient_name": "Synthetic Mine Director",
                    "recipient_role": "Exploration Manager",
                    "company_name": "Synthetic Minerals Corp",
                    "project_or_asset": "Goldfield Asset",
                    "jurisdiction": "Nevada",
                    "commodity": "Gold",
                    "verified_public_context": ["Technical Report 2025"],
                    "geosentry_service": "Desktop Screening Report",
                },
                "freelance_clients": {
                    "recipient_name": "Synthetic Business Owner",
                    "recipient_role": "Founder",
                    "business_name": "Synthetic Retail Inc",
                    "selected_service": "Full-Stack SaaS Consulting",
                    "verified_business_context": ["E-commerce SaaS"],
                    "verified_portfolio_facts": ["Shipped 5 SaaS platforms"],
                },
                "masters_scholarship": {
                    "professor_name": "Dr. Synthetic Supervisor",
                    "university": "Synthetic University",
                    "department": "Computer Science",
                    "research_area": "Autonomous Agents",
                    "verified_publication_or_project": "Agentic AI Paper 2026",
                    "verified_candidate_academic_facts": ["BSc CS 3.95 GPA"],
                    "intended_intake": "Fall 2026",
                },
                "custom_demo_purpose": {
                    "company_name": "Synthetic Audit Enterprise",
                    "audit_scope": "Full System Security & Compliance Audit",
                },
            }

            demo_summary = []

            for slug, purpose in purposes.items():
                sid = transaction.savepoint()
                try:
                    camp = Campaign.objects.filter(workspace=ws, campaign_purpose=purpose, name=f"Synthetic {purpose.name} Campaign").first()
                    if camp and not camp.is_synthetic:
                        raise RuntimeError(f"Collision detected: Real customer campaign matches '{camp.name}'.")
                    if not camp:
                        camp = Campaign.objects.create(
                            workspace=ws,
                            campaign_purpose=purpose,
                            name=f"Synthetic {purpose.name} Campaign",
                            campaign_type=purpose.slug,
                            sender_identity=sender,
                            owner=demo_user,
                            is_synthetic=True,
                            synthetic_run=demo_run,
                        )
                        record_audit_event(
                            workspace=ws,
                            event_type="campaign_created",
                            actor=demo_user,
                            target=camp,
                            action_key=f"camp_create_{ws.id}_{purpose.id}_{slug}",
                            metadata={"campaign_name": camp.name},
                        )

                    # Ensure sequence steps exist
                    if not camp.sequence_steps.exists():
                        templates = CampaignPurposeSequenceTemplate.objects.filter(campaign_purpose=purpose)
                        for tmpl in templates:
                            step, created = MessageSequenceStep.objects.get_or_create(
                                campaign=camp,
                                sequence_number=tmpl.sequence_number,
                                defaults={
                                    "channel": tmpl.channel,
                                    "step_type": tmpl.step_type,
                                    "delay_hours": tmpl.delay_hours,
                                    "length_limit": tmpl.length_limit,
                                },
                            )
                            if created:
                                record_audit_event(
                                    workspace=ws,
                                    event_type="sequence_step_created",
                                    actor=demo_user,
                                    target=step,
                                    action_key=f"seq_inst_{camp.id}_{tmpl.sequence_number}",
                                    metadata={"step_id": step.id},
                                )

                    lead_li_url = f"https://linkedin.com/in/huntflow-synthetic-{slug}"
                    lead = Lead.objects.filter(workspace=ws, linkedin_url=lead_li_url).first()
                    if lead and not lead.is_synthetic:
                        raise RuntimeError(f"Collision detected: Real customer lead matches '{lead_li_url}'.")
                    if not lead:
                        lead = Lead.objects.create(
                            workspace=ws,
                            public_identifier=f"synth_lead_{slug}",
                            linkedin_url=lead_li_url,
                            first_name="Synthetic",
                            last_name=slug.title(),
                            company="Synthetic Corp",
                            api_email=f"target-{slug}@synthetic-demo.invalid",
                            is_synthetic=True,
                            synthetic_run=demo_run,
                        )
                        record_audit_event(
                            workspace=ws,
                            event_type="target_imported",
                            actor=demo_user,
                            target=lead,
                            action_key=f"target_create_{ws.id}_{lead.public_identifier}",
                            metadata={"lead_id": lead.id},
                        )

                    deal = Deal.objects.filter(campaign=camp, lead=lead).first()
                    if deal and not deal.is_synthetic:
                        raise RuntimeError("Collision detected: Real customer deal matches synthetic pair.")
                    if not deal:
                        deal = Deal.objects.create(
                            campaign=camp,
                            lead=lead,
                            state=DealState.QUALIFIED,
                            is_synthetic=True,
                            synthetic_run=demo_run,
                        )

                    context = synthetic_contexts[slug]

                    for step in camp.sequence_steps.all():
                        draft = generate_draft_for_deal_step(
                            deal, step, context, user=demo_user, is_synthetic=True, synthetic_run=demo_run
                        )
                        if step.sequence_number == 1 and draft.status == "draft":
                            review_campaign_message_draft(draft, action="approve", reviewer=demo_user)

                    demo_summary.append({
                        "campaign": camp.name,
                        "purpose": purpose.slug,
                        "steps_count": camp.sequence_steps.count(),
                        "drafts_count": CampaignMessageDraft.objects.filter(campaign=camp).count(),
                    })
                    transaction.savepoint_commit(sid)
                except Exception as exc:
                    transaction.savepoint_rollback(sid)
                    self.stderr.write(f"Error processing campaign '{slug}': {exc}")
                    raise exc

            demo_run.status = "completed"
            demo_run.completed_at = timezone.now()
            demo_run.save()

            record_audit_event(
                workspace=ws,
                event_type="synthetic_demo_executed",
                actor=demo_user,
                target=demo_run,
                action_key=f"synth_demo_{demo_run.run_key}",
                metadata={"summary": demo_summary},
            )

        self.stdout.write(self.style.SUCCESS("Synthetic Demonstration Completed!"))
        self.stdout.write(json.dumps(demo_summary, indent=2))

        # Read-only Reconciliation Audit Summary
        runs_count = SyntheticDemonstrationRun.objects.filter(workspace=ws).count()
        campaigns_count = Campaign.objects.filter(workspace=ws, is_synthetic=True).count()
        leads_count = Lead.objects.filter(workspace=ws, is_synthetic=True).count()
        deals_count = Deal.objects.filter(campaign__workspace=ws, is_synthetic=True).count()
        drafts = list(CampaignMessageDraft.objects.filter(workspace=ws, is_synthetic=True))
        drafts_count = len(drafts)
        reviewed_drafts_count = len([d for d in drafts if d.status == "approved"])

        gen_events_count = AuditEvent.objects.filter(workspace=ws, event_type="draft_generated").count()
        style_events_count = AuditEvent.objects.filter(workspace=ws, event_type="draft_style_validated").count()
        claims_events_count = AuditEvent.objects.filter(workspace=ws, event_type="draft_claims_validated").count()
        review_events_count = AuditEvent.objects.filter(workspace=ws, event_type="draft_review_approved").count()
        demo_events_count = AuditEvent.objects.filter(workspace=ws, event_type="synthetic_demo_executed").count()

        sendable_count = CampaignMessageDraft.objects.filter(workspace=ws, is_synthetic=True, sendable=True).count()
        ext_auth_count = CampaignMessageDraft.objects.filter(workspace=ws, is_synthetic=True, external_action_authorized=True).count()
        non_draft_mode_count = CampaignMessageDraft.objects.filter(workspace=ws, is_synthetic=True, draft_mode=False).count()

        reconciliation_report = {
            "synthetic_runs_count": runs_count,
            "synthetic_campaigns_count": campaigns_count,
            "synthetic_leads_count": leads_count,
            "synthetic_deals_count": deals_count,
            "synthetic_drafts_count": drafts_count,
            "reviewed_drafts_count": reviewed_drafts_count,
            "audit_events": {
                "total_workspace_events": AuditEvent.objects.filter(workspace=ws).count(),
                "generation_events_count": gen_events_count,
                "style_validation_events_count": style_events_count,
                "claims_validation_events_count": claims_events_count,
                "review_events_count": review_events_count,
                "demonstration_events_count": demo_events_count,
            },
            "safety_invariants": {
                "sendable_count": sendable_count,
                "external_action_authorized_count": ext_auth_count,
                "non_draft_mode_count": non_draft_mode_count,
            },
        }

        self.stdout.write("\n=== SYNTHETIC RECONCILIATION AUDIT REPORT ===")
        self.stdout.write(json.dumps(reconciliation_report, indent=2))

        # Per-Draft Completeness Assertions
        for d in drafts:
            assert d.purpose_version is not None, f"Draft {d.id} missing purpose_version"
            assert d.prompt_version is not None, f"Draft {d.id} missing prompt_version"
            assert d.content_checksum, f"Draft {d.id} missing content_checksum"
            has_gen = AuditEvent.objects.filter(
                workspace=ws, event_type="draft_generated", action_key=f"draft_gen_{d.deal_id}_{d.sequence_step_id}"
            ).exists()
            assert has_gen, f"Draft {d.id} missing draft_generated audit event"
            has_style = AuditEvent.objects.filter(
                workspace=ws, event_type="draft_style_validated", action_key=f"style_val_{d.deal_id}_{d.sequence_step_id}"
            ).exists()
            assert has_style, f"Draft {d.id} missing style validation audit event"
            has_claims = AuditEvent.objects.filter(
                workspace=ws, event_type="draft_claims_validated", action_key=f"claims_val_{d.deal_id}_{d.sequence_step_id}"
            ).exists()
            assert has_claims, f"Draft {d.id} missing claims validation audit event"
            if d.status == "approved":
                rev_count = AuditEvent.objects.filter(
                    workspace=ws, event_type="draft_review_approved", action_key=f"draft_review_{d.id}_approve"
                ).count()
                assert rev_count == 1, f"Draft {d.id} has {rev_count} review events instead of exactly 1"

        assert drafts_count == 14, f"Expected 14 synthetic drafts, got {drafts_count}"
        assert gen_events_count == 14, f"Expected 14 draft_generated events, got {gen_events_count}"
        assert style_events_count == 14, f"Expected 14 style validation events, got {style_events_count}"
        assert claims_events_count == 14, f"Expected 14 claims validation events, got {claims_events_count}"
        assert review_events_count == 5, f"Expected 5 review events, got {review_events_count}"

        assert sendable_count == 0, "Safety violation: sendable synthetic draft found!"
        assert ext_auth_count == 0, "Safety violation: externally authorized synthetic draft found!"
        assert non_draft_mode_count == 0, "Safety violation: non-Draft Mode synthetic draft found!"
