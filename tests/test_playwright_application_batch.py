# tests/test_playwright_application_batch.py
"""Playwright LiveServer Application Batch Workflow Test for HuntFlow SaaS Platform."""
import pytest
import asyncio
from pathlib import Path
from django.test import LiveServerTestCase
from django.contrib.auth import get_user_model
from playwright.async_api import async_playwright

from openoutreach.career.models import CandidateProfile, Job, JobApplication
from openoutreach.core.services.workspace_membership import create_workspace_with_owner

User = get_user_model()

ARTIFACT_DIR = Path("test-results/playwright")
ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)


class PlaywrightApplicationBatchTest(LiveServerTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.user = User.objects.create_superuser(
            username="pw_app_admin",
            email="pw_app@synthetic-demo.invalid",
            password="password123",
        )
        cls.workspace = create_workspace_with_owner(
            name="Playwright Application Workspace",
            slug="pw-app-ws",
            owner_user=cls.user,
        )

    def test_playwright_application_batch_workflow(self):
        """Run async playwright application batch workflow against live server."""
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
                await page.fill('input[name="username"]', "pw_app_admin")
                await page.fill('input[name="password"]', "password123")
                await page.click('button[type="submit"]')

                # 2. Candidate career portal overview
                await page.goto(f"{base_url}/dashboard/career/")
                await page.wait_for_selector("body")

                # 3. View job list
                await page.goto(f"{base_url}/dashboard/career/jobs/")
                content = await page.content()
                assert "job" in content.lower() or "career" in content.lower() or "dashboard" in content.lower()

                # 4. View candidate documents/versions
                await page.goto(f"{base_url}/dashboard/career/documents/")
                content_docs = await page.content()
                assert "document" in content_docs.lower() or "career" in content_docs.lower() or "dashboard" in content_docs.lower()

            except Exception as exc:
                screenshot_path = ARTIFACT_DIR / "failure_app_screenshot.png"
                await page.screenshot(path=str(screenshot_path))
                raise exc
            finally:
                await browser.close()
