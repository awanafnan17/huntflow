# openoutreach/core/management/commands/ensure_huntflow_admin.py
"""Management command to create or reset the huntflow_admin user.

Sets staff=True, superuser=True, active=True. Prompts for password if interactive,
otherwise generates a temporary password and displays it once.
"""
from __future__ import annotations

import getpass
import secrets
import sys

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Ensure that a staff/superuser huntflow_admin exists with active status."

    def handle(self, *args, **options):
        username = "huntflow_admin"
        
        # Check if user already exists
        user = User.objects.filter(username=username).first()
        is_new = user is None

        # Determine password
        password = None
        if sys.stdin.isatty():
            self.stdout.write("Running interactively. Enter password for huntflow_admin:")
            while True:
                p1 = getpass.getpass("Password: ")
                p2 = getpass.getpass("Retype Password: ")
                if p1 == p2:
                    password = p1
                    break
                self.stderr.write("Passwords do not match. Try again.")
        else:
            # Generate a secure temporary password
            password = secrets.token_urlsafe(12)

        if is_new:
            user = User.objects.create_user(
                username=username,
                password=password,
                is_staff=True,
                is_superuser=True,
                is_active=True,
            )
            self.stdout.write(f"Successfully created user {username}.")
            if not sys.stdin.isatty():
                self.stdout.write("=========================================================")
                self.stdout.write(" WARNING: Non-interactive environment detected.")
                self.stdout.write(f" Temporary Password generated for '{username}':")
                self.stdout.write(f"   {password}")
                self.stdout.write(" Please change this password immediately upon logging in!")
                self.stdout.write("=========================================================")
        else:
            # Immutability guardrail: NEVER rotate or reset existing operator passwords
            user.is_staff = True
            user.is_superuser = True
            user.is_active = True
            user.save(update_fields=["is_staff", "is_superuser", "is_active"])
            self.stdout.write(f"User {username} already exists; ensured active staff/superuser status (password preserved).")
