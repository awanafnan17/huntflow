# openoutreach/core/services/billing_provider.py
"""Billing provider interface and mock implementation for HuntFlow SaaS subscription management."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Dict, Any, Optional
from django.utils import timezone

from openoutreach.core.models import Workspace, SubscriptionPlan, WorkspaceEntitlement


class BillingProvider(ABC):
    """Abstract interface for billing providers (Stripe, Chargebee, Mock)."""

    @abstractmethod
    def create_customer(self, workspace: Workspace, billing_email: str) -> str:
        """Create a billing customer record and return customer_id."""
        pass

    @abstractmethod
    def create_subscription(self, workspace: Workspace, plan: SubscriptionPlan) -> WorkspaceEntitlement:
        """Create or update a subscription for a workspace."""
        pass

    @abstractmethod
    def cancel_subscription(self, workspace: Workspace) -> WorkspaceEntitlement:
        """Cancel subscription at period end."""
        pass

    @abstractmethod
    def get_checkout_url(self, workspace: Workspace, plan: SubscriptionPlan, return_url: str) -> str:
        """Generate a secure checkout URL for plan upgrade."""
        pass


class MockBillingProvider(BillingProvider):
    """Local mock billing provider for development, testing, and isolated environments."""

    def create_customer(self, workspace: Workspace, billing_email: str) -> str:
        return f"cus_mock_{workspace.slug}_{hash(billing_email) % 10000}"

    def create_subscription(self, workspace: Workspace, plan: SubscriptionPlan) -> WorkspaceEntitlement:
        entitlement, _ = WorkspaceEntitlement.objects.get_or_create(workspace=workspace)
        entitlement.plan = plan
        entitlement.status = "active"
        entitlement.current_period_start = timezone.now()
        entitlement.current_period_end = timezone.now() + timezone.timedelta(days=30)
        entitlement.cancel_at_period_end = False
        entitlement.save()
        return entitlement

    def cancel_subscription(self, workspace: Workspace) -> WorkspaceEntitlement:
        entitlement = WorkspaceEntitlement.objects.get(workspace=workspace)
        entitlement.cancel_at_period_end = True
        entitlement.save()
        return entitlement

    def get_checkout_url(self, workspace: Workspace, plan: SubscriptionPlan, return_url: str) -> str:
        return f"/dashboard/workspaces/{workspace.id}/billing/?plan={plan.slug}&mock_checkout=1"


def get_billing_provider() -> BillingProvider:
    """Factory function returning active billing provider (defaults to MockBillingProvider)."""
    return MockBillingProvider()
