# openoutreach/dashboard/saas_views.py
import json
import hashlib
from typing import Dict, Any
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib import messages
from django.core.exceptions import ValidationError, PermissionDenied
from django.http import JsonResponse, HttpResponse
from django.views.decorators.http import require_POST
from django.utils import timezone

from django.contrib.auth import get_user_model
from openoutreach.core.models import (
    Workspace,
    WorkspaceMembership,
    Campaign,
    CampaignPurpose,
    SenderIdentity,
    SenderIdentityAllowedPurpose,
    MessageSequenceStep,
    CampaignMessageDraft,
    AuditEvent,
    SubscriptionPlan,
    WorkspaceEntitlement,
    UsageMeter,
    WorkspaceInvitation,
    PasswordResetToken,
    EmailVerificationToken,
)
from openoutreach.core.permissions import has_workspace_permission, get_workspace_object_or_404
from openoutreach.core.services.workspace_membership import (
    create_workspace_with_owner,
)
from openoutreach.core.services.campaign_wizard import (
    create_campaign_from_wizard,
)
from openoutreach.core.services.entitlements import (
    get_or_create_workspace_entitlement,
    get_workspace_usage_summary,
)
from openoutreach.core.services.workspace_invitation import (
    create_invitation,
    accept_invitation,
    revoke_invitation,
    verify_email_token,
    generate_password_reset_token,
    confirm_password_reset,
)
from openoutreach.core.services.billing_provider import (
    get_billing_provider,
)
from openoutreach.core.services.campaign_generator import (
    generate_draft_for_deal_step,
    review_campaign_message_draft,
)
from openoutreach.crm.models.lead import (
    Lead,
    SuppressionRecord,
    TargetImport,
    TargetImportMapping,
    TargetImportRow,
)
from openoutreach.crm.models.deal import Deal, DealState
from openoutreach.crm.services.import_service import (
    upload_target_import,
    set_import_mapping,
    preview_and_validate_import,
    confirm_and_execute_import,
)


def _get_active_membership(user, workspace_id: int) -> WorkspaceMembership:
    """Ensure user is an active member of the workspace."""
    if not user.is_authenticated:
        raise PermissionDenied("Authentication required.")
    
    membership = WorkspaceMembership.objects.filter(workspace_id=workspace_id, user=user).first()
    if not membership or membership.status != "active":
        raise PermissionDenied("User is not an active member of this workspace.")
    return membership


# ── 1. Onboarding Views ──────────────────────────────────────────────────

def onboarding_start(request):
    """Step 1: Workspace creation."""
    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        slug = request.POST.get("slug", "").strip()
        if not name or not slug:
            messages.error(request, "Workspace name and slug are required.")
            return redirect("dashboard:onboarding_start")

        try:
            ws = create_workspace_with_owner(name=name, slug=slug, owner_user=request.user)
            AuditEvent.objects.create(workspace=ws, event_type="workspace_created", actor=request.user)
            return redirect("dashboard:onboarding_sender", workspace_id=ws.id)
        except Exception as exc:
            messages.error(request, f"Workspace creation failed: {exc}")
            return redirect("dashboard:onboarding_start")

    return render(request, "dashboard/onboarding_start.html")


def onboarding_sender(request, workspace_id):
    """Step 2: Sender setup."""
    membership = _get_active_membership(request.user, workspace_id)
    ws = membership.workspace

    if request.method == "POST":
        display_name = request.POST.get("display_name", "").strip()
        email = request.POST.get("email", "").strip()
        sender_type = request.POST.get("sender_type", "individual")

        if not display_name or not email:
            messages.error(request, "Display name and email are required.")
            return redirect("dashboard:onboarding_sender", workspace_id=ws.id)

        sender = SenderIdentity.objects.create(
            workspace=ws,
            display_name=display_name,
            email=email,
            sender_type=sender_type,
            verification_status="pending",
            is_active=True,
        )
        # Link system purposes
        for purpose in CampaignPurpose.objects.filter(is_system=True):
            SenderIdentityAllowedPurpose.objects.get_or_create(sender_identity=sender, campaign_purpose=purpose)

        AuditEvent.objects.create(workspace=ws, event_type="sender_identity_created", actor=request.user, payload={"sender_id": sender.id})
        return redirect("dashboard:onboarding_purpose", workspace_id=ws.id)

    senders = SenderIdentity.objects.filter(workspace=ws)
    return render(request, "dashboard/onboarding_sender.html", {"workspace": ws, "senders": senders})


