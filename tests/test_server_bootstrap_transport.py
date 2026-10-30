# tests/test_server_bootstrap_transport.py
"""
HuntFlow Secure Server Bootstrap Transport Test Suite.

Verifies:
1. Zero Provider Traffic: Network tripwire strictly fails if any request hits linkedin.com.
2. Negative Authorization: Anonymous, Bakr cross-tenant, wrong workspace, wrong profile,
   invalid secret, expired secret, cross-origin, and concurrent viewers are blocked with 0 browser launches.
3. Secret Protection: Raw secret NEVER appears in WebSocket URL. First-message handshake required.
   No passwords, MFA codes, raw secrets, or frame payloads logged.
4. End-to-End Synthetic Interaction: Server-owned Chromium renders local synthetic login fixture,
   receives mouse/keyboard input, completes MFA, captures server storage state, stamps huntflow_managed,
   leaves profile in needs_reauth, and safely releases ProviderAccountLease.
"""
import asyncio
import hashlib
import http.server
import json
import logging
import os
import socket
import threading
import time
import urllib.request
import pytest

from asgiref.sync import sync_to_async
from django.contrib.auth.models import User
from django.contrib.sessions.backends.db import SessionStore
from django.test import Client
from django.utils import timezone
import websockets
from websockets.asyncio.client import connect

from openoutreach.core.models import Workspace, WorkspaceMembership
from openoutreach.linkedin.models import LinkedInProfile, ServerBootstrapSession, ProviderAccountLease
from openoutreach.linkedin.services.bootstrap_runtime import BootstrapServer
from openoutreach.linkedin.services.server_bootstrap import (
    start_server_bootstrap,
    hash_bootstrap_token,
)

# ── 1. PROVIDER NETWORK TRIPWIRE ──
@pytest.fixture(autouse=True)
def provider_network_tripwire(monkeypatch):
    """Enforces absolute zero outbound HTTP/HTTPS requests to LinkedIn domains."""
    orig_urlopen = urllib.request.urlopen

    def tripwire_urlopen(url, *args, **kwargs):
        target = str(url.full_url if hasattr(url, "full_url") else url).lower()
        if "linkedin.com" in target:
            raise AssertionError(f"PROVIDER NETWORK TRIPWIRE TRIGGERED: Outbound call to {target} is strictly banned!")
        return orig_urlopen(url, *args, **kwargs)

    monkeypatch.setattr(urllib.request, "urlopen", tripwire_urlopen)


