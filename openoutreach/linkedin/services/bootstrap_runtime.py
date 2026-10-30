# openoutreach/linkedin/services/bootstrap_runtime.py
"""
HuntFlow Dedicated Server Bootstrap Runtime Service.

Core Principles:
1. Server Browser Ownership: Async Playwright Chromium is created, owned, and controlled
   exclusively inside this server process.
2. Zero Public Debug Ports: No CDP, VNC, noVNC, or X11 ports are exposed.
   Chromium runs headless; CDP screencast is 100% server-internal.
3. Strict Handshake & Authorization:
   - WebSocket URL contains ONLY the non-secret bootstrap_id: /ws/bootstrap/<bootstrap_id>/
   - Authenticated via same-origin Django session cookie (sessionid).
   - First-message handshake requires the single-use secret token within 10 seconds.
   - Enforces single-viewer atomic DB lock. Rejects concurrent viewers.
   - Origin validation: allowed origins strictly enforced.
4. Ephemeral Streaming & Zero Credential Logging:
   - Screencast JPEG frames are streamed memory-only at 8-12 FPS.
   - Frames, keystrokes, passwords, MFA codes, and clipboard are NEVER persisted or logged.
5. Trust Boundary:
   - Storage state is captured directly by this runtime via context.storage_state().
   - Calls complete_server_bootstrap_from_runtime() internally.
   - Leaves profile in needs_reauth with session_origin=huntflow_managed.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import urllib.parse
from typing import Any, Dict, Optional, Set

from asgiref.sync import sync_to_async
from django.conf import settings
from django.contrib.auth.models import User
from django.contrib.sessions.models import Session
from django.utils import timezone
from playwright.async_api import async_playwright, Browser, BrowserContext, Page, CDPSession
import websockets
from websockets.asyncio.server import ServerConnection, serve

from openoutreach.core.models import WorkspaceMembership
from openoutreach.linkedin.models import ServerBootstrapSession
from openoutreach.linkedin.services.server_bootstrap import (
    claim_bootstrap_viewer,
    release_bootstrap_viewer,
    complete_server_bootstrap_from_runtime,
    abort_server_bootstrap,
    renew_server_bootstrap_lease,
    hash_bootstrap_token,
)

logger = logging.getLogger(__name__)
# Suppress low-level websockets frame/wire debug logs from leaking payloads into loggers
logging.getLogger("websockets").setLevel(logging.WARNING)

# Allowed production origins
DEFAULT_ALLOWED_ORIGINS = {
    "https://huntflow.online",
    "https://www.huntflow.online",
}


def is_origin_allowed(origin: Optional[str]) -> bool:
    """Validates WebSocket origin header against approved production domains or local dev."""
    if not origin:
        return False

    norm_origin = origin.strip().lower()
    if norm_origin in DEFAULT_ALLOWED_ORIGINS:
        return True

    # Allow localhost / 127.0.0.1 for testing and development
    if getattr(settings, "DEBUG", False) or os.environ.get("HUNTFLOW_ALLOW_LOCAL_ORIGINS") == "1":
        parsed = urllib.parse.urlparse(norm_origin)
        if parsed.hostname in ("127.0.0.1", "localhost", "testserver"):
            return True

    return False


class BootstrapSessionRuntime:
    """Manages the in-process Playwright Chromium lifecycle for a single bootstrap session."""

    def __init__(
        self,
        bootstrap_session_id: int,
        target_url: Optional[str] = None,
        lease_token: str = "",
    ):
        self.bootstrap_session_id = bootstrap_session_id
        self.target_url = target_url or os.environ.get("HUNTFLOW_SYNTHETIC_LOGIN_URL") or "https://www.linkedin.com/login"
        self.lease_token = lease_token
        self.playwright: Any = None
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self.cdp: Optional[CDPSession] = None
        self.active_ws: Optional[ServerConnection] = None
        self.is_running: bool = False
        self.is_closing: bool = False
        self.reconnect_task: Optional[asyncio.Task] = None
        self.heartbeat_task: Optional[asyncio.Task] = None
        self._frame_lock = asyncio.Lock()
        self._last_frame_sent_at = 0.0

    async def start_browser(self) -> None:
        """Launches headless Chromium and attaches server CDP screencast."""
        if self.is_running:
            return

        logger.info("Launching server-side Chromium for bootstrap session #%d", self.bootstrap_session_id)
        self.playwright = await async_playwright().start()
        launch_args = [
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--disable-software-rasterizer",
        ]

        self.browser = await self.playwright.chromium.launch(
            headless=True,
            args=launch_args,
        )

        self.context = await self.browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
        )

        self.page = await self.context.new_page()
        self.cdp = await self.context.new_cdp_session(self.page)

        # Attach CDP screencast frame listener
        self.cdp.on("Page.screencastFrame", self._on_screencast_frame)

        # Start screencast stream
        await self.cdp.send(
            "Page.startScreencast",
            {
                "format": "jpeg",
                "quality": 70,
                "maxWidth": 1280,
                "maxHeight": 800,
                "everyNthFrame": 1,
            },
        )

        # Navigate to destination URL
        logger.info(
            "Navigating server browser for bootstrap session #%d to initial URL",
            self.bootstrap_session_id,
        )
        await self.page.goto(self.target_url, wait_until="domcontentloaded")
        self.is_running = True

        # Start periodic lease renewal task
        self.heartbeat_task = asyncio.create_task(self._lease_heartbeat_loop())

    def _on_screencast_frame(self, event: Dict[str, Any]) -> None:
        """Handles ephemeral screencast frames from Chromium."""
        session_id = event.get("sessionId")
        asyncio.create_task(self._dispatch_frame(event, session_id))

    async def _dispatch_frame(self, event: Dict[str, Any], cdp_session_id: Optional[int]) -> None:
        """Dispatches an ephemeral frame over the active WebSocket and acknowledges CDP."""
        try:
            now = asyncio.get_event_loop().time()
            # Adaptive rate limiting: target 8-12 FPS (min 0.08s between frames)
            if now - self._last_frame_sent_at < 0.08:
                if self.cdp and cdp_session_id is not None:
                    await self.cdp.send("Page.screencastFrameAck", {"sessionId": cdp_session_id})
                return

            if self.active_ws and not self.is_closing:
                frame_data = event.get("data", "")
                if frame_data:
                    payload = json.dumps({
                        "type": "frame",
                        "data": frame_data,
                        "metadata": event.get("metadata", {}),
                    })
                    await self.active_ws.send(payload)
                    self._last_frame_sent_at = now

            if self.cdp and cdp_session_id is not None:
                await self.cdp.send("Page.screencastFrameAck", {"sessionId": cdp_session_id})
        except Exception:
            pass

    async def _lease_heartbeat_loop(self) -> None:
        """Periodically renews the ProviderAccountLease while the operator is active."""
        while self.is_running and not self.is_closing:
            await asyncio.sleep(60)
            if not self.is_running or self.is_closing:
                break
            try:
                renewed = await sync_to_async(self._do_lease_renew)()
                if not renewed:
                    logger.warning("Lease renewal failed for bootstrap session #%d. Closing.", self.bootstrap_session_id)
                    await self.teardown(reason="lease_renewal_failed")
                    break
            except Exception as e:
                logger.warning("Error in lease heartbeat loop: %s", e)

    def _do_lease_renew(self) -> bool:
        sess = ServerBootstrapSession.objects.filter(id=self.bootstrap_session_id).first()
        if not sess or sess.status != ServerBootstrapSession.Status.ACTIVE:
            return False
        from openoutreach.linkedin.services.account_governor import renew_provider_lease
        return renew_provider_lease(
            linkedin_profile=sess.linkedin_profile,
            owner_token=sess.lease_token,
            extension_seconds=300,
        )

    async def handle_input(self, msg: Dict[str, Any]) -> None:
        """Processes and forwards validated client input to the server Playwright page."""
        if not self.page or not self.is_running or self.is_closing:
            return

        msg_type = msg.get("type", "")
        # Safe logging: log ONLY the event type, never coordinates, characters, or text
        logger.debug("Bootstrap session #%d received input event: %s", self.bootstrap_session_id, msg_type)

        try:
            if msg_type == "mouse_move":
                x = int(msg.get("x", 0))
                y = int(msg.get("y", 0))
                if 0 <= x <= 1920 and 0 <= y <= 1200:
                    await self.page.mouse.move(x, y)

            elif msg_type == "mouse_down":
                x = int(msg.get("x", 0))
                y = int(msg.get("y", 0))
                btn = msg.get("button", "left")
                if btn not in ("left", "right", "middle"):
                    btn = "left"
                if 0 <= x <= 1920 and 0 <= y <= 1200:
                    await self.page.mouse.move(x, y)
                    await self.page.mouse.down(button=btn)

            elif msg_type == "mouse_up":
                x = int(msg.get("x", 0))
                y = int(msg.get("y", 0))
                btn = msg.get("button", "left")
                if btn not in ("left", "right", "middle"):
                    btn = "left"
                if 0 <= x <= 1920 and 0 <= y <= 1200:
                    await self.page.mouse.move(x, y)
                    await self.page.mouse.up(button=btn)

            elif msg_type == "wheel":
                dx = float(msg.get("delta_x", 0))
                dy = float(msg.get("delta_y", 0))
                dx = max(-1000.0, min(1000.0, dx))
                dy = max(-1000.0, min(1000.0, dy))
                await self.page.mouse.wheel(dx, dy)

            elif msg_type == "key_down":
                key = str(msg.get("key", ""))
                if key and len(key) <= 50:
                    await self.page.keyboard.down(key)

            elif msg_type == "key_up":
                key = str(msg.get("key", ""))
                if key and len(key) <= 50:
                    await self.page.keyboard.up(key)

            elif msg_type == "text_input":
                text = str(msg.get("text", ""))
                if text and len(text) <= 256:
                    await self.page.keyboard.insert_text(text)

        except Exception as e:
            logger.debug("Input dispatch error: %s", e)

    async def complete(self) -> Tuple[bool, str]:
        """Captures server storage state and marks bootstrap completed."""
        if not self.context:
            return False, "no_active_browser_context"

        logger.info("Capturing server storage state for bootstrap session #%d", self.bootstrap_session_id)
        success, msg = await complete_server_bootstrap_from_runtime(
            bootstrap_session_id=self.bootstrap_session_id,
            browser_context=self.context,
            caller_lease_token=self.lease_token,
        )
        await self.teardown(reason="completed")
        return success, msg

    async def abort(self, reason: str = "operator_aborted") -> Tuple[bool, str]:
        """Aborts the bootstrap session, closes browser, and releases lease."""
        def _do_abort():
            sess = ServerBootstrapSession.objects.filter(id=self.bootstrap_session_id).first()
            if sess and sess.status in (ServerBootstrapSession.Status.PENDING, ServerBootstrapSession.Status.ACTIVE):
                sess.status = ServerBootstrapSession.Status.ABORTED
                sess.aborted_at = timezone.now()
                sess.failure_reason = reason
                sess.viewer_connected = False
                sess.viewer_connection_id = ""
                sess.save(update_fields=["status", "aborted_at", "failure_reason", "viewer_connected", "viewer_connection_id"])
                from openoutreach.linkedin.services.server_bootstrap import _release_bootstrap_lease
                _release_bootstrap_lease(sess)
                return True, "aborted"
            return False, "cannot_abort"

        success, msg = await sync_to_async(_do_abort)()
        await self.teardown(reason=reason)
        return success, msg

    async def teardown(self, reason: str = "teardown") -> None:
        """Shuts down CDP, browser, context, and background tasks."""
        if self.is_closing:
            return
        self.is_closing = True
        self.is_running = False

        if self.heartbeat_task and not self.heartbeat_task.done():
            self.heartbeat_task.cancel()

        if self.reconnect_task and not self.reconnect_task.done():
            self.reconnect_task.cancel()

        try:
            if self.cdp:
                await self.cdp.send("Page.stopScreencast")
                await self.cdp.detach()
        except Exception:
            pass

        try:
            if self.context:
                await self.context.close()
        except Exception:
            pass

        try:
            if self.browser:
                await self.browser.close()
        except Exception:
            pass

        try:
            if self.playwright:
                await self.playwright.stop()
        except Exception:
            pass

        logger.info("Browser runtime torn down for bootstrap session #%d (reason: %s)", self.bootstrap_session_id, reason)


class BootstrapServer:
    """Async WebSocket server orchestrating authentication and browser runtimes."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 18081,
        allowed_origins: Optional[Set[str]] = None,
        synthetic_login_url: Optional[str] = None,
    ):
        self.host = host
        self.port = port
        self.allowed_origins = allowed_origins or DEFAULT_ALLOWED_ORIGINS
        self.synthetic_login_url = synthetic_login_url
        self.runtimes: Dict[int, BootstrapSessionRuntime] = {}
        self.server: Any = None

    async def start(self) -> None:
        """Starts the WebSocket server."""
        self.server = await serve(
            self.handle_connection,
            self.host,
            self.port,
            max_size=65536,  # 64KB message cap
        )
        logger.info("Bootstrap WebSocket server listening on %s:%d", self.host, self.port)

    async def stop(self) -> None:
        """Stops the server and tears down all active runtimes."""
        if self.server:
            self.server.close()
            await self.server.wait_closed()

        for runtime in list(self.runtimes.values()):
            await runtime.teardown(reason="server_shutdown")
        self.runtimes.clear()
        logger.info("Bootstrap WebSocket server stopped.")

    async def handle_connection(self, websocket: ServerConnection) -> None:
        """Handles incoming WebSocket connections through the full security lifecycle."""
        path = getattr(websocket.request, "path", "")
        match = re.match(r"^/ws/bootstrap/(\d+)/?$", path)
        if not match:
            await websocket.close(4004, "Invalid route")
            return

        bootstrap_id = int(match.group(1))

        # 1. Origin Validation
        headers = websocket.request.headers
        origin = headers.get("Origin")
        if not is_origin_allowed(origin):
            logger.warning("Rejected bootstrap WS connection with forbidden origin: %s", origin)
            await websocket.close(4003, "Forbidden origin")
            return

        # 2. Django Session Authentication
        cookie_header = headers.get("Cookie", "")
        session_key = self._extract_cookie(cookie_header, getattr(settings, "SESSION_COOKIE_NAME", "sessionid"))
        if not session_key:
            logger.warning("Rejected bootstrap WS connection: missing session cookie")
            await websocket.close(4001, "Missing session cookie")
            return

        user = await sync_to_async(self._authenticate_session)(session_key)
        if not user:
            logger.warning("Rejected bootstrap WS connection: invalid or expired session")
            await websocket.close(4001, "Invalid session")
            return

        # 3. Bootstrap Session & Tenant Authorization
        sess_data = await sync_to_async(self._get_bootstrap_session_data)(bootstrap_id, user.id)
        if not sess_data["ok"]:
            logger.warning("Rejected bootstrap WS connection for session #%d: %s", bootstrap_id, sess_data["error"])
            close_code = 4003 if "tenant" in sess_data["error"] or "operator" in sess_data["error"] else 4004
            await websocket.close(close_code, sess_data["error"])
            return

        session_obj = sess_data["session"]
        token_hash = session_obj.token_hash
        lease_token = session_obj.lease_token

        # 4. Handshake Deadline: Wait up to 10 seconds for the first authorization message
        try:
            raw_msg = await asyncio.wait_for(websocket.recv(), timeout=10.0)
            msg = json.loads(raw_msg)
        except asyncio.TimeoutError:
            logger.warning("Bootstrap WS connection timed out waiting for authorization handshake (#%d)", bootstrap_id)
            await websocket.close(4008, "Handshake timeout")
            return
        except Exception:
            await websocket.close(4000, "Invalid handshake payload")
            return

        if msg.get("type") != "authorize" or not msg.get("secret"):
            await websocket.close(4003, "Invalid handshake type")
            return

        raw_secret = str(msg.get("secret", "")).strip()
        computed_hash = hash_bootstrap_token(raw_secret)
        if not hmac.compare_digest(computed_hash, token_hash):
            logger.warning("Bootstrap authorization secret mismatch for session #%d", bootstrap_id)
            await websocket.close(4003, "Invalid bootstrap secret")
            return

        # 5. Atomic Viewer Lock
        connection_id = secrets.token_hex(16)
        claimed = await sync_to_async(claim_bootstrap_viewer)(bootstrap_id, connection_id, token_hash)
        if not claimed:
            logger.warning("Rejected bootstrap WS connection: concurrent viewer already active for #%d", bootstrap_id)
            await websocket.close(4009, "Concurrent viewer not permitted")
            return

        # 6. Initialize or Re-attach Browser Runtime
        runtime = self.runtimes.get(bootstrap_id)
        if not runtime or not runtime.is_running:
            target_url = self.synthetic_login_url or os.environ.get("HUNTFLOW_SYNTHETIC_LOGIN_URL")
            runtime = BootstrapSessionRuntime(
                bootstrap_session_id=bootstrap_id,
                target_url=target_url,
                lease_token=lease_token,
            )
            self.runtimes[bootstrap_id] = runtime
            try:
                await runtime.start_browser()
            except Exception as e:
                logger.error("Failed to launch browser for bootstrap #%d: %s", bootstrap_id, e)
                await sync_to_async(release_bootstrap_viewer)(bootstrap_id, connection_id)
                await websocket.close(4500, "Browser launch failed")
                return
        else:
            # Reconnecting: cancel any pending disconnect teardown
            if runtime.reconnect_task and not runtime.reconnect_task.done():
                runtime.reconnect_task.cancel()

        runtime.active_ws = websocket
        await websocket.send(json.dumps({"type": "authorized", "message": "Bootstrap authorized. Screencast active."}))

        # 7. Main Input Event Loop
        try:
            async for raw_input in websocket:
                try:
                    input_data = json.loads(raw_input)
                    action = input_data.get("type")

                    if action == "complete":
                        success, result_msg = await runtime.complete()
                        if success:
                            await websocket.send(json.dumps({"type": "completed", "message": result_msg}))
                        else:
                            await websocket.send(json.dumps({"type": "error", "message": result_msg}))
                        break

                    elif action == "abort":
                        await runtime.abort(reason="operator_requested")
                        await websocket.send(json.dumps({"type": "aborted", "message": "Bootstrap aborted."}))
                        break

                    elif action == "heartbeat":
                        await websocket.send(json.dumps({"type": "heartbeat_ack"}))

                    else:
                        await runtime.handle_input(input_data)

                except json.JSONDecodeError:
                    pass
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            # Viewer disconnected
            await sync_to_async(release_bootstrap_viewer)(bootstrap_id, connection_id)
            runtime.active_ws = None

            # If not completed or aborted, schedule a 30-second disconnect teardown
            if runtime.is_running and not runtime.is_closing:
                runtime.reconnect_task = asyncio.create_task(self._delayed_disconnect_teardown(bootstrap_id, runtime))

    async def _delayed_disconnect_teardown(self, bootstrap_id: int, runtime: BootstrapSessionRuntime) -> None:
        """Waits 30 seconds for a reconnect; if none occurs, aborts and tears down."""
        try:
            await asyncio.sleep(30)
            if runtime.is_running and runtime.active_ws is None:
                logger.info("Grace period expired for disconnected bootstrap #%d. Tearing down.", bootstrap_id)
                await runtime.abort(reason="operator_disconnected_timeout")
                self.runtimes.pop(bootstrap_id, None)
        except asyncio.CancelledError:
            pass

    def _extract_cookie(self, cookie_header: str, cookie_name: str) -> Optional[str]:
        """Parses a specific cookie from the raw Cookie header."""
        if not cookie_header:
            return None
        pairs = [c.strip() for c in cookie_header.split(";")]
        for pair in pairs:
            if "=" in pair:
                k, v = pair.split("=", 1)
                if k.strip() == cookie_name:
                    return v.strip()
        return None

    def _authenticate_session(self, session_key: str) -> Optional[User]:
        """Validates Django session from Postgres and returns the authenticated User."""
        try:
            session = Session.objects.filter(session_key=session_key).first()
            if not session:
                return None
            if session.expire_date <= timezone.now():
                return None
            data = session.get_decoded()
            user_id = data.get("_auth_user_id")
            if not user_id:
                return None
            user = User.objects.filter(pk=user_id, is_active=True).first()
            return user
        except Exception as e:
            logger.warning("Session authentication exception: %s", e)
            return None

    def _get_bootstrap_session_data(self, bootstrap_id: int, user_id: int) -> Dict[str, Any]:
        """Validates tenant isolation and bootstrap state for the session."""
        sess = ServerBootstrapSession.objects.filter(id=bootstrap_id).first()
        if not sess:
            return {"ok": False, "error": "bootstrap_not_found"}

        if sess.status not in (ServerBootstrapSession.Status.PENDING, ServerBootstrapSession.Status.ACTIVE):
            return {"ok": False, "error": f"bootstrap_not_active_{sess.status}"}

        if sess.expires_at <= timezone.now():
            sess.status = ServerBootstrapSession.Status.EXPIRED
            sess.save(update_fields=["status"])
            return {"ok": False, "error": "bootstrap_expired"}

        if sess.operator_id != user_id:
            return {"ok": False, "error": "operator_mismatch"}

        # Verify active membership in workspace
        membership = WorkspaceMembership.objects.filter(
            workspace=sess.workspace,
            user_id=user_id,
            status__in=["active", "ACTIVE"],
        ).first()
        if not membership:
            return {"ok": False, "error": "tenant_isolation_forbidden"}

        return {"ok": True, "session": sess}
