# tests/test_playwright_live_server_smoke.py
"""Playwright LiveServer Integration Test Suite for HuntFlow SaaS Platform."""
import os
import pytest
import asyncio
from pathlib import Path
from django.test import LiveServerTestCase
from django.contrib.auth import get_user_model
from playwright.async_api import async_playwright

from openoutreach.core.models import (
    Workspace,
    CampaignPurpose,
    CampaignPurposeSequenceTemplate,
    PromptPack,
    PromptPackVersion,
    CampaignPurposeVersion,
)
from openoutreach.core.services.workspace_membership import create_workspace_with_owner

User = get_user_model()

ARTIFACT_DIR = Path("test-results/playwright")
ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)


class PlaywrightLiveServerSmokeTest(LiveServerTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.user = User.objects.create_superuser(
            username="playwright_admin",
            email="pw@synthetic-demo.invalid",
            password="password123",
        )
        cls.workspace = create_workspace_with_owner(
            name="Playwright Live Workspace",
            slug="playwright-live-ws",
            owner_user=cls.user,
        )

        # Seed prompt pack and purpose versions
        for purpose in CampaignPurpose.objects.filter(is_system=True):
            pack, _ = PromptPack.objects.get_or_create(
                name=f"{purpose.name} Pack",
                defaults={"campaign_purpose": purpose},
            )
            pv, _ = PromptPackVersion.objects.get_or_create(
                prompt_pack=pack,
                version_number=1,
                defaults={"prompt_template": "Hi {recipient_name}, connecting regarding {company_name}."},
            )
            CampaignPurposeVersion.objects.get_or_create(
                campaign_purpose=purpose,
                version_number=1,
            )
            if not purpose.sequence_templates.exists():
                CampaignPurposeSequenceTemplate.objects.create(
                    campaign_purpose=purpose,
                    sequence_number=1,
                    channel="linkedin",
                    step_type="connection_note",
                )

    def test_browser_live_server_smoke(self):
        """Run async playwright browser automation test against live server."""
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
                await page.fill('input[name="username"]', "playwright_admin")
                await page.fill('input[name="password"]', "password123")
                await page.click('button[type="submit"]')

                # Verify dashboard overview loaded
                await page.wait_for_selector("body")

                # 2. Navigate to Onboarding
                await page.goto(f"{base_url}/dashboard/onboarding/")
                await page.fill('input[name="name"]', "Browser Form Workspace")
                await page.fill('input[name="slug"]', "browser-form-ws")
                await page.click('button[type="submit"]')

                # 3. Onboarding Sender
                await page.fill('input[name="display_name"]', "Live Browser Sender")
                await page.fill('input[name="email"]', "browser_sender@synthetic-demo.invalid")
                await page.click('button[type="submit"]')

                # 4. Onboarding Purpose
                await page.click('button[type="submit"]')

                # 5. Verify Health check public JSON endpoint
                await page.goto(f"{base_url}/health/")
                content = await page.content()
                assert "healthy" in content.lower()

                # 6. Verify Readiness check public JSON endpoint
                await page.goto(f"{base_url}/readiness/")
                content = await page.content()
                assert "ready" in content.lower()

            except Exception as exc:
                # Capture artifacts on failure
                screenshot_path = ARTIFACT_DIR / "failure_screenshot.png"
                html_path = ARTIFACT_DIR / "failure_dom.html"
                logs_path = ARTIFACT_DIR / "console_logs.txt"

                await page.screenshot(path=str(screenshot_path))
                html_content = await page.content()
                with open(html_path, "w", encoding="utf-8") as f:
                    f.write(html_content)

                with open(logs_path, "w", encoding="utf-8") as f:
                    f.write("\n".join(console_logs))
                    f.write("\n--- Failed Requests ---\n")
                    f.write("\n".join(failed_requests))

                raise exc
            finally:
                await browser.close()
