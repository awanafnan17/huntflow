# tests/test_daemon_controls.py
"""Tests for Daemon status tracking, controls, and duplicate prevention (Batch 6)."""
from __future__ import annotations

import os
import pytest
import subprocess
from unittest.mock import patch, MagicMock

from django.urls import reverse
from django.utils import timezone

from openoutreach.core.models import DaemonStatus, SiteConfig
from tests.factories import UserFactory


@pytest.mark.django_db
class TestDaemonStatusAndControls:

    def test_daemon_page_requires_staff_login(self, client):
        """Visiting the daemon page as unauthenticated or non-staff redirects to login."""
        url = reverse("dashboard:daemon")
        
        # 1. Unauthenticated
        response = client.get(url)
        assert response.status_code == 302
        assert "/dashboard/login/" in response.url

        # 2. Authenticated but non-staff/non-superuser
        user = UserFactory(username="regularuser", is_staff=False, is_superuser=False)
        client.force_login(user)
        response = client.get(url)
        # Assuming our dashboard auth decorator redirects or shows forbidden.
        # Let's assert it is not a 200 OK or redirects to login/access-denied.
        assert response.status_code in [302, 403]

    def test_daemon_status_renders_correctly(self, client):
        """Dashboard daemon status page renders correctly with active stats."""
        user = UserFactory(username="staffop", is_staff=True)
        client.force_login(user)

        # Create a running status row
        status = DaemonStatus.objects.create(
            is_running=True,
            pid=12345,
            last_heartbeat=timezone.now(),
            last_error="No active issues",
            active_profile_username="test_user",
        )

        url = reverse("dashboard:daemon")
        
        # Mock is_pid_alive to return running
        with patch("openoutreach.core.readiness.is_pid_alive") as mock_alive:
            mock_alive.return_value = "running"
            response = client.get(url)
            
            assert response.status_code == 200
            content = response.content.decode("utf-8")
            assert "Running" in content
            assert "12345" in content
            assert "test_user" in content
            assert "No active issues" in content

    def test_start_stop_controls_require_post(self, client):
        """Posting to start/stop controls updates daemon state, while GET is read-only."""
        user = UserFactory(username="staffop", is_staff=True)
        client.force_login(user)

        status = DaemonStatus.objects.create(
            is_running=False,
            pid=None,
            last_heartbeat=None,
        )

        url = reverse("dashboard:daemon")
        
        # GET request should not start process
        with patch("subprocess.Popen") as mock_popen:
            client.get(url)
            mock_popen.assert_not_called()

        # POST with action=start should spawn subprocess
        with patch("subprocess.Popen") as mock_popen, \
             patch("os.kill") as mock_kill, \
             patch("openoutreach.core.readiness.check_launch_readiness") as mock_readiness:
            mock_readiness.return_value = {
                "overall_status": "ready",
                "hard_blockers": [],
                "warnings": [],
                "next_action": {"description": "Test"},
                "sections": {
                    "task_queue": {
                        "ignored_pending": 0,
                        "failed_tasks": 0,
                        "cancelled_tasks": 0,
                        "next_scheduled_task": "None"
                    }
                }
            }
            mock_kill.side_effect = OSError("No such process")
            response = client.post(url, {"action": "start"})
            assert response.status_code == 302
            mock_popen.assert_called_once()

    def test_duplicate_daemon_prevention_logic(self, client):
        """Attempting to start the daemon when it is already running does not spawn duplicate processes."""
        user = UserFactory(username="staffop", is_staff=True)
        client.force_login(user)

        status = DaemonStatus.objects.create(
            is_running=True,
            pid=12345,
            last_heartbeat=timezone.now(),
        )

        url = reverse("dashboard:daemon")
        
        with patch("subprocess.Popen") as mock_popen, \
             patch("openoutreach.core.readiness.is_pid_alive") as mock_alive:
            mock_alive.return_value = "running"
            response = client.post(url, {"action": "start"})
            
            assert response.status_code == 302
            # Should NOT start another process
            mock_popen.assert_not_called()

    def test_secrets_are_never_rendered_in_logs(self, client):
        """Dashboard log viewer sanitizes LLM or BetterContact keys from log outputs."""
        user = UserFactory(username="staffop", is_staff=True)
        client.force_login(user)

        # Configure secret keys
        cfg = SiteConfig.load()
        cfg.llm_api_key = "sk-llm-super-secret-key-1234"
        cfg.bettercontact_api_key = "test-1-aaaaaaaa"
        cfg.save()

        # Create a dummy log file
        log_content = (
            "INF daemon started\n"
            "ERR LLM failed with key sk-llm-super-secret-key-1234 invalid\n"
            "INF BetterContact query test-1-aaaaaaaa returned status\n"
        )
        with open("daemon.log", "w", encoding="utf-8") as f:
            f.write(log_content)

        url = reverse("dashboard:daemon")
        try:
            response = client.get(url)
            assert response.status_code == 200
            content = response.content.decode("utf-8")
            
            # Verify keys are masked
            assert "sk-llm-super-secret-key-1234" not in content
            assert "test-1-aaaaaaaa" not in content
            assert "********" in content
        finally:
            # Clean up dummy log file
            if os.path.exists("daemon.log"):
                os.remove("daemon.log")

    @patch("os.kill")
    @patch("subprocess.run")
    def test_is_pid_alive_nonexistent_pid(self, mock_run, mock_kill):
        """nonexistent PID returns stopped."""
        from openoutreach.core.readiness import is_pid_alive
        import errno
        mock_kill.side_effect = OSError(errno.ESRCH, "No such process")
        
        assert is_pid_alive(99999) == "stopped"
        mock_run.assert_not_called()

    @patch("os.kill")
    @patch("subprocess.run")
    def test_is_pid_alive_matching_daemon(self, mock_run, mock_kill):
        """matching HuntFlow command returns running."""
        from openoutreach.core.readiness import is_pid_alive
        import sys
        mock_kill.return_value = None
        
        # Mock POSIX
        with patch("os.name", "posix"):
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=f"{sys.executable} -u manage.py rundaemon"
            )
            assert is_pid_alive(123) == "running"

    @patch("os.kill")
    @patch("subprocess.run")
    def test_is_pid_alive_another_django_project(self, mock_run, mock_kill):
        """another Django project returns unknown."""
        from openoutreach.core.readiness import is_pid_alive
        mock_kill.return_value = None
        
        with patch("os.name", "posix"):
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="/other/path/python manage.py rundaemon"
            )
            assert is_pid_alive(123) == "unknown"

    @patch("os.kill")
    @patch("subprocess.run")
    def test_is_pid_alive_unrelated_process(self, mock_run, mock_kill):
        """unrelated process returns unknown."""
        from openoutreach.core.readiness import is_pid_alive
        mock_kill.return_value = None
        
        with patch("os.name", "posix"):
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="/usr/bin/nginx -g daemon off;"
            )
            assert is_pid_alive(123) == "unknown"

    @patch("os.kill")
    @patch("subprocess.run")
    def test_is_pid_alive_permission_error(self, mock_run, mock_kill):
        """permission error returns unknown."""
        from openoutreach.core.readiness import is_pid_alive
        import errno
        mock_kill.side_effect = OSError(errno.EPERM, "Operation not permitted")
        
        assert is_pid_alive(123) == "unknown"
        mock_run.assert_not_called()

    @patch("os.kill")
    @patch("subprocess.run")
    def test_is_pid_alive_timeout_and_blank(self, mock_run, mock_kill):
        """timeout or blank returns unknown."""
        from openoutreach.core.readiness import is_pid_alive
        mock_kill.return_value = None
        
        with patch("os.name", "posix"):
            # Timeout
            mock_run.side_effect = subprocess.TimeoutExpired(["ps"], 2)
            assert is_pid_alive(123) == "unknown"
            
            # Blank/Error
            mock_run.side_effect = None
            mock_run.return_value = MagicMock(returncode=1, stdout="")
            assert is_pid_alive(123) == "unknown"

    @patch("subprocess.Popen")
    @patch("openoutreach.core.readiness.is_pid_alive")
    @patch("openoutreach.core.readiness.check_launch_readiness")
    def test_start_daemon_posix_options(self, mock_readiness, mock_is_alive, mock_popen, client):
        """POSIX Popen receives start_new_session=True and no Windows creation flags."""
        user = UserFactory(username="staffop", is_staff=True)
        client.force_login(user)
        
        mock_readiness.return_value = {
            "overall_status": "ready", "hard_blockers": [], "warnings": [],
            "next_action": {"description": "Test"}, "sections": {"task_queue": {"ignored_pending": 0, "failed_tasks": 0, "cancelled_tasks": 0, "next_scheduled_task": "None"}}
        }
        mock_is_alive.return_value = "stopped"
        
        url = reverse("dashboard:daemon")
        with patch("os.name", "posix"):
            client.post(url, {"action": "start"})
            mock_popen.assert_called_once()
            kwargs = mock_popen.call_args[1]
            assert kwargs.get("start_new_session") is True
            assert "creationflags" not in kwargs
            assert kwargs.get("cwd") is not None

    @patch("subprocess.Popen")
    @patch("openoutreach.core.readiness.is_pid_alive")
    @patch("openoutreach.core.readiness.check_launch_readiness")
    def test_start_daemon_windows_options(self, mock_readiness, mock_is_alive, mock_popen, client):
        """Windows Popen receives CREATE_NEW_PROCESS_GROUP and no start_new_session."""
        user = UserFactory(username="staffop", is_staff=True)
        client.force_login(user)
        
        mock_readiness.return_value = {
            "overall_status": "ready", "hard_blockers": [], "warnings": [],
            "next_action": {"description": "Test"}, "sections": {"task_queue": {"ignored_pending": 0, "failed_tasks": 0, "cancelled_tasks": 0, "next_scheduled_task": "None"}}
        }
        mock_is_alive.return_value = "stopped"
        
        url = reverse("dashboard:daemon")
        with patch("os.name", "nt"):
            client.post(url, {"action": "start"})
            mock_popen.assert_called_once()
            kwargs = mock_popen.call_args[1]
            assert "start_new_session" not in kwargs
            assert kwargs.get("creationflags") is not None

    @patch("openoutreach.core.readiness.is_pid_alive")
    def test_safe_stop_daemon_flows(self, mock_is_alive):
        """Test safe_stop_daemon stop logic scenarios."""
        from openoutreach.core.readiness import safe_stop_daemon
        
        status = DaemonStatus.objects.create(is_running=True, pid=555)
        
        # 1. Stopped state clears PID without signal
        mock_is_alive.return_value = "stopped"
        with patch("os.kill") as mock_kill:
            res = safe_stop_daemon(status)
            assert res["success"] is True
            assert res["result"] == "already_stopped"
            assert status.pid is None
            assert status.is_running is False
            mock_kill.assert_not_called()
            
        # 2. Unknown state sends no signal
        status.pid = 555
        status.is_running = True
        status.save()
        mock_is_alive.return_value = "unknown"
        with patch("os.kill") as mock_kill:
            res = safe_stop_daemon(status)
            assert res["success"] is False
            assert res["result"] == "abort_unknown"
            mock_kill.assert_not_called()

        # 3. Double-check is enforced and blocks SIGTERM if state changes to stopped/unknown before signal
        status.pid = 555
        status.is_running = True
        status.save()
        mock_is_alive.side_effect = ["running", "stopped"]
        with patch("os.kill") as mock_kill:
            res = safe_stop_daemon(status)
            assert res["success"] is False
            assert res["result"] == "abort_unknown"
            mock_kill.assert_not_called()

        # 4. Safe termination succeeds with wait
        mock_is_alive.side_effect = None
        mock_is_alive.side_effect = ["running", "running", "stopped"]
        status.pid = 555
        status.is_running = True
        status.save()
        with patch("os.kill") as mock_kill, patch("time.sleep") as mock_sleep:
            res = safe_stop_daemon(status)
            assert res["success"] is True
            assert res["result"] == "stopped"
            assert status.pid is None
            assert status.is_running is False
            mock_kill.assert_called_once()

    @patch("openoutreach.core.readiness.is_pid_alive")
    def test_rotate_logs_gating_scenarios(self, mock_is_alive):
        """Test log rotation states check logic."""
        from django.core.management import call_command
        
        status, _ = DaemonStatus.objects.get_or_create(pk=1)
        status.pid = 1234
        status.is_running = True
        status.save()
        
        # Mock active log file existence and size
        with patch("os.path.exists", side_effect=lambda p: p == "daemon.log"), \
             patch("os.path.getsize", return_value=999999999):
             
            # Running state blocks actual rotation
            mock_is_alive.return_value = "running"
            with patch("shutil.copy") as mock_copy:
                try:
                    call_command("rotate_huntflow_logs")
                except SystemExit:
                    pass
                mock_copy.assert_not_called()

            # Unknown state blocks actual rotation
            mock_is_alive.return_value = "unknown"
            with patch("shutil.copy") as mock_copy:
                try:
                    call_command("rotate_huntflow_logs")
                except SystemExit:
                    pass
                mock_copy.assert_not_called()

            # Stopped state allows actual rotation
            mock_is_alive.return_value = "stopped"
            with patch("shutil.copy") as mock_copy, \
                 patch("openoutreach.core.management.commands.rotate_huntflow_logs.open", MagicMock()):
                call_command("rotate_huntflow_logs")
                mock_copy.assert_called_once()