def onboarding_purpose(request, workspace_id):
    """Step 3: Purpose selection."""
    membership = _get_active_membership(request.user, workspace_id)
    ws = membership.workspace
    purposes = CampaignPurpose.objects.filter(is_active=True).filter(workspace__isnull=True) | CampaignPurpose.objects.filter(workspace=ws, is_active=True)

    if request.method == "POST":
        purpose_id = request.POST.get("purpose_id")
        purpose = get_object_or_404(CampaignPurpose, pk=purpose_id)
        sender = SenderIdentity.objects.filter(workspace=ws).first()

        campaign = create_campaign_from_wizard(
            workspace=ws,
            campaign_purpose=purpose,
            sender_identity=sender,
            name=f"Initial {purpose.name} Campaign",
            user=request.user,
        )
        AuditEvent.objects.create(workspace=ws, event_type="initial_campaign_created", actor=request.user, payload={"campaign_id": campaign.id})
        messages.success(request, f"Campaign '{campaign.name}' created!")
        return redirect("dashboard:campaign_detail", workspace_id=ws.id, campaign_id=campaign.id)

    return render(request, "dashboard/onboarding_purpose.html", {"workspace": ws, "purposes": purposes})


# ── 2. Sender Identity UI Views ──────────────────────────────────────────

def sender_list(request, workspace_id):
    membership = _get_active_membership(request.user, workspace_id)
    senders = SenderIdentity.objects.filter(workspace=membership.workspace)
    return render(request, "dashboard/sender_list.html", {"workspace": membership.workspace, "senders": senders})


def sender_create(request, workspace_id):
    membership = _get_active_membership(request.user, workspace_id)
    ws = membership.workspace
    purposes = CampaignPurpose.objects.filter(is_active=True)

    if request.method == "POST":
        display_name = request.POST.get("display_name", "").strip()
        email = request.POST.get("email", "").strip()
        sender_type = request.POST.get("sender_type", "individual")
        linkedin_url = request.POST.get("linkedin_profile_url", "").strip()
        company_or_brand = request.POST.get("company_or_brand", "").strip()
        signature = request.POST.get("signature", "").strip()

        sender = SenderIdentity.objects.create(
            workspace=ws,
            display_name=display_name,
            email=email,
            sender_type=sender_type,
            linkedin_profile_url=linkedin_url,
            company_or_brand=company_or_brand,
            signature=signature,
            verification_status="pending",
            is_active=True,
        )

        selected_purposes = request.POST.getlist("allowed_purposes")
        for pid in selected_purposes:
            p = CampaignPurpose.objects.filter(pk=pid).first()
            if p:
                SenderIdentityAllowedPurpose.objects.get_or_create(sender_identity=sender, campaign_purpose=p)

        AuditEvent.objects.create(workspace=ws, event_type="sender_identity_created", actor=request.user)
        messages.success(request, f"Sender Identity '{sender.display_name}' created (pending verification).")
        return redirect("dashboard:sender_list", workspace_id=ws.id)

    return render(request, "dashboard/sender_form.html", {"workspace": ws, "purposes": purposes})


# ── 3. Target Import Views ──────────────────────────────────────────────

def import_list(request, workspace_id):
    membership = _get_active_membership(request.user, workspace_id)
    imports = TargetImport.objects.filter(workspace=membership.workspace)
    return render(request, "dashboard/import_list.html", {"workspace": membership.workspace, "imports": imports})


def import_upload(request, workspace_id):
    membership = _get_active_membership(request.user, workspace_id)
    ws = membership.workspace
    campaigns = Campaign.objects.filter(workspace=ws)

    if request.method == "POST":
        name = request.POST.get("name", "CSV Import").strip()
        campaign_id = request.POST.get("campaign_id")
        csv_file = request.FILES.get("csv_file")

        if not csv_file:
            messages.error(request, "Please select a CSV file.")
            return redirect("dashboard:import_upload", workspace_id=ws.id)

        campaign = Campaign.objects.filter(pk=campaign_id, workspace=ws).first() if campaign_id else None
        csv_text = csv_file.read().decode("utf-8", errors="replace")

        imp = upload_target_import(
            workspace=ws,
            name=name,
            file_name=csv_file.name,
            csv_content=csv_text,
            campaign=campaign,
            user=request.user,
        )
        return redirect("dashboard:import_mapping", workspace_id=ws.id, import_id=imp.id)

    return render(request, "dashboard/import_upload.html", {"workspace": ws, "campaigns": campaigns})


