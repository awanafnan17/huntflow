# openoutreach/core/services/entitlements.py
"""Workspace entitlement checks and usage metering service for HuntFlow SaaS."""
from __future__ import annotations

from typing import Dict, Any
from django.utils import timezone
from django.core.exceptions import ValidationError

from openoutreach.core.models import (
    Workspace,
    SubscriptionPlan,
    WorkspaceEntitlement,
    UsageMeter,
)


def seed_default_subscription_plans() -> Dict[str, SubscriptionPlan]:
    """Seed system subscription plans (Starter, Professional, Enterprise)."""
    starter, _ = SubscriptionPlan.objects.get_or_create(
        slug="starter",
        defaults={
            "name": "Starter Plan",
            "description": "Essential outreach features for small teams and solo operators.",
            "monthly_price_usd": 49.00,
            "max_campaigns": 5,
            "max_monthly_leads": 500,
            "max_sender_identities": 2,
            "max_members": 3,
        },
    )
    pro, _ = SubscriptionPlan.objects.get_or_create(
        slug="professional",
        defaults={
            "name": "Professional Plan",
            "description": "Full SaaS campaign engine with high volume limits.",
            "monthly_price_usd": 149.00,
            "max_campaigns": 25,
            "max_monthly_leads": 5000,
            "max_sender_identities": 10,
            "max_members": 10,
        },
    )
    enterprise, _ = SubscriptionPlan.objects.get_or_create(
        slug="enterprise",
        defaults={
            "name": "Enterprise Plan",
            "description": "Unlimited campaigns, custom purposes, dedicated infrastructure.",
            "monthly_price_usd": 499.00,
            "max_campaigns": -1,
            "max_monthly_leads": 50000,
            "max_sender_identities": 50,
            "max_members": 50,
        },
    )
    return {"starter": starter, "professional": pro, "enterprise": enterprise}


def get_or_create_workspace_entitlement(workspace: Workspace) -> WorkspaceEntitlement:
    """Ensure workspace has an entitlement record, defaulting to Starter trial."""
    plans = seed_default_subscription_plans()
    entitlement, created = WorkspaceEntitlement.objects.get_or_create(
        workspace=workspace,
        defaults={
            "status": "trialing",
            "plan": plans["starter"],
            "current_period_start": timezone.now(),
        },
    )
    return entitlement


from django.db import transaction
from openoutreach.core.services.audit_service import record_audit_event


def get_entitlement_decision(workspace: Workspace, metric_name: str, requested_quantity: int = 1) -> Dict[str, Any]:
    """Return a structured decision dict for workspace entitlement."""
    entitlement = get_or_create_workspace_entitlement(workspace)
    status = entitlement.status
    plan = entitlement.plan

    if status in ["expired", "suspended", "canceled", "cancelled"]:
        return {
            "allowed": False,
            "reason_code": f"subscription_{status}",
            "current_usage": 0,
            "limit": 0,
            "subscription_status": status,
        }

    if status == "past_due" and metric_name in ["campaigns", "monthly_leads", "members"]:
        return {
            "allowed": False,
            "reason_code": "subscription_past_due",
            "current_usage": 0,
            "limit": 0,
            "subscription_status": status,
        }

    if not plan:
        return {
            "allowed": True,
            "reason_code": "unlimited",
            "current_usage": 0,
            "limit": -1,
            "subscription_status": status,
        }

    limit = -1
    current_usage = 0

    if metric_name == "campaigns":
        limit = plan.max_campaigns
        current_usage = workspace.campaigns.count()
    elif metric_name == "sender_identities":
        limit = plan.max_sender_identities
        current_usage = workspace.sender_identities.count()
    elif metric_name == "members":
        limit = plan.max_members
        current_usage = workspace.memberships.filter(status="active").count()
    elif metric_name == "monthly_leads":
        limit = plan.max_monthly_leads
        period_start = entitlement.current_period_start
        meter = UsageMeter.objects.filter(
            workspace=workspace, metric_name="leads_imported", billing_period_start=period_start
        ).first()
        current_usage = meter.count if meter else 0

    if limit != -1 and (current_usage + requested_quantity) > limit:
        return {
            "allowed": False,
            "reason_code": f"{metric_name}_limit_exceeded",
            "current_usage": current_usage,
            "limit": limit,
            "subscription_status": status,
        }

    return {
        "allowed": True,
        "reason_code": "within_limit",
        "current_usage": current_usage,
        "limit": limit,
        "subscription_status": status,
    }