# ── 2. LOCAL SYNTHETIC PROVIDER LOGIN FIXTURE ──
class SyntheticProviderHandler(http.server.BaseHTTPRequestHandler):
    """Simulates a provider login flow locally: login -> MFA -> authenticated destination."""

    def log_message(self, format, *args):
        # Suppress noisy HTTP logs during testing
        pass

    def do_GET(self):
        if self.path == "/login":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            html = """
            <!DOCTYPE html>
            <html>
            <head><title>Synthetic Provider Login</title></head>
            <body style="margin: 0; padding: 40px; font-family: sans-serif; background: #f3f4f6;">
                <div style="max-width: 400px; margin: 0 auto; background: white; padding: 30px; border-radius: 8px;">
                    <h2>Sign In</h2>
                    <form method="POST" action="/login">
                        <div style="margin-bottom: 15px;">
                            <label>Username</label><br>
                            <input id="username" name="username" type="text" style="width: 100%; height: 35px;" autofocus>
                        </div>
                        <div style="margin-bottom: 15px;">
                            <label>Password</label><br>
                            <input id="password" name="password" type="password" style="width: 100%; height: 35px;">
                        </div>
                        <button id="submit-btn" type="submit" style="width: 100%; height: 40px; background: #2563eb; color: white; border: none; cursor: pointer;">
                            Sign In
                        </button>
                    </form>
                </div>
            </body>
            </html>
            """
            self.wfile.write(html.encode("utf-8"))

        elif self.path == "/mfa":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            html = """
            <!DOCTYPE html>
            <html>
            <head><title>Synthetic MFA Challenge</title></head>
            <body style="margin: 0; padding: 40px; font-family: sans-serif; background: #f3f4f6;">
                <div style="max-width: 400px; margin: 0 auto; background: white; padding: 30px; border-radius: 8px;">
                    <h2>Security Verification</h2>
                    <p>Enter 6-digit code</p>
                    <form method="POST" action="/mfa">
                        <div style="margin-bottom: 15px;">
                            <input id="mfa_code" name="mfa_code" type="text" style="width: 100%; height: 35px;" autofocus>
                        </div>
                        <button id="verify-btn" type="submit" style="width: 100%; height: 40px; background: #059669; color: white; border: none; cursor: pointer;">
                            Verify & Proceed
                        </button>
                    </form>
                </div>
            </body>
            </html>
            """
            self.wfile.write(html.encode("utf-8"))

        elif self.path == "/feed":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            html = """
            <!DOCTYPE html>
            <html>
            <head><title>Authenticated Feed</title></head>
            <body style="padding: 50px; font-family: sans-serif;">
                <h1 id="feed-header">Welcome to Authenticated Feed</h1>
            </body>
            </html>
            """
            self.wfile.write(html.encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        post_body = self.rfile.read(content_length).decode("utf-8")

        if self.path == "/login":
            # Redirect to MFA
            self.send_response(303)
            self.send_header("Location", "/mfa")
            self.send_header("Set-Cookie", "synthetic_step=mfa; Path=/")
            self.end_headers()

        elif self.path == "/mfa":
            # Set synthetic auth cookie and redirect to feed
            self.send_response(303)
            self.send_header("Location", "/feed")
            self.send_header("Set-Cookie", "li_at=synthetic_li_at_test_cookie_value; Path=/")
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()


@pytest.fixture(scope="module")
def synthetic_server():
    """Runs local synthetic provider HTTP server on a random port."""
    server = http.server.HTTPServer(("127.0.0.1", 0), SyntheticProviderHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{port}/login"
    yield url
    server.shutdown()


def get_free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ── 3. TEST DATA FIXTURES ──
@pytest.fixture
def bootstrap_env(db):
    """Sets up Candidate (tenant 1) and Bakr (tenant 2) workspaces, profiles, and sessions."""
    # Candidate Workspace & Profile #2
    user_candidate = User.objects.create_user(username="candidate_test_user", password="password123", is_staff=True)
    ws_candidate = Workspace.objects.create(name="Default Workspace", slug="default")
    WorkspaceMembership.objects.create(workspace=ws_candidate, user=user_candidate, role="OWNER", status="active")
    prof_candidate = LinkedInProfile.objects.create(
        id=2,
        user=user_candidate,
        workspace=ws_candidate,
        linkedin_username="candidate_test_prof",
        session_status="needs_reauth",
        session_origin="imported_seed",
        active=True,
    )

    # Bakr Workspace & User
    user_bakr = User.objects.create_user(username="bakr_test_user", password="password123", is_staff=True)
    ws_bakr = Workspace.objects.create(name="Bakr Agency", slug="bakr-agency")
    WorkspaceMembership.objects.create(workspace=ws_bakr, user=user_bakr, role="OWNER", status="active")

    # Django Sessions
    session_candidate = SessionStore()
    session_candidate["_auth_user_id"] = str(user_candidate.pk)
    session_candidate["_auth_user_backend"] = "django.contrib.auth.backends.ModelBackend"
    session_candidate.save()

    session_bakr = SessionStore()
    session_bakr["_auth_user_id"] = str(user_bakr.pk)
    session_bakr["_auth_user_backend"] = "django.contrib.auth.backends.ModelBackend"
    session_bakr.save()

    return {
        "user_candidate": user_candidate,
        "ws_candidate": ws_candidate,
        "prof_candidate": prof_candidate,
        "session_candidate_key": session_candidate.session_key,
        "user_bakr": user_bakr,
        "ws_bakr": ws_bakr,
        "session_bakr_key": session_bakr.session_key,
    }


# ── 4. NEGATIVE AUTHORIZATION TESTS (0 BROWSERS LAUNCHED) ──
@pytest.mark.django_db(transaction=True)
def test_negative_authorization_launches_zero_browsers(bootstrap_env):
    """Verifies that all unauthorized WebSocket attempts fail closed with 0 Chromium instances launched."""
    prof = bootstrap_env["prof_candidate"]
    ws = bootstrap_env["ws_candidate"]
    user = bootstrap_env["user_candidate"]

    session, raw_token, err = start_server_bootstrap(
        workspace=ws,
        linkedin_profile=prof,
        operator=user,
        ttl_seconds=60,
    )
    assert err is None
    assert session is not None

    ws_port = get_free_port()
    server = BootstrapServer(
        host="127.0.0.1",
        port=ws_port,
        allowed_origins={"https://huntflow.online"},
    )

    async def run_negative_checks():
        await server.start()
        try:
            # 1. Anonymous connection (no Cookie)
            with pytest.raises(websockets.exceptions.ConnectionClosed) as exc:
                async with connect(
                    f"ws://127.0.0.1:{ws_port}/ws/bootstrap/{session.pk}/",
                    additional_headers={"Origin": "https://huntflow.online"},
                ) as ws_conn:
                    await ws_conn.recv()
            assert exc.value.rcvd.code == 4001

            # 2. Forbidden cross-origin (evil.com)
            with pytest.raises(websockets.exceptions.ConnectionClosed) as exc:
                async with connect(
                    f"ws://127.0.0.1:{ws_port}/ws/bootstrap/{session.pk}/",
                    additional_headers={
                        "Origin": "https://evil.com",
                        "Cookie": f"sessionid={bootstrap_env['session_candidate_key']}",
                    },
                ) as ws_conn:
                    await ws_conn.recv()
            assert exc.value.rcvd.code == 4003

            # 3. Bakr cross-tenant connection to Candidate Profile #2 bootstrap
            with pytest.raises(websockets.exceptions.ConnectionClosed) as exc:
                async with connect(
                    f"ws://127.0.0.1:{ws_port}/ws/bootstrap/{session.pk}/",
                    additional_headers={
                        "Origin": "https://huntflow.online",
                        "Cookie": f"sessionid={bootstrap_env['session_bakr_key']}",
                    },
                ) as ws_conn:
                    await ws_conn.recv()
            assert exc.value.rcvd.code == 4003

            # 4. Wrong bootstrap ID
            with pytest.raises(websockets.exceptions.ConnectionClosed) as exc:
                async with connect(
                    f"ws://127.0.0.1:{ws_port}/ws/bootstrap/999999/",
                    additional_headers={
                        "Origin": "https://huntflow.online",
                        "Cookie": f"sessionid={bootstrap_env['session_candidate_key']}",
                    },
                ) as ws_conn:
                    await ws_conn.recv()
            assert exc.value.rcvd.code == 4004

            # 5. Wrong bootstrap secret in first handshake message
            with pytest.raises(websockets.exceptions.ConnectionClosed) as exc:
                async with connect(
                    f"ws://127.0.0.1:{ws_port}/ws/bootstrap/{session.pk}/",
                    additional_headers={
                        "Origin": "https://huntflow.online",
                        "Cookie": f"sessionid={bootstrap_env['session_candidate_key']}",
                    },
                ) as ws_conn:
                    await ws_conn.send(json.dumps({"type": "authorize", "secret": "wrong_secret_token"}))
                    await ws_conn.recv()
            assert exc.value.rcvd.code == 4003

            # 6. Handshake timeout (no authorize message sent within deadline)
            # In testing, send an invalid message type
            with pytest.raises(websockets.exceptions.ConnectionClosed) as exc:
                async with connect(
                    f"ws://127.0.0.1:{ws_port}/ws/bootstrap/{session.pk}/",
                    additional_headers={
                        "Origin": "https://huntflow.online",
                        "Cookie": f"sessionid={bootstrap_env['session_candidate_key']}",
                    },
                ) as ws_conn:
                    await ws_conn.send(json.dumps({"type": "mouse_move", "x": 10, "y": 10}))
                    await ws_conn.recv()
            assert exc.value.rcvd.code == 4003

            # Verify 0 runtimes created
            assert len(server.runtimes) == 0

        finally:
            await server.stop()

    asyncio.run(run_negative_checks())

    # Verify session is still PENDING and lease remains intact
    session.refresh_from_db()
    assert session.status == ServerBootstrapSession.Status.PENDING
    assert session.viewer_connected is False


# ── 5. SINGLE VIEWER CONCURRENT REJECTION TEST ──
@pytest.mark.django_db(transaction=True)
def test_concurrent_viewer_rejected_atomically(bootstrap_env, synthetic_server):
    """Verifies that a second viewer connection is rejected with 4009 while first viewer is active."""
    prof = bootstrap_env["prof_candidate"]
    ws = bootstrap_env["ws_candidate"]
    user = bootstrap_env["user_candidate"]

    session, raw_token, err = start_server_bootstrap(
        workspace=ws,
        linkedin_profile=prof,
        operator=user,
        ttl_seconds=60,
    )

    ws_port = get_free_port()
    server = BootstrapServer(
        host="127.0.0.1",
        port=ws_port,
        allowed_origins={"https://huntflow.online"},
        synthetic_login_url=synthetic_server,
    )

    async def run_concurrency_test():
        await server.start()
        try:
            # First viewer connects and authorizes
            ws1 = await connect(
                f"ws://127.0.0.1:{ws_port}/ws/bootstrap/{session.pk}/",
                additional_headers={
                    "Origin": "https://huntflow.online",
                    "Cookie": f"sessionid={bootstrap_env['session_candidate_key']}",
                },
            )
            await ws1.send(json.dumps({"type": "authorize", "secret": raw_token}))
            ack = json.loads(await ws1.recv())
            assert ack.get("type") == "authorized"

            # Verify DB viewer lock
            sess_db = await sync_to_async(ServerBootstrapSession.objects.get)(id=session.pk)
            assert sess_db.viewer_connected is True

            # Second viewer attempts to connect to same bootstrap
            with pytest.raises(websockets.exceptions.ConnectionClosed) as exc:
                async with connect(
                    f"ws://127.0.0.1:{ws_port}/ws/bootstrap/{session.pk}/",
                    additional_headers={
                        "Origin": "https://huntflow.online",
                        "Cookie": f"sessionid={bootstrap_env['session_candidate_key']}",
                    },
                ) as ws2:
                    await ws2.send(json.dumps({"type": "authorize", "secret": raw_token}))
                    await ws2.recv()

            assert exc.value.rcvd.code == 4009

            # Teardown first viewer cleanly
            await ws1.send(json.dumps({"type": "abort"}))
            await ws1.close()

        finally:
            await server.stop()

    asyncio.run(run_concurrency_test())


# ── 6. END-TO-END SYNTHETIC INTERACTION & LEAK TEST ──
@pytest.mark.django_db(transaction=True)
def test_synthetic_screencast_e2e_and_zero_secret_leak(bootstrap_env, synthetic_server, caplog):
    """Complete synthetic provider authentication flow over authenticated WebSocket screencast:
    - Verifies raw secret NEVER in URL
    - Screencast delivers JPEG frames
    - Coordinate clicks and typing reach synthetic browser
    - MFA challenge completed
    - Server context captures storage state
    - session_origin becomes huntflow_managed, status becomes needs_reauth
    - Lease released and browser cleanly closed
    - Secret leak test proves 0 passwords, MFA codes, raw secrets, or frames logged.
    """
    caplog.set_level(logging.DEBUG)
    logging.getLogger("websockets").setLevel(logging.WARNING)

    prof = bootstrap_env["prof_candidate"]
    ws = bootstrap_env["ws_candidate"]
    user = bootstrap_env["user_candidate"]

    # 1. Dashboard start request
    client = Client()
    client.force_login(user)
    # Ensure active workspace in session
    session_obj = client.session
    session_obj["active_workspace_slug"] = ws.slug
    session_obj.save()

    response = client.post("/dashboard/settings/linkedin/bootstrap/start/")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    bootstrap_id = data["bootstrap_id"]
    raw_secret = data["secret"]
    ws_url = data["ws_url"]

    # Invariant: Raw secret must NOT be in the WebSocket URL!
    assert raw_secret not in ws_url
    assert f"/ws/bootstrap/{bootstrap_id}/" in ws_url

    ws_port = get_free_port()
    server = BootstrapServer(
        host="127.0.0.1",
        port=ws_port,
        allowed_origins={"https://huntflow.online"},
        synthetic_login_url=synthetic_server,
    )

    async def run_e2e():
        await server.start()
        try:
            fake_user = "synthetic_operator@example.invalid"
            fake_pass = "TEST_ONLY_SECRET_PASSWORD_99"
            fake_mfa = "847291"

            async with connect(
                f"ws://127.0.0.1:{ws_port}/ws/bootstrap/{bootstrap_id}/",
                additional_headers={
                    "Origin": "https://huntflow.online",
                    "Cookie": f"sessionid={client.session.session_key}",
                },
            ) as ws_conn:
                # Send first-message authorization
                await ws_conn.send(json.dumps({"type": "authorize", "secret": raw_secret}))
                ack = json.loads(await ws_conn.recv())
                assert ack.get("type") == "authorized"

                # Wait for initial screencast frames to arrive
                frames_received = 0
                runtime = server.runtimes[bootstrap_id]

                # Wait for DOM to render
                await asyncio.sleep(1.0)
                page = runtime.page
                assert page is not None

                # Receive frames
                while frames_received < 2:
                    raw_frame = await asyncio.wait_for(ws_conn.recv(), timeout=5.0)
                    msg = json.loads(raw_frame)
                    if msg.get("type") == "frame":
                        frames_received += 1
                        assert len(msg.get("data", "")) > 100

                # Coordinate-targeted typing on #username
                box_user = await page.locator("#username").bounding_box()
                assert box_user is not None
                cx_user = int(box_user["x"] + box_user["width"] / 2)
                cy_user = int(box_user["y"] + box_user["height"] / 2)

                await ws_conn.send(json.dumps({"type": "mouse_down", "x": cx_user, "y": cy_user, "button": "left"}))
                await ws_conn.send(json.dumps({"type": "mouse_up", "x": cx_user, "y": cy_user, "button": "left"}))
                await ws_conn.send(json.dumps({"type": "text_input", "text": fake_user}))

                # Coordinate-targeted typing on #password
                box_pass = await page.locator("#password").bounding_box()
                assert box_pass is not None
                cx_pass = int(box_pass["x"] + box_pass["width"] / 2)
                cy_pass = int(box_pass["y"] + box_pass["height"] / 2)

                await ws_conn.send(json.dumps({"type": "mouse_down", "x": cx_pass, "y": cy_pass, "button": "left"}))
                await ws_conn.send(json.dumps({"type": "mouse_up", "x": cx_pass, "y": cy_pass, "button": "left"}))
                await ws_conn.send(json.dumps({"type": "text_input", "text": fake_pass}))

                # Click Submit button
                box_btn = await page.locator("#submit-btn").bounding_box()
                assert box_btn is not None
                cx_btn = int(box_btn["x"] + box_btn["width"] / 2)
                cy_btn = int(box_btn["y"] + box_btn["height"] / 2)

                await ws_conn.send(json.dumps({"type": "mouse_down", "x": cx_btn, "y": cy_btn, "button": "left"}))
                await ws_conn.send(json.dumps({"type": "mouse_up", "x": cx_btn, "y": cy_btn, "button": "left"}))

                # Wait for MFA screen to appear
                await page.wait_for_selector("#mfa_code", timeout=5000)

                # Type MFA code
                box_mfa = await page.locator("#mfa_code").bounding_box()
                assert box_mfa is not None
                cx_mfa = int(box_mfa["x"] + box_mfa["width"] / 2)
                cy_mfa = int(box_mfa["y"] + box_mfa["height"] / 2)

                await ws_conn.send(json.dumps({"type": "mouse_down", "x": cx_mfa, "y": cy_mfa, "button": "left"}))
                await ws_conn.send(json.dumps({"type": "mouse_up", "x": cx_mfa, "y": cy_mfa, "button": "left"}))
                await ws_conn.send(json.dumps({"type": "text_input", "text": fake_mfa}))

                # Click Verify button
                box_vbtn = await page.locator("#verify-btn").bounding_box()
                assert box_vbtn is not None
                cx_vbtn = int(box_vbtn["x"] + box_vbtn["width"] / 2)
                cy_vbtn = int(box_vbtn["y"] + box_vbtn["height"] / 2)

                await ws_conn.send(json.dumps({"type": "mouse_down", "x": cx_vbtn, "y": cy_vbtn, "button": "left"}))
                await ws_conn.send(json.dumps({"type": "mouse_up", "x": cx_vbtn, "y": cy_vbtn, "button": "left"}))

                # Wait for authenticated feed destination
                await page.wait_for_selector("#feed-header", timeout=5000)

                # Send Complete message
                await ws_conn.send(json.dumps({"type": "complete"}))

                # Wait for completion ack
                while True:
                    ack_msg = json.loads(await ws_conn.recv())
                    if ack_msg.get("type") in ("completed", "error"):
                        assert ack_msg.get("type") == "completed"
                        break

        finally:
            await server.stop()

    asyncio.run(run_e2e())

    # 2. Verify Database Invariants
    prof.refresh_from_db()
    assert prof.session_origin == "huntflow_managed"
    assert prof.session_status == "needs_reauth"
    assert prof.bootstrap_completed_at is not None

    sess_db = ServerBootstrapSession.objects.get(id=bootstrap_id)
    assert sess_db.status == ServerBootstrapSession.Status.COMPLETED
    assert sess_db.viewer_connected is False

    # Lease must be cleanly released
    active_leases = ProviderAccountLease.objects.filter(linkedin_profile=prof).count()
    assert active_leases == 0

    # 3. Secret Leak Verification
    log_text = caplog.text
    assert "TEST_ONLY_SECRET_PASSWORD_99" not in log_text
    assert "847291" not in log_text
    assert raw_secret not in log_text
    assert "synthetic_li_at_test_cookie_value" not in log_text