def import_mapping(request, workspace_id, import_id):
    membership = _get_active_membership(request.user, workspace_id)
    imp = get_workspace_object_or_404(TargetImport, import_id, membership.workspace)

    if request.method == "POST":
        mapping = {
            "first_name": request.POST.get("map_first_name", "").strip(),
            "last_name": request.POST.get("map_last_name", "").strip(),
            "email": request.POST.get("map_email", "").strip(),
            "linkedin_url": request.POST.get("map_linkedin_url", "").strip(),
            "current_title": request.POST.get("map_current_title", "").strip(),
            "company": request.POST.get("map_company", "").strip(),
        }
        set_import_mapping(imp, mapping)
        return redirect("dashboard:import_preview", workspace_id=membership.workspace.id, import_id=imp.id)

    sample_row = imp.rows.first().raw_data if imp.rows.exists() else {}
    csv_headers = list(sample_row.keys())
    return render(request, "dashboard/import_mapping.html", {"workspace": membership.workspace, "import_obj": imp, "csv_headers": csv_headers})


def import_preview(request, workspace_id, import_id):
    membership = _get_active_membership(request.user, workspace_id)
    imp = get_workspace_object_or_404(TargetImport, import_id, membership.workspace)

    results = preview_and_validate_import(imp)

    if request.method == "POST":
        if not has_workspace_permission(request.user, membership.workspace, "campaign_manager"):
            raise PermissionDenied("Permission denied to confirm import.")
        confirm_and_execute_import(imp)
        AuditEvent.objects.create(workspace=membership.workspace, event_type="target_import_confirmed", actor=request.user, payload={"import_id": imp.id})
        messages.success(request, f"Import '{imp.name}' confirmed!")
        return redirect("dashboard:import_list", workspace_id=membership.workspace.id)

    return render(request, "dashboard/import_preview.html", {"workspace": membership.workspace, "import_obj": imp, "results": results, "rows": imp.rows.all()})


# ── 4. Draft Mode Review Views ──────────────────────────────────────────

def draft_list(request, workspace_id, campaign_id=None):
    membership = _get_active_membership(request.user, workspace_id)
    ws = membership.workspace

    drafts = CampaignMessageDraft.objects.filter(workspace=ws)
    if campaign_id:
        drafts = drafts.filter(campaign_id=campaign_id)

    return render(request, "dashboard/draft_list.html", {"workspace": ws, "drafts": drafts})


def draft_detail(request, workspace_id, draft_id):
    membership = _get_active_membership(request.user, workspace_id)
    draft = get_workspace_object_or_404(CampaignMessageDraft, draft_id, membership.workspace)

    if request.method == "POST":
        action = request.POST.get("action")
        reason = request.POST.get("rejection_reason", "")
        review_campaign_message_draft(draft, action=action, reviewer=request.user, rejection_reason=reason)
        AuditEvent.objects.create(
            workspace=membership.workspace,
            event_type="draft_reviewed",
            actor=request.user,
            payload={"draft_id": draft.id, "action": action},
        )
        messages.success(request, f"Draft action '{action}' performed. Draft remains non-sendable.")
        return redirect("dashboard:draft_detail", workspace_id=membership.workspace.id, draft_id=draft.id)

    return render(request, "dashboard/draft_detail.html", {"workspace": membership.workspace, "draft": draft})


# ── 5. Synthetic Demonstration Handler ──────────────────────────────────

