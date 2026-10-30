# openoutreach/core/management/commands/huntflow_start.py
import subprocess
import sys
import os
from django.core.management.base import BaseCommand

class Command(BaseCommand):
    help = "Start all HuntFlow services (Web server, Daemon, Workers)."

    def handle(self, *args, **options):
        self.stdout.write(self.style.SUCCESS("Starting HuntFlow Production-Style Stack..."))
        
        env = os.environ.copy()
        env["DJANGO_SETTINGS_MODULE"] = "openoutreach.settings"
        env["SECURE_SSL_REDIRECT"] = "False"

        import urllib.request
        from openoutreach.core.readiness import is_pid_alive, get_daemon_liveness_classification
        from openoutreach.core.models import DaemonStatus

        # 1. Web server check & reuse
        web_reused = False
        web_pid = None
        try:
            req = urllib.request.urlopen("http://127.0.0.1:8080/dashboard/live-work/", timeout=1)
            if req.status in (200, 302):
                web_reused = True
        except Exception:
            web_reused = False

        if web_reused:
            self.stdout.write(self.style.SUCCESS("WEB\nSTATUS: REUSED"))
        else:
            web_cmd = [sys.executable, "manage.py", "runserver", "127.0.0.1:8080"]
            web_log = open("/tmp/huntflow_web.log", "a")
            p_web = subprocess.Popen(web_cmd, env=env, stdout=web_log, stderr=web_log)
            web_pid = p_web.pid
            self.stdout.write(self.style.SUCCESS(f"WEB\nSTATUS: STARTED\nPID: {web_pid}"))

        # 2. Daemon check & start
        status, _ = DaemonStatus.objects.get_or_create(pk=1)
        info = get_daemon_liveness_classification(status)

        if info["classification"] == "HEALTHY":
            self.stdout.write(self.style.SUCCESS(f"DAEMON\nSTATUS: REUSED\nPID: {status.pid}"))
        else:
            if info["classification"] == "STALE_HEARTBEAT":
                self.stdout.write(self.style.WARNING(f"Stopping STALE_HEARTBEAT daemon PID {status.pid}..."))
                from openoutreach.core.readiness import safe_stop_daemon
                safe_stop_daemon(status)

            daemon_cmd = [sys.executable, "manage.py", "rundaemon"]
            daemon_log = open("/tmp/huntflow_daemon.log", "a")
            p_daemon = subprocess.Popen(daemon_cmd, env=env, stdout=daemon_log, stderr=daemon_log)
            self.stdout.write(self.style.SUCCESS(f"DAEMON\nSTATUS: STARTED\nPID: {p_daemon.pid}"))

        self.stdout.write(self.style.SUCCESS("All HuntFlow services running!"))
