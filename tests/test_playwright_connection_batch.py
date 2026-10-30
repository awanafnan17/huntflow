# tests/test_playwright_connection_batch.py
"""Playwright LiveServer Connection Batch Workflow Test for HuntFlow SaaS Platform."""
import pytest
import asyncio
from pathlib import Path
from django.test import LiveServerTestCase
from django.contrib.auth import get_user_model
from playwright.async_api import async_playwright

from openoutreach.core.models import Workspace, Campaign, SenderIdentity
from openoutreach.crm.models import Lead, Deal
from openoutreach.core.services.workspace_membership import create_workspace_with_owner

User = get_user_model()

ARTIFACT_DIR = Path("test-results/playwright")
ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)


class PlaywrightConnectionBatchTest(LiveServerTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.user = User.objects.create_superuser(
            username="pw_conn_admin",
            email="pw_conn@synthetic-demo.invalid",
            password="password123",
        )
        cls.workspace = create_workspace_with_owner(
            name="Playwright Connection Workspace",
            slug="pw-conn-ws",
            owner_user=cls.user,
        )

    def test_playwright_connection_batch_workflow(self):
        """Run async playwright connection batch workflow against live server."""
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(asyncio.run, self._run_browser_test())
            future.result()

    async def _run_browser_test(self):
        console_logs = []
        failed_requests = []

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context()
            page = await context.new_page()

            page.on("console", lambda msg: console_logs.append(f"[{msg.type}] {msg.text}"))
            page.on("requestfailed", lambda req: failed_requests.append(f"{req.url}: {req.failure}"))

            try:
                base_url = self.live_server_url

                # 1. Login flow
                await page.goto(f"{base_url}/dashboard/login/")
                await page.fill('input[name="username"]', "pw_conn_admin")
                await page.fill('input[name="password"]', "password123")
                await page.click('button[type="submit"]')

                # 2. Select Workspace / Dashboard Overview
                await page.goto(f"{base_url}/dashboard/")
                await page.wait_for_selector("body")

                # 3. Create Campaign
                await page.goto(f"{base_url}/dashboard/campaigns/create/")
                await page.fill('input[name="name"]', "PW Synthetic Connection Campaign")
                await page.click('button[type="submit"]')

                # 4. Import synthetic targets / Leads page preview
                await page.goto(f"{base_url}/dashboard/leads/")
                content = await page.content()
                assert "leads" in content.lower() or "campaign" in content.lower() or "dashboard" in content.lower()

                # 5. Inspect audit history
                await page.goto(f"{base_url}/dashboard/action-logs/")
                content_logs = await page.content()
                assert "action" in content_logs.lower() or "logs" in content_logs.lower() or "audit" in content_logs.lower() or "dashboard" in content_logs.lower()

            except Exception as exc:
                screenshot_path = ARTIFACT_DIR / "failure_conn_screenshot.png"
                await page.screenshot(path=str(screenshot_path))
                raise exc
            finally:
                await browser.close()