def run_synthetic_demonstration(request, workspace_id):
    membership = _get_active_membership(request.user, workspace_id)
    ws = membership.workspace

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

    purposes = {
        "job_outreach": CampaignPurpose.objects.get(slug="job_outreach", is_system=True),
        "geosentry_mining": CampaignPurpose.objects.get(slug="geosentry_mining", is_system=True),
        "freelance_clients": CampaignPurpose.objects.get(slug="freelance_clients", is_system=True),
        "masters_scholarship": CampaignPurpose.objects.get(slug="masters_scholarship", is_system=True),
        "custom_demo_purpose": custom_purpose,
    }

    demo_results = []

    sender, _ = SenderIdentity.objects.get_or_create(
        workspace=ws,
        email="demo@synthetic.internal",
        defaults={
            "display_name": "Demo Preview Sender",
            "sender_type": "individual",
            "verification_status": "pending",
        },
    )

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
            "verified_business_context": ["B2B Consulting"],
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

    for slug, purpose in purposes.items():
        camp, _ = Campaign.objects.get_or_create(
            workspace=ws,
            campaign_purpose=purpose,
            name=f"Synthetic {purpose.name} Campaign",
            defaults={
                "campaign_type": purpose.slug,
                "sender_identity": sender,
                "owner": request.user,
            },
        )

        if not camp.sequence_steps.exists():
            templates = CampaignPurposeSequenceTemplate.objects.filter(campaign_purpose=purpose)
            for tmpl in templates:
                MessageSequenceStep.objects.get_or_create(
                    campaign=camp,
                    sequence_number=tmpl.sequence_number,
                    defaults={
                        "channel": tmpl.channel,
                        "step_type": tmpl.step_type,
                        "delay_hours": tmpl.delay_hours,
                        "length_limit": tmpl.length_limit,
                    },
                )

        lead, _ = Lead.objects.get_or_create(
            workspace=ws,
            public_identifier=f"synth_lead_{slug}",
            defaults={
                "first_name": "Synthetic",
                "last_name": slug.title(),
                "company": "Synthetic Corp",
                "linkedin_url": f"https://linkedin.com/in/synth_lead_{slug}",
            },
        )
        deal, _ = Deal.objects.get_or_create(campaign=camp, lead=lead, defaults={"state": DealState.QUALIFIED})
        step = camp.sequence_steps.first()

        context = synthetic_contexts[slug]
        draft = generate_draft_for_deal_step(deal, step, context, user=request.user)
        if draft.status == "draft":
            review_campaign_message_draft(draft, action="approve", reviewer=request.user)

        demo_results.append({
            "campaign": camp.name,
            "purpose": purpose.slug,
            "draft_id": draft.id,
            "draft_mode": draft.draft_mode,
            "sendable": draft.sendable,
            "checksum": draft.content_checksum[:16],
            "status": draft.status,
        })

    return JsonResponse({
        "status": "Demonstration completed successfully",
        "demo_results": demo_results,
        "all_drafts_non_sendable": all(not d["sendable"] for d in demo_results),
    })


# ── 6. Phase 5 Operations & Commercial Foundation Views ────────────────

def health_live_check(request):
    """Liveness probe: verifies the Django web process is responsive."""
    return JsonResponse({
        "status": "live",
        "service": "HuntFlow",
        "timestamp": timezone.now().isoformat(),
    })


def health_ready_check(request):
    """Readiness probe: validates database connectivity and redis accessibility without provider calls."""
    import os
    import socket
    from urllib.parse import urlparse
    from django.db import connections
    from django.conf import settings

    services = {
        "web": "ok",
        "database": "unknown",
        "redis": "unknown",
    }
    reasons = []

    # 1. Database Connectivity
    try:
        connection = connections["default"]
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        services["database"] = "ok"
    except Exception as e:
        services["database"] = "down"
        reasons.append("database_unavailable")

    # 2. Redis Connectivity (if configured or local)
    redis_url = os.environ.get("REDIS_URL", "")
    if redis_url:
        try:
            url = urlparse(redis_url)
            host = url.hostname or "127.0.0.1"
            port = url.port or 6379
            with socket.create_connection((host, port), timeout=2) as s:
                s.sendall(b"*1\r\n$4\r\nPING\r\n")
                resp = s.recv(1024)
                if b"+PONG" in resp or b"PONG" in resp:
                    services["redis"] = "ok"
                else:
                    services["redis"] = "down"
                    reasons.append("redis_ping_failed")
        except Exception:
            services["redis"] = "down"
            reasons.append("redis_unavailable")
    else:
        services["redis"] = "not_configured"

    # 3. Overall Readiness
    is_ready = services["web"] == "ok" and services["database"] == "ok" and (services["redis"] in ("ok", "not_configured"))
    status_code = 200 if is_ready else 503

    return JsonResponse({
        "status": "ready" if is_ready else "not_ready",
        "service": "HuntFlow",
        "services": services,
        "reasons": reasons if not is_ready else [],
        "timestamp": timezone.now().isoformat(),
    }, status=status_code)


def health_check(request):
    """Public health check endpoint returning JSON 200 OK."""
    return JsonResponse({
        "status": "healthy",
        "service": "HuntFlow SaaS Campaign Engine",
        "timestamp": timezone.now().isoformat(),
    })



