import os
import time
import logging
from typing import List
from core.session_manager import FacebookSessionManager
from core.anti_spam import AntiSpamProcessor

class FacebookPagePoster:
    def __init__(self, session: FacebookSessionManager):
        self.session = session
        self.page = session.page

    def verify_logged_in(self) -> bool:
        self.page.goto("https://www.facebook.com/", wait_until="domcontentloaded")
        self.session.human_delay(3.0, 5.0)
        if self.page.locator("input[name='email']").count() > 0 or "checkpoint" in self.page.url:
            logging.error("Phiên đăng nhập đã hết hạn hoặc bị Checkpoint!")
            return False
        return True

    def create_post(self, page_id: str, raw_content: str, media_paths: List[str] = None) -> bool:
        try:
            self.session.switch_to_page_profile(page_id)
            if "checkpoint" in self.page.url or "login" in self.page.url:
                raise RuntimeError("Phát hiện chuyển hướng Checkpoint hoặc Login!")

            # Đóng popup nếu có
            self.page.keyboard.press("Escape")
            self.session.human_delay(1.0, 2.0)

            post_trigger = self.page.locator(
                "div[role='button']:has-text('Bạn đang nghĩ gì'), "
                "div[role='button']:has-text('What\'s on your mind'), "
                "div[role='button']:has-text('Viết gì đó')"
            ).first

            post_trigger.wait_for(state="visible", timeout=15000)
            post_trigger.click()
            self.session.human_delay(2.5, 4.5)

            dialog = self.page.locator("div[role='dialog']").first
            textbox = dialog.locator("div[role='textbox'][contenteditable='true']").first
            textbox.wait_for(state="visible", timeout=10000)
            textbox.click()

            # Trộn nội dung Spintax
            final_content = AntiSpamProcessor.spin_text(raw_content)
            logging.info("Đang nhập nội dung bài viết đã trộn Spintax...")
            textbox.fill(final_content)
            self.session.human_delay(2.0, 4.0)

            if media_paths:
                mutated_files = []
                for p in media_paths:
                    if os.path.exists(p):
                        if p.lower().endswith((".jpg", ".jpeg", ".png")):
                            mutated_files.append(AntiSpamProcessor.mutate_image(p))
                        else:
                            mutated_files.append(os.path.abspath(p))

                if mutated_files:
                    logging.info(f"Đang tải lên {len(mutated_files)} file media...")
                    file_input = dialog.locator("input[type='file']").first
                    if file_input.count() == 0:
                        media_btn = dialog.locator("div[aria-label*='Ảnh/video'], div[aria-label*='Photo/video']").first
                        if media_btn.count() > 0:
                            media_btn.click()
                            self.session.human_delay(1.5, 2.5)
                            file_input = dialog.locator("input[type='file']").first

                    file_input.set_input_files(mutated_files)
                    self.session.human_delay(6.0, 10.0)

            next_btn = dialog.locator("div[aria-label='Tiếp'], div[aria-label='Next']").first
            if next_btn.is_visible():
                next_btn.click()
                self.session.human_delay(2.0, 3.5)

            post_btn = dialog.locator("div[aria-label='Đăng'], div[aria-label='Post']").first
            post_btn.wait_for(state="visible", timeout=15000)

            # Chờ đến khi nút Đăng hết bị disable (khi upload video/ảnh nặng)
            for _ in range(30):
                if post_btn.get_attribute("aria-disabled") == "true":
                    time.sleep(2)
                else:
                    break

            post_btn.click()
            logging.info(f"Đã bấm Đăng cho Page {page_id}. Đang chờ hoàn tất...")
            self.session.human_delay(8.0, 12.0)
            return True

        except Exception as e:
            logging.error(f"Lỗi khi đăng bài lên Page {page_id}: {str(e)}")
            os.makedirs("error_screenshots", exist_ok=True)
            self.page.screenshot(path=f"error_screenshots/err_{page_id}_{int(time.time())}.png")
            return False