def check_entitlement(workspace: Workspace, metric_name: str, requested_quantity: int = 1) -> bool:
    """
    Check if a workspace is entitled to perform an operation based on plan limits.
    Raises ValidationError if entitlement limit is exceeded.
    """
    decision = get_entitlement_decision(workspace, metric_name, requested_quantity)
    if not decision["allowed"]:
        record_audit_event(
            workspace=workspace,
            event_type="entitlement_denied",
            action_key=f"denied_{metric_name}_{workspace.id}_{timezone.now().strftime('%Y%m%d%H%M')}",
            metadata={"metric": metric_name, "reason": decision["reason_code"]},
        )
        raise ValidationError(f"Entitlement denied ({decision['reason_code']}). Status: {decision['subscription_status']}.")
    return True


@transaction.atomic
def consume_entitlement(
    *,
    workspace: Workspace,
    metric: str,
    quantity: int = 1,
    action_key: str = None,
    operation=None,
) -> Dict[str, Any]:
    """
    Atomically check entitlement, lock usage/entitlement, execute operation,
    and record usage when operation succeeds.
    Returns structured result dict.
    """
    entitlement = WorkspaceEntitlement.objects.select_for_update().filter(workspace=workspace).first()
    if not entitlement:
        entitlement = get_or_create_workspace_entitlement(workspace)

    decision = get_entitlement_decision(workspace, metric, quantity)
    if not decision["allowed"]:
        record_audit_event(
            workspace=workspace,
            event_type="entitlement_denied",
            action_key=f"denied_{metric}_{workspace.id}_{action_key or timezone.now().timestamp()}",
            metadata={"metric": metric, "reason": decision["reason_code"]},
        )
        raise ValidationError(f"Entitlement denied ({decision['reason_code']}). Status: {decision['subscription_status']}.")

    res = None
    if operation:
        res = operation()

    meter = record_usage(workspace, metric, quantity)
    current_usage = meter.count if meter else decision["current_usage"] + quantity

    return {
        "allowed": True,
        "reason_code": "allowed",
        "current_usage": current_usage,
        "limit": decision["limit"],
        "subscription_status": decision["subscription_status"],
        "operation_result": res,
    }


def record_usage(workspace: Workspace, metric_name: str, quantity: int = 1) -> UsageMeter:
    """Record usage against a workspace meter for the current billing period."""
    entitlement = get_or_create_workspace_entitlement(workspace)
    period_start = entitlement.current_period_start
    period_end = entitlement.current_period_end or (period_start + timezone.timedelta(days=30))

    meter, _ = UsageMeter.objects.select_for_update().get_or_create(
        workspace=workspace,
        metric_name=metric_name,
        billing_period_start=period_start,
        defaults={"billing_period_end": period_end},
    )
    meter.count += quantity
    meter.save()
    return meter


def get_workspace_usage_summary(workspace: Workspace) -> Dict[str, Any]:
    """Retrieve workspace plan limits and current usage metrics."""
    entitlement = get_or_create_workspace_entitlement(workspace)
    plan = entitlement.plan
    period_start = entitlement.current_period_start

    leads_meter = UsageMeter.objects.filter(
        workspace=workspace, metric_name="leads_imported", billing_period_start=period_start
    ).first()

    return {
        "workspace_slug": workspace.slug,
        "plan_name": plan.name if plan else "Free",
        "plan_slug": plan.slug if plan else "free",
        "status": entitlement.status,
        "campaigns_used": workspace.campaigns.count(),
        "campaigns_limit": plan.max_campaigns if plan else 0,
        "sender_identities_used": workspace.sender_identities.count(),
        "sender_identities_limit": plan.max_sender_identities if plan else 0,
        "members_used": workspace.memberships.filter(status="active").count(),
        "members_limit": plan.max_members if plan else 0,
        "leads_imported_this_period": leads_meter.count if leads_meter else 0,
        "leads_limit": plan.max_monthly_leads if plan else 0,
    }