def healthz_check(request):
    """
    Standard read-only health endpoint for HuntFlow web application.
    Validates web, database, daemon heartbeat, and local service availability without exposing secrets.
    """
    from django.db import connections
    from openoutreach.core.models import WorkspaceAutomationSettings
    import urllib.request
    import json

    services = {
        "web": "ok",
        "database": "unknown",
        "ollama": "unknown",
        "rundaemon": "unknown",
    }
    overall_status = "ok"

    # 1. DB check
    try:
        connection = connections["default"]
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        services["database"] = "ok"
    except Exception:
        services["database"] = "down"
        overall_status = "degraded"

    # 2. Ollama check (safe ping & model availability)
    ollama_model = None
    try:
        from openoutreach.core.llm import get_default_ollama_base_url
        ollama_base = get_default_ollama_base_url()
        req = urllib.request.Request(f"{ollama_base}/api/tags", headers={"User-Agent": "HuntFlowHealthCheck/1.0"})
        with urllib.request.urlopen(req, timeout=2) as resp:
            if resp.status == 200:
                data = json.loads(resp.read().decode())
                models = [m.get("name", "") for m in data.get("models", [])]
                if any("qwen" in m for m in models):
                    services["ollama"] = "ok"
                    ollama_model = next((m for m in models if "qwen" in m), "qwen2.5:1.5b")
                else:
                    services["ollama"] = "model_missing"
                    overall_status = "degraded"
            else:
                services["ollama"] = "degraded"
                overall_status = "degraded"
    except Exception:
        services["ollama"] = "unavailable"
        overall_status = "degraded"

    # 3. Rundaemon heartbeat check
    heartbeat_age_seconds = None
    try:
        from openoutreach.core.models import DaemonStatus
        st = WorkspaceAutomationSettings.objects.order_by("-updated_at").first()
        ds = DaemonStatus.objects.filter(pk=1).first()

        latest_hb = None
        if st and st.last_daemon_heartbeat:
            latest_hb = st.last_daemon_heartbeat
        if ds and ds.last_heartbeat:
            if latest_hb is None or ds.last_heartbeat > latest_hb:
                latest_hb = ds.last_heartbeat

        if latest_hb:
            age = (timezone.now() - latest_hb).total_seconds()
            heartbeat_age_seconds = round(age, 1)
            if age < 180:
                services["rundaemon"] = "ok"
            elif age < 600:
                services["rundaemon"] = "degraded"
                if overall_status == "ok":
                    overall_status = "degraded"
            else:
                services["rundaemon"] = "down"
                if overall_status == "ok":
                    overall_status = "degraded"
        else:
            services["rundaemon"] = "down"
            if overall_status == "ok":
                overall_status = "degraded"
    except Exception:
        services["rundaemon"] = "down"
        if overall_status == "ok":
            overall_status = "degraded"

    status_code = 200 if services["web"] == "ok" and services["database"] == "ok" else 503
    payload = {
        "status": overall_status,
        "services": services,
        "timestamp": timezone.now().isoformat(),
    }
    if heartbeat_age_seconds is not None:
        payload["heartbeat_age_seconds"] = heartbeat_age_seconds
    if ollama_model:
        payload["ollama_model"] = ollama_model

    return JsonResponse(payload, status=status_code)


def readiness_check(request):
    """Public readiness check endpoint validating DB Connectivity, Pending Migrations, and Configuration."""
    from django.db import connections
    from django.db.migrations.executor import MigrationExecutor
    from django.conf import settings

    # 1. Check DB Connectivity
    try:
        connection = connections["default"]
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception:
        return JsonResponse({"status": "not_ready", "reason_code": "database_unavailable"}, status=503)

    # 2. Check Pending Migrations
    try:
        executor = MigrationExecutor(connection)
        plan = executor.migration_plan(executor.loader.graph.leaf_nodes())
        if plan:
            return JsonResponse({"status": "not_ready", "reason_code": "migrations_pending"}, status=503)
    except Exception:
        return JsonResponse({"status": "not_ready", "reason_code": "dependency_unavailable"}, status=503)

    # 3. Check Configuration Requirements
    secret_key = getattr(settings, "SECRET_KEY", "")
    pepper = getattr(settings, "TOKEN_PEPPER", "")
    is_default_secret = secret_key == "openoutreach-local-dev-key-change-in-production"

    if not secret_key or is_default_secret or not pepper or len(pepper) < 32 or pepper == secret_key:
        return JsonResponse({"status": "not_ready", "reason_code": "configuration_missing"}, status=503)

    return JsonResponse({
        "status": "ready",
        "database": "connected",
        "timestamp": timezone.now().isoformat(),
    })


