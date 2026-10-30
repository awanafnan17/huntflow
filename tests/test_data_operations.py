# tests/test_data_operations.py
"""Test suite for customer data operations: export, deletion retention lock, and account deletion safeguards."""
from django.test import TestCase
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError

from openoutreach.core.models import Workspace, WorkspaceMembership, AuditEvent
from openoutreach.core.services.workspace_membership import create_workspace_with_owner
from openoutreach.core.services.data_operations import (
    export_workspace_data,
    delete_workspace_with_retention,
    delete_user_account,
)

User = get_user_model()


class DataOperationsTestCase(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="data_owner", email="owner@synthetic-demo.invalid")
        self.member = User.objects.create_user(username="data_member", email="member@synthetic-demo.invalid")
        self.workspace = create_workspace_with_owner(
            name="Data Ops Workspace",
            slug="data-ops-ws",
            owner_user=self.owner,
        )
        WorkspaceMembership.objects.create(
            workspace=self.workspace,
            user=self.member,
            role="campaign_manager",
            status="active",
        )

    def test_export_workspace_data_returns_sanitized_structure(self):
        data = export_workspace_data(self.workspace, actor=self.owner)
        self.assertIn("workspace", data)
        self.assertEqual(data["workspace"]["name"], "Data Ops Workspace")
        self.assertIn("memberships", data)
        self.assertTrue(AuditEvent.objects.filter(workspace=self.workspace, event_type="workspace_exported").exists())

    def test_delete_workspace_with_retention_lock(self):
        ws = delete_workspace_with_retention(self.workspace, actor=self.owner)
        self.assertEqual(ws.status, "pending_deletion")
        self.assertTrue(AuditEvent.objects.filter(workspace=self.workspace, event_type="workspace_deletion_requested").exists())

    def test_delete_user_account_fails_if_sole_owner_of_active_workspace(self):
        with self.assertRaises(ValidationError) as ctx:
            delete_user_account(self.owner, actor=self.owner)
        self.assertIn("sole owner", str(ctx.exception))

    def test_delete_user_account_succeeds_for_regular_member(self):
        user = delete_user_account(self.member, actor=self.owner)
        self.assertFalse(user.is_active)
        self.assertEqual(self.member.workspace_memberships.first().status, "suspended")
        self.assertTrue(AuditEvent.objects.filter(workspace=self.workspace, event_type="account_deletion_requested").exists())
