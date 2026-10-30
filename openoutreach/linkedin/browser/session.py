# openoutreach/linkedin/browser/session.py
from __future__ import annotations

import logging
import random
import time
from functools import cached_property

from django.utils import timezone
from openoutreach.core.conf import MIN_DELAY, MAX_DELAY

logger = logging.getLogger(__name__)

# The main LinkedIn auth cookie
_AUTH_COOKIE_NAME = "li_at"


def random_sleep(min_val, max_val):
    delay = random.uniform(min_val, max_val)
    logger.debug(f"Pause: {delay:.2f}s")
    time.sleep(delay)


class AccountSession:
    def __init__(self, linkedin_profile):
        self.linkedin_profile = linkedin_profile
        self.django_user = linkedin_profile.user

        # Active campaign — set by the daemon before each lane execution
        self.campaign = None

        # Playwright objects – created on first access or after crash
        self.page = None
        self.context = None
        self.browser = None
        self.playwright = None
        self._owned_lease_cm = None

    @cached_property
    def campaigns(self):
        """All campaigns this user/profile belongs to (cached)."""
        from openoutreach.core.models import Campaign
        from django.db.models import Q
        q = Q()
        if self.django_user:
            q |= Q(users=self.django_user)
        if self.linkedin_profile:
            q |= Q(linkedin_profile=self.linkedin_profile)
            if getattr(self.linkedin_profile, "workspace", None):
                q |= Q(workspace=self.linkedin_profile.workspace)
        return list(Campaign.objects.filter(q).distinct())

    def ensure_browser(self):
        """Launch or recover browser + login if needed. Call before using .page.
        
        Guarantees:
        1. Fail-closed: Rejects browser instantiation if session_status requires re-authentication.
        2. Concurrency serialization: Ensures a ProviderAccountLease covers the browser lifecycle.
        """
        from openoutreach.linkedin.browser.launch import start_browser_session

        # 1. Fail-closed check: require valid session_status
        def _do_refresh_status():
            from django.db import connection
            connection.close()
            self.linkedin_profile.refresh_from_db(fields=["session_status", "active"])
        try:
            import asyncio
            asyncio.get_running_loop()
            import threading
            t = threading.Thread(target=_do_refresh_status)
            t.start()
            t.join()
        except RuntimeError:
            _do_refresh_status()

        if not getattr(self.linkedin_profile, "active", True):
            from linkedin_cli.exceptions import AuthenticationError
            raise AuthenticationError(f"LinkedInProfile #{self.linkedin_profile.pk} is disabled/inactive.")

        status = getattr(self.linkedin_profile, "session_status", "unknown")
        if status in ["needs_reauth", "expired", "invalid", "checkpoint", "restricted", "challenge_required"]:
            from linkedin_cli.exceptions import AuthenticationError
            raise AuthenticationError(
                f"Session status '{status}' for LinkedInProfile #{self.linkedin_profile.pk} requires re-authentication. Browser launch blocked."
            )

        # Check operator manual provider use pause
        ws = getattr(self.linkedin_profile, "workspace", None) or (
            getattr(self, "campaign", None) and self.campaign.workspace
        )
        if ws:
            from openoutreach.core.models import WorkspaceAutomationSettings
            auto_st = WorkspaceAutomationSettings.get_for_workspace(ws)
            if auto_st and getattr(auto_st, "manual_provider_use_paused", False):
                task = getattr(self, "task", None)
                is_explicit_verify = bool(
                    task and getattr(task, "task_type", None) == "session_health_verification"
                    and getattr(task, "operator_initiated", False)
                )
                if not is_explicit_verify:
                    raise RuntimeError(
                        f"ManualProviderUsePaused: LinkedInProfile #{self.linkedin_profile.pk} provider execution is temporarily paused by operator for manual use. Browser launch blocked."
                    )

        # 2. Concurrency serialization: Acquire full browser lifecycle lease if not already held by caller
        from openoutreach.linkedin.services.account_governor import (
            account_provider_lock,
            get_active_profile_lease_token,
        )

        active_token = get_active_profile_lease_token(self.linkedin_profile.pk)
        if not active_token and self._owned_lease_cm is None:
            ws = getattr(self.linkedin_profile, "workspace", None) or (
                getattr(self, "campaign", None) and self.campaign.workspace
            )
            lease_cm = account_provider_lock(
                linkedin_profile=self.linkedin_profile,
                workspace=ws,
                operation_name="browser_lifecycle",
                lease_seconds=300,
            )
            locked_prof = lease_cm.__enter__()
            if locked_prof is None:
                raise RuntimeError(
                    f"ProviderAccountLeaseConflict: LinkedInProfile #{self.linkedin_profile.pk} is actively leased by another process. Browser launch blocked."
                )
            self._owned_lease_cm = lease_cm

        if not self.page or self.page.is_closed():
            logger.debug("Launching/recovering browser for %s", self)
            try:
                start_browser_session(session=self)
            except Exception:
                self.close()
                raise
        else:
            self._maybe_refresh_cookies()

    @cached_property
    def self_profile(self) -> dict:
        """Authenticated user's profile dict, fetched once per session.

        The dict isn't persisted to DB (we dropped ``Lead.profile_data``),
        so the first access per session triggers a Voyager call via the
        ``linkedin_cli`` self-discovery primitive; the ``cached_property``
        keeps it warm for the rest of the session. CRM-side persistence
        (the disqualified ``self_lead``) is layered on in ``register_self_lead``.
        """
        from linkedin_cli.setup.self_profile import discover_self_profile
        from openoutreach.linkedin.db.leads import register_self_lead

        from linkedin_cli.exceptions import AuthenticationError

        try:
            profile = discover_self_profile(self)
            register_self_lead(self, profile)
            return profile
        except AuthenticationError as e:
            logger.warning("LinkedIn API 401 Unauthorized for %s — marking session EXPIRED: %s", self, e)
            self.linkedin_profile.session_status = "expired"
            self.linkedin_profile.session_error_details = str(e)
            try:
                self.linkedin_profile.save(update_fields=["session_status", "session_error_details"])
            except Exception:
                pass

            fallback = {
                "public_identifier": self.linkedin_profile.linkedin_username.split("@")[0],
                "first_name": "Operator",
                "last_name": "Account",
                "country_code": "US",
            }
            register_self_lead(self, fallback)
            return fallback
        except Exception as e:
            logger.warning("Could not discover self_profile via Voyager API (%s) — using fallback profile", e)
            fallback = {
                "public_identifier": self.linkedin_profile.linkedin_username.split("@")[0],
                "first_name": "Operator",
                "last_name": "Account",
                "country_code": "US",
            }
            register_self_lead(self, fallback)
            return fallback

    @cached_property
    def active_timezone(self) -> str | None:
        """IANA zone for the active-hours window, resolved once per session.

        An explicit ``ACTIVE_TIMEZONE`` in conf wins (operator override);
        otherwise the zone is inferred from the LinkedIn profile country.
        None when neither yields a zone — the scheduler/daemon treat None as
        "no active-hours gating" rather than guessing UTC. Resolving via
        ``self_profile`` means this fires only after login.
        """
        from openoutreach.core.conf import ACTIVE_TIMEZONE
        from openoutreach.core.tz_country import timezone_for_country

        if ACTIVE_TIMEZONE:
            return ACTIVE_TIMEZONE
        return timezone_for_country(self.self_profile.get("country_code"))

    def active_timezone_provenance(self) -> str:
        """Human-readable note on where ``active_timezone`` came from — used in
        the daemon's active-hours log so an inferred (and possibly wrong) zone
        is visible and overridable."""
        from openoutreach.core.conf import ACTIVE_TIMEZONE

        if ACTIVE_TIMEZONE:
            return f"{ACTIVE_TIMEZONE} (configured via ACTIVE_TIMEZONE)"
        tz = self.active_timezone
        country = (self.self_profile.get("country_code") or "?").upper()
        if tz:
            return (
                f"{tz} (inferred from LinkedIn profile country {country}; "
                "override with ACTIVE_TIMEZONE)"
            )
        return "unknown (no profile country and no ACTIVE_TIMEZONE) — not gating"

    def wait(self, min_delay=MIN_DELAY, max_delay=MAX_DELAY):
        random_sleep(min_delay, max_delay)
        self.page.wait_for_load_state("domcontentloaded")

    def reauthenticate(self):
        """Force a fresh login: close browser, clear saved cookies, re-launch."""
        from openoutreach.linkedin.browser.launch import start_browser_session

        logger.warning("Re-authenticating %s — clearing saved session", self)
        self.close()
        self.linkedin_profile.cookie_data = None
        def _do_clear():
            from django.db import connection
            connection.close()
            self.linkedin_profile.save(update_fields=["cookie_data"])
        try:
            import asyncio
            asyncio.get_running_loop()
            import threading
            t = threading.Thread(target=_do_clear)
            t.start()
            t.join()
        except RuntimeError:
            _do_clear()
        start_browser_session(session=self)

    def _maybe_refresh_cookies(self):
        """Re-login if the li_at auth cookie in the saved DB state is expired."""
        from openoutreach.linkedin.browser.launch import start_browser_session

        def _do_refresh_db():
            from django.db import connection
            connection.close()
            self.linkedin_profile.refresh_from_db(fields=["cookie_data"])
        try:
            import asyncio
            asyncio.get_running_loop()
            import threading
            t = threading.Thread(target=_do_refresh_db)
            t.start()
            t.join()
        except RuntimeError:
            _do_refresh_db()

        cookie_data = self.linkedin_profile.cookie_data
        if not cookie_data:
            return
        if isinstance(cookie_data, str) and cookie_data.startswith("gcm_enc::"):
            from openoutreach.core.encryption import decrypt_dict
            cookie_data = decrypt_dict(cookie_data)
        if not isinstance(cookie_data, dict):
            return
        for cookie in cookie_data.get("cookies", []):
            if cookie.get("name") == _AUTH_COOKIE_NAME:
                expires = cookie.get("expires", -1)
                if expires > 0 and expires < time.time():
                    logger.warning("Auth cookie expired for %s — re-authenticating", self)
                    self.close()
                    start_browser_session(session=self)
                return

    def persist_storage_state(self) -> bool:
        """Capture and persist the current Playwright storage state (cookies) to the DB.
        
        Requirements:
        - capture self.context.storage_state()
        - require structurally valid dictionary with a 'cookies' list
        - require 'li_at' cookie to remain present in the cookies list
        - reject malformed/unexpected structures fail closed
        - preserve complete supported browser storage state including server-minted
          auxiliary cookies (bcookie, bscookie, JSESSIONID, lidc, etc.)
        - schema versioning: ensure 'version': 1 is stamped
        - use self.linkedin_profile.set_cookie_data(state) to encrypt with AES-256-GCM
        - persist to DB with save(update_fields=['cookie_data'])
        - never print cookie values or log sensitive credentials
        """
        if not self.context:
            return False

        try:
            state = self.context.storage_state()
        except Exception as exc:
            logger.warning("Failed to extract storage state from context for %s: %s", self, exc)
            return False

        if not isinstance(state, dict):
            logger.error("Extracted storage state is not a dictionary for %s", self)
            return False

        cookies = state.get("cookies")
        if not isinstance(cookies, list) or not cookies:
            logger.warning("Extracted storage state contains no cookies for %s — refusing to overwrite DB", self)
            return False

        # Require 'li_at' cookie to remain present
        li_at_cookie = next(
            (c for c in cookies if isinstance(c, dict) and c.get("name") == _AUTH_COOKIE_NAME and c.get("value")),
            None
        )
        if not li_at_cookie:
            logger.warning(
                "Extracted storage state missing li_at cookie for %s — auth lost; marking needs_reauth without overwriting stored credential",
                self
            )
            self.linkedin_profile.session_status = "needs_reauth"
            self.linkedin_profile.session_error_details = "Browser session lost li_at authentication cookie."
            try:
                self.linkedin_profile.save(update_fields=["session_status", "session_error_details"])
            except Exception as e:
                logger.debug("Failed updating profile status: %s", e)
            return False

        # Stamp version marker for schema stability
        if "version" not in state:
            state["version"] = 1

        # Check provenance: if huntflow_managed, delegate to persist_managed_storage_state
        if getattr(self.linkedin_profile, "session_origin", "") == "huntflow_managed" and hasattr(self.linkedin_profile, "persist_managed_storage_state"):
            def _do_managed_save():
                from django.db import connection
                connection.close()
                self.linkedin_profile.persist_managed_storage_state(state)

            try:
                import asyncio
                asyncio.get_running_loop()
                import threading
                t = threading.Thread(target=_do_managed_save)
                t.start()
                t.join()
            except RuntimeError:
                _do_managed_save()
        else:
            # Use canonical encrypted setter preserving existing provenance
            self.linkedin_profile.set_cookie_data(state)
            if hasattr(self.linkedin_profile, "last_server_state_persisted_at"):
                self.linkedin_profile.last_server_state_persisted_at = timezone.now()

            def _do_save():
                from django.db import connection
                connection.close()
                update_fields = ["cookie_data"]
                if hasattr(self.linkedin_profile, "last_server_state_persisted_at"):
                    update_fields.append("last_server_state_persisted_at")
                self.linkedin_profile.save(update_fields=update_fields)

            try:
                import asyncio
                asyncio.get_running_loop()
                import threading
                t = threading.Thread(target=_do_save)
                t.start()
                t.join()
            except RuntimeError:
                _do_save()

        cookie_count = len(cookies)
        auxiliary_names = sorted(list(set(c.get("name") for c in cookies if isinstance(c, dict) and c.get("name") != _AUTH_COOKIE_NAME)))
        logger.info(
            "Persisted updated canonical encrypted storage state for %s (%d cookies, auxiliary: %s)",
            self, cookie_count, auxiliary_names
        )
        return True

    def close(self):
        # 1. Attempt storage state persistence before context is destroyed
        if self.context:
            try:
                self.persist_storage_state()
            except Exception as e:
                logger.warning("Error persisting storage state during close for %s: %s", self, e)

        # 2. Safely close Playwright objects
        try:
            if self.page:
                try:
                    self.page.close()
                except Exception:
                    pass
            if self.context:
                try:
                    self.context.close()
                except Exception:
                    pass
            if self.browser:
                try:
                    self.browser.close()
                except Exception:
                    pass
            if self.playwright:
                try:
                    self.playwright.stop()
                except Exception:
                    pass
            logger.info("Browser closed gracefully (%s)", self)
        except Exception as e:
            logger.debug("Error closing browser: %s", e)
        finally:
            self.page = self.context = self.browser = self.playwright = None

            # 3. Release lifecycle lease if owned by this session
            if self._owned_lease_cm:
                try:
                    self._owned_lease_cm.__exit__(None, None, None)
                except Exception as e:
                    logger.debug("Error releasing session lifecycle lease: %s", e)
                finally:
                    self._owned_lease_cm = None

        logger.info("Account session closed → %s", self)

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    def __repr__(self) -> str:
        return self.linkedin_profile.linkedin_username