def workspace_members(request, workspace_id):
    membership = _get_active_membership(request.user, workspace_id)
    ws = membership.workspace

    members = WorkspaceMembership.objects.filter(workspace=ws).select_related("user")
    invitations = WorkspaceInvitation.objects.filter(workspace=ws, status="pending")

    return render(request, "dashboard/workspace_members.html", {
        "workspace": ws,
        "members": members,
        "invitations": invitations,
        "user_membership": membership,
    })


def workspace_invite(request, workspace_id):
    membership = _get_active_membership(request.user, workspace_id)
    ws = membership.workspace

    if not has_workspace_permission(request.user, ws, "members.manage"):
        raise PermissionDenied("Only workspace owners or admins can invite new members.")

    if request.method == "POST":
        email = request.POST.get("email", "").strip()
        role = request.POST.get("role", "campaign_manager").strip()

        try:
            invite, raw_token = create_invitation(workspace=ws, email=email, role=role, invited_by=request.user)
            messages.success(request, f"Invitation created for {email} ({role}). Token: {raw_token}")
            return redirect("dashboard:workspace_members", workspace_id=ws.id)
        except ValidationError as ve:
            messages.error(request, str(ve.message if hasattr(ve, 'message') else ve))
            return redirect("dashboard:workspace_members", workspace_id=ws.id)

    return redirect("dashboard:workspace_members", workspace_id=ws.id)


def workspace_revoke_invite(request, workspace_id, invite_id):
    membership = _get_active_membership(request.user, workspace_id)
    ws = membership.workspace

    if not has_workspace_permission(request.user, ws, "members.manage"):
        raise PermissionDenied("Only workspace owners or admins can revoke invitations.")

    try:
        revoke_invitation(invite_id, ws, actor=request.user)
        messages.success(request, "Invitation revoked.")
    except ValidationError as ve:
        messages.error(request, str(ve.message if hasattr(ve, 'message') else ve))

    return redirect("dashboard:workspace_members", workspace_id=ws.id)


def workspace_accept_invite(request, token_str):
    if not request.user.is_authenticated:
        return redirect(f"/dashboard/login/?next=/dashboard/invitations/accept/{token_str}/")

    try:
        membership = accept_invitation(token_str, request.user)
        messages.success(request, f"Joined workspace '{membership.workspace.name}' successfully as {membership.role}!")
        return redirect("dashboard:overview")
    except ValidationError as ve:
        messages.error(request, str(ve.message if hasattr(ve, 'message') else ve))
        return redirect("dashboard:overview")


def workspace_billing(request, workspace_id):
    membership = _get_active_membership(request.user, workspace_id)
    ws = membership.workspace

    plans = SubscriptionPlan.objects.filter(is_active=True)
    summary = get_workspace_usage_summary(ws)

    if request.GET.get("mock_checkout"):
        plan_slug = request.GET.get("plan", "professional")
        plan = SubscriptionPlan.objects.filter(slug=plan_slug).first()
        if plan:
            provider = get_billing_provider()
            provider.create_subscription(ws, plan)
            messages.success(request, f"Mock Checkout complete! Upgraded to {plan.name}.")
            return redirect("dashboard:workspace_billing", workspace_id=ws.id)

    return render(request, "dashboard/workspace_billing.html", {
        "workspace": ws,
        "summary": summary,
        "plans": plans,
    })


def user_verify_email(request):
    if request.method == "POST":
        token_str = request.POST.get("token", "").strip()
        try:
            tok = verify_email_token(token_str)
            messages.success(request, f"Email '{tok.email}' verified successfully!")
        except ValidationError as ve:
            messages.error(request, str(ve.message if hasattr(ve, 'message') else ve))
        return redirect("dashboard:overview")

    token_str = request.GET.get("token", "")
    return render(request, "dashboard/user_verify_email.html", {"token": token_str})


def user_password_reset(request):
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "request":
            username = request.POST.get("username", "").strip()
            user = get_user_model().objects.filter(username=username).first()
            if user:
                tok_obj, raw_token = generate_password_reset_token(user)
                messages.success(request, f"Password reset token generated for {username}: {raw_token}")
            else:
                messages.error(request, "User not found.")
            return redirect("dashboard:login")
        elif action == "confirm":
            token_str = request.POST.get("token", "").strip()
            new_password = request.POST.get("password", "").strip()
            try:
                confirm_password_reset(token_str, new_password)
                messages.success(request, "Password reset successfully! Please log in.")
                return redirect("dashboard:login")
            except ValidationError as ve:
                messages.error(request, str(ve.message if hasattr(ve, 'message') else ve))

    return render(request, "dashboard/user_password_reset.html")

