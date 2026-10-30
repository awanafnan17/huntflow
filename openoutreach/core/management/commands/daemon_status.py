# openoutreach/core/management/commands/daemon_status.py
"""Management command to report HuntFlow daemon status (Batch 6)."""
from __future__ import annotations

import os
from django.core.management.base import BaseCommand
from django.utils import timezone


class Command(BaseCommand):
    help = "Report current HuntFlow daemon execution status."

    def handle(self, *args, **options):
        from openoutreach.core.readiness import is_pid_alive, get_daemon_liveness_classification
        from openoutreach.core.models import DaemonStatus, Task

        status, _ = DaemonStatus.objects.get_or_create(pk=1)
        info = get_daemon_liveness_classification(status)

        self.stdout.write("HuntFlow Daemon Status")
        self.stdout.write("----------------------")
        self.stdout.write(f"Classification: {info['classification']}")
        self.stdout.write(f"Status (DB):  {'Running' if status.is_running else 'Stopped'}")
        self.stdout.write(f"Status (OS):  {info['os_state'].capitalize()}")
        self.stdout.write(f"PID:          {status.pid or 'None'}")
        self.stdout.write(f"Heartbeat:    {status.last_heartbeat or 'None'}")
        self.stdout.write(f"Active User:  {status.active_profile_username or 'None'}")
        
        pending_count = Task.objects.filter(status=Task.Status.PENDING).count()
        failed_count = Task.objects.filter(status=Task.Status.FAILED).count()
        self.stdout.write(f"Pending Q:    {pending_count}")
        self.stdout.write(f"Failed Tasks: {failed_count}")

        if status.last_error:
            self.stdout.write(f"Last Error:   {status.last_error}")
