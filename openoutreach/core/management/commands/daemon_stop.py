# openoutreach/core/management/commands/daemon_stop.py
"""Management command to stop a running HuntFlow daemon process (Batch 6)."""
from __future__ import annotations

import os
import signal
from django.core.management.base import BaseCommand
from django.utils import timezone


class Command(BaseCommand):
    help = "Stop a running HuntFlow daemon process by PID."

    def handle(self, *args, **options):
        from openoutreach.core.models import DaemonStatus
        from openoutreach.core.readiness import safe_stop_daemon

        status, _ = DaemonStatus.objects.get_or_create(pk=1)

        res = safe_stop_daemon(status)
        if res["success"]:
            self.stdout.write(self.style.SUCCESS(f"Daemon stopped successfully: {res.get('result')}"))
        else:
            self.stderr.write(self.style.ERROR(f"Failed to stop daemon: {res.get('error')}"))
