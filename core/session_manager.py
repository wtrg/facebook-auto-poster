"""Browser session lifecycle, persistent context management, and cookie handling."""

import os
import time
import random
import logging
from pathlib import Path
from typing import Optional, List, Dict, Any
from playwright.sync_api import sync_playwright, BrowserContext, Page, Playwright

logger = logging.getLogger("facebook_auto_poster")


class FacebookSessionManager:
    """
    Manages Playwright Chromium browser lifecycle with persistent user profile,
    credential injection, and safe resource cleanup on startup or shutdown.
    """
    def __init__(
        self,
        profile_dir: str,
        raw_cookie: str = "",
        delay_multiplier: float = 1.0
    ):
        self.profile_dir = Path(profile_dir).resolve()
        self.raw_cookie = raw_cookie.strip()
        self.delay_multiplier = delay_multiplier
        self.playwright: Optional[Playwright] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None

    def start_browser(
        self,
        headless: bool = False,
        user_data_dir: Optional[str] = None
    ) -> Page:
        """
        Start Chromium persistent context. If startup fails, cleanly release
        any partially created resources before raising to prevent leaks.
        """
        if self.context is not None and self.page is not None:
            return self.page

        try:
            target_dir = Path(user_data_dir).resolve() if user_data_dir else self.profile_dir
            target_dir.mkdir(parents=True, exist_ok=True)

            self.playwright = sync_playwright().start()
            self.context = self.playwright.chromium.launch_persistent_context(
                user_data_dir=str(target_dir),
                headless=headless,
                viewport={"width": 1366, "height": 768},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-notifications"
                ]
            )
            if self.raw_cookie:
                self._inject_base_cookie(self.raw_cookie)

            self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
            return self.page
        except Exception as e:
            # Release partially created browser resources on startup failure
            self.close()
            raise RuntimeError(f"Failed to start browser session: {e}") from e

    def _inject_base_cookie(self, cookie_str: str) -> None:
        """Parse and inject cookie name-value pairs, strictly ignoring i_user."""
        if not self.context or not cookie_str:
            return

        cookies: List[Dict[str, str]] = []
        for item in cookie_str.split(";"):
            item = item.strip()
            if "=" in item:
                name, value = item.split("=", 1)
                name_clean = name.strip()
                val_clean = value.strip()
                if not name_clean or name_clean.lower() == "i_user":
                    continue
                cookies.append({
                    "name": name_clean,
                    "value": val_clean,
                    "domain": ".facebook.com",
                    "path": "/"
                })
        if cookies:
            self.context.add_cookies(cookies)

    def switch_to_page_profile(self, page_id: str) -> None:
        """
        Navigate to target Page URL.
        Note: Navigating or setting cookie is NOT proof of publishing actor identity.
        Positive UI verification must follow.
        """
        if not self.page:
            raise RuntimeError("Browser session not started.")

        logger.info(f"Đang chuyển hướng tới Page: {page_id}...")
        url = f"https://www.facebook.com/{page_id}"
        self.page.goto(url, wait_until="domcontentloaded")
        self.human_delay(1.0, 2.5)

    def human_delay(self, min_sec: float = 2.0, max_sec: float = 5.0) -> None:
        """Simulate human pacing delay. If delay_multiplier is 0, skips sleep."""
        if self.delay_multiplier <= 0:
            return
        actual_min = min(min_sec, max_sec)
        actual_max = max(min_sec, max_sec)
        delay = random.uniform(actual_min, actual_max) * self.delay_multiplier
        if delay > 0:
            time.sleep(delay)

    def close(self) -> None:
        """Safely release all browser and Playwright resources."""
        if self.page:
            try:
                self.page.close()
            except Exception:
                pass
            self.page = None

        if self.context:
            try:
                self.context.close()
            except Exception:
                pass
            self.context = None

        if self.playwright:
            try:
                self.playwright.stop()
            except Exception:
                pass
            self.playwright = None
