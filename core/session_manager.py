import time
import random
import logging
from playwright.sync_api import sync_playwright, BrowserContext, Page

class FacebookSessionManager:
    def __init__(self, profile_dir: str, raw_cookie: str = ""):
        self.profile_dir = profile_dir
        self.raw_cookie = raw_cookie
        self.playwright = None
        self.context: BrowserContext = None
        self.page: Page = None

    def start_browser(self, headless: bool = False):
        self.playwright = sync_playwright().start()
        self.context = self.playwright.chromium.launch_persistent_context(
            user_data_dir=self.profile_dir,
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

    def _inject_base_cookie(self, cookie_str: str):
        cookies = []
        for item in cookie_str.split(";"):
            item = item.strip()
            if "=" in item:
                name, value = item.split("=", 1)
                if name.strip() == "i_user":
                    continue
                cookies.append({
                    "name": name.strip(),
                    "value": value.strip(),
                    "domain": ".facebook.com",
                    "path": "/"
                })
        if cookies:
            self.context.add_cookies(cookies)

    def switch_to_page_profile(self, page_id: str):
        logging.info(f"Đang chuyển ngữ cảnh sang Page ID: {page_id}...")
        self.context.add_cookies([{
            "name": "i_user",
            "value": str(page_id),
            "domain": ".facebook.com",
            "path": "/"
        }])
        self.page.goto(f"https://www.facebook.com/{page_id}", wait_until="domcontentloaded")
        self.human_delay(4.0, 7.0)

    @staticmethod
    def human_delay(min_sec: float = 2.0, max_sec: float = 5.0):
        time.sleep(random.uniform(min_sec, max_sec))

    def close(self):
        if self.context:
            self.context.close()
        if self.playwright:
            self.playwright.stop()
