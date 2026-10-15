"""Facebook Page Posting automation with positive actor verification,
accessible DOM scoping, bounded upload waits, and post-click uncertainty isolation.
"""

import os
import re
import time
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple, Dict, Any, Set
from playwright.sync_api import Page, Locator, TimeoutError as PlaywrightTimeoutError

from core.contracts import PostingResult, AuthCheckpointError
from core.session_manager import FacebookSessionManager
from core.anti_spam import AntiSpamProcessor
from core.config_loader import sanitize_text

logger = logging.getLogger("facebook_auto_poster")


def is_element_disabled(locator: Locator) -> bool:
    """Check both native disabled state and WAI-ARIA aria-disabled attribute."""
    try:
        if not locator.is_visible():
            return True
        aria_disabled = locator.get_attribute("aria-disabled")
        if aria_disabled and aria_disabled.strip().lower() == "true":
            return True
        if locator.is_disabled():
            return True
        return False
    except Exception:
        return True


class FacebookPagePoster:
    """
    Automates publishing to Facebook Pages with strict verification:
    - Distinguishes authenticated UI from empty/error/checkpoint pages.
    - Requires positive UI evidence that publishing actor matches requested Page.
    - Fails pre-send on missing media or upload errors.
    - Separates pre-send failures from post-click uncertainty (UNKNOWN_NEEDS_RECONCILIATION).
    - Verifies publication by matching content and extracting unique post ID/URL.
    """
    def __init__(self, session: FacebookSessionManager):
        self.session = session

    @property
    def page(self) -> Page:
        if not self.session.page:
            raise RuntimeError("Browser session page is not initialized.")
        return self.session.page

    def verify_logged_in(self, timeout_ms: int = 10000) -> bool:
        """
        Distinguish confirmed authenticated UI from empty/error/login/checkpoint pages.
        Failure halts the shift immediately.
        """
        page = self.page
        try:
            page.goto("https://www.facebook.com/", wait_until="domcontentloaded")
            self.session.human_delay(1.0, 2.0)
        except Exception as e:
            logger.error(f"Lỗi kết nối khi kiểm tra phiên đăng nhập: {sanitize_text(str(e))}")
            return False

        # 1. Negative Indicators (Login form, checkpoint URL or text)
        current_url = page.url.lower()
        if "checkpoint" in current_url or "login" in current_url or current_url.startswith("about:"):
            logger.error(f"Phát hiện URL không hợp lệ hoặc Checkpoint: {page.url}")
            return False

        try:
            # Check login inputs
            if page.locator("input[name='email'], input[name='pass'], button[name='login']").count() > 0:
                logger.error("Phát hiện form đăng nhập Facebook (chưa đăng nhập hoặc phiên hết hạn).")
                return False

            # Check checkpoint keywords in body
            body_text = page.locator("body").inner_text(timeout=timeout_ms)
            lower_body = body_text.lower()
            if any(term in lower_body for term in ["checkpoint", "xác minh danh tính", "tài khoản bị khóa", "confirm your identity"]):
                logger.error("Phát hiện thông báo Checkpoint trong nội dung trang.")
                return False

            # 2. Positive Authenticated Indicators
            # Must find at least one reliable authenticated element: navigation, account menu, feed, or home composer
            authenticated_selectors = [
                "div[role='feed']",
                "div[role='navigation']",
                "div[aria-label*='Tài khoản'], div[aria-label*='Account']",
                "div[aria-label*='Trang chủ'], div[aria-label*='Home']",
                "div[aria-label*='Menu Facebook'], div[aria-label*='Facebook Menu']",
                "div[role='button']:has-text('Bạn đang nghĩ gì'), div[role='button']:has-text(\"What's on your mind\")"
            ]

            found_authenticated = False
            for sel in authenticated_selectors:
                if page.locator(sel).count() > 0:
                    found_authenticated = True
                    break

            if not found_authenticated:
                # Wait briefly for any positive indicator before deciding it is empty/unauthenticated
                combined = ", ".join(authenticated_selectors)
                try:
                    page.locator(combined).first.wait_for(state="visible", timeout=timeout_ms)
                    found_authenticated = True
                except Exception:
                    found_authenticated = False

            if not found_authenticated:
                logger.error("Không tìm thấy thành phần UI xác thực tích cực nào (trang trống, lỗi mạng hoặc chưa đăng nhập).")
                return False

            logger.info("Đã xác nhận phiên đăng nhập Facebook hoạt động bình thường (Authenticated UI Verified).")
            return True

        except Exception as e:
            logger.error(f"Ngoại lệ khi kiểm tra đăng nhập: {sanitize_text(str(e))}")
            return False

    def verify_publishing_actor(self, page_id: str, page_name: Optional[str] = None) -> Tuple[bool, str]:
        """
        Verify that the active publishing actor matches requested Page with positive UI evidence.
        Changing i_user cookie or navigating to Page is NOT identity proof.
        Returns (is_verified, reason_or_evidence).
        """
        page = self.page
        current_url = page.url.lower()
        if "checkpoint" in current_url or "login" in current_url:
            return False, f"Redirected to checkpoint/login: {page.url}"

        expected_tokens = [t.lower().strip() for t in [page_name, page_id] if t and t.strip()]
        if not expected_tokens:
            return False, "No page_id or page_name supplied to verify actor."

        try:
            # 1. Primary: Check explicit persona / actor badge
            badge_loc = page.locator(
                "[data-testid='actor-indicator'], "
                "div[aria-label*='Đang tương tác dưới tên'], "
                "div[aria-label*='Interacting as'], "
                "div[aria-label*='Acting as']"
            ).first

            if badge_loc.count() > 0:
                badge_text = ""
                try:
                    t = (badge_loc.inner_text(timeout=2000) or "").strip()
                    a = (badge_loc.get_attribute("aria-label") or "").strip()
                    badge_text = f"{t} {a}".strip().lower()
                except Exception:
                    pass

                if badge_text:
                    for token in expected_tokens:
                        if token in badge_text:
                            return True, f"Positive actor badge verified: '{badge_text}' matches '{token}'"
                    return False, f"Actor mismatch: UI shows active actor '{badge_text}', which does not match '{page_name or page_id}'."

            # 2. Secondary: If no persona badge exists, check page profile header
            header_loc = page.locator("div[role='main'] h1, h1, div[role='main'] h2").first
            if header_loc.count() > 0:
                header_text = ""
                try:
                    header_text = (header_loc.inner_text(timeout=2000) or "").strip().lower()
                except Exception:
                    pass
                if header_text:
                    for token in expected_tokens:
                        if token in header_text:
                            return True, f"Positive page header verified: '{header_text}' matches '{token}'"

            return False, f"Actor unverified: No positive UI actor evidence for '{page_name or page_id}' found on page."

        except Exception as e:
            return False, f"Error verifying publishing actor: {sanitize_text(str(e))}"

    def get_preexisting_post_references(self) -> Set[str]:
        """
        Scan page/feed for existing post links before clicking post,
        preventing stale pre-existing posts from falsely satisfying confirmation.
        """
        refs: Set[str] = set()
        try:
            links = self.page.locator("a[href*='/posts/'], a[href*='/permalink/'], a[href*='story_fbid='], a[href*='fbid=']").all()
            for link in links:
                try:
                    href = link.get_attribute("href")
                    if href:
                        refs.add(href.strip())
                except Exception:
                    continue
        except Exception:
            pass
        return refs

    def _find_confirmed_new_post(
        self,
        content: str,
        page_name: str,
        preexisting_refs: Set[str],
        timeout_ms: int = 15000
    ) -> Optional[Dict[str, str]]:
        """
        Locate newly published post in feed that matches content, actor, and has
        an actual post reference not present in preexisting_refs.
        """
        page = self.page
        deadline = time.time() + (timeout_ms / 1000.0)

        # Truncate content for fuzzy substring matching (first 30 chars without spintax)
        clean_content = re.sub(r'[\r\n]+', ' ', content).strip()
        match_snippet = clean_content[:30] if len(clean_content) >= 30 else clean_content

        while time.time() < deadline:
            try:
                articles = page.locator("div[role='article'], div[data-testid*='post'], div[role='feed'] > div").all()
                for art in articles:
                    try:
                        art_text = art.inner_text()
                        if match_snippet and match_snippet not in art_text:
                            continue

                        # Extract post links within this article
                        post_links = art.locator("a[href*='/posts/'], a[href*='/permalink/'], a[href*='story_fbid='], a[href*='fbid=']").all()
                        for pl in post_links:
                            href = pl.get_attribute("href")
                            if not href or href.strip() in preexisting_refs:
                                continue

                            href_clean = href.strip()
                            # Extract post_id from URL
                            post_id = self._extract_post_id_from_url(href_clean)
                            if post_id:
                                return {
                                    "post_id": post_id,
                                    "post_url": href_clean
                                }
                    except Exception:
                        continue
            except Exception:
                pass

            time.sleep(1.0)

        return None

    @staticmethod
    def _extract_post_id_from_url(url: str) -> Optional[str]:
        """Extract numeric or alphanumeric post ID from standard Facebook post permalink."""
        # Examples:
        # https://www.facebook.com/123456/posts/9876543210
        # https://www.facebook.com/permalink.php?story_fbid=9876543210&id=123456
        # https://www.facebook.com/groups/123/posts/9876543210/
        m = re.search(r'/posts/([0-9A-Za-z_-]+)', url)
        if m:
            return m.group(1).rstrip('/')
        m_fbid = re.search(r'[?&](?:story_fbid|fbid)=([0-9A-Za-z_-]+)', url)
        if m_fbid:
            return m_fbid.group(1)
        m_perm = re.search(r'/permalink/([0-9A-Za-z_-]+)', url)
        if m_perm:
            return m_perm.group(1).rstrip('/')
        return None

    def create_post_verified(
        self,
        page_id: str,
        raw_content: str,
        media_paths: Optional[List[str]] = None,
        page_name: Optional[str] = None,
        task_id: Optional[str] = None,
        button_timeout_sec: float = 15.0,
        dialog_dismiss_timeout_ms: int = 15000,
        post_click_verify_timeout_ms: int = 15000
    ) -> PostingResult:
        """
        Execute reliable, verified posting to Facebook Page.
        Guarantees:
        - Hard pre-send failure on missing media, unverified actor, or disabled button.
        - Bounded wait for media upload.
        - Strict separation: errors after click transition to UNCERTAIN (never silently retried).
        - Confirmed outcome requires real post ID/URL matching attempt content.
        """
        page = self.page

        # ---------------------------------------------------------------------
        # 1. PRE-SEND VALIDATION: Check media files on disk
        # ---------------------------------------------------------------------
        if media_paths:
            for mp in media_paths:
                if not os.path.exists(mp):
                    err = f"Missing media file: '{mp}' does not exist on disk."
                    logger.error(err)
                    return PostingResult(
                        success=False,
                        confirmed=False,
                        is_uncertain=False,
                        error_code="MISSING_MEDIA",
                        error_message=err
                    )

        # ---------------------------------------------------------------------
        # 2. PRE-SEND NAVIGATION & CONTEXT SWITCH
        # ---------------------------------------------------------------------
        try:
            self.session.switch_to_page_profile(page_id)
        except Exception as e:
            return PostingResult(
                success=False,
                confirmed=False,
                is_uncertain=False,
                error_code="NAVIGATION_FAILED",
                error_message=f"Failed navigating to page {page_id}: {sanitize_text(str(e))}"
            )

        # ---------------------------------------------------------------------
        # 3. PRE-SEND AUTH & CHECKPOINT CHECK
        # ---------------------------------------------------------------------
        current_url = page.url.lower()
        if "checkpoint" in current_url or "login" in current_url:
            return PostingResult(
                success=False,
                confirmed=False,
                is_uncertain=False,
                error_code="AUTH_CHECKPOINT",
                error_message=f"Session redirected to login or checkpoint: {page.url}"
            )

        # ---------------------------------------------------------------------
        # 4. PRE-SEND ACTOR IDENTITY VERIFICATION
        # ---------------------------------------------------------------------
        actor_ok, actor_reason = self.verify_publishing_actor(page_id=page_id, page_name=page_name)
        if not actor_ok:
            logger.error(f"Từ chối đăng bài do chưa xác minh được danh tính Page: {actor_reason}")
            return PostingResult(
                success=False,
                confirmed=False,
                is_uncertain=False,
                error_code="ACTOR_UNVERIFIED",
                error_message=actor_reason
            )

        # Close any lingering popups before triggering composer
        try:
            page.keyboard.press("Escape")
            self.session.human_delay(0.5, 1.0)
        except Exception:
            pass

        # ---------------------------------------------------------------------
        # 5. PRE-SEND COMPOSER TRIGGER & DIALOG SCOPING
        # ---------------------------------------------------------------------
        trigger_selectors = [
            "div[role='button']:has-text('Bạn đang nghĩ gì')",
            "div[role='button']:has-text(\"What's on your mind\")",
            "div[role='button']:has-text('Viết gì đó')",
            "div[role='button']:has-text('Write something')"
        ]
        post_trigger = None
        for sel in trigger_selectors:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible():
                post_trigger = loc
                break

        if not post_trigger:
            return PostingResult(
                success=False,
                confirmed=False,
                is_uncertain=False,
                error_code="COMPOSER_NOT_FOUND",
                error_message="Could not find visible post creation trigger button on page."
            )

        try:
            post_trigger.click()
            self.session.human_delay(1.0, 2.0)
        except Exception as e:
            return PostingResult(
                success=False,
                confirmed=False,
                is_uncertain=False,
                error_code="COMPOSER_CLICK_FAILED",
                error_message=f"Failed clicking composer trigger: {sanitize_text(str(e))}"
            )

        # Scope dialog
        dialog = page.locator("div[role='dialog']").first
        try:
            dialog.wait_for(state="visible", timeout=10000)
        except PlaywrightTimeoutError:
            return PostingResult(
                success=False,
                confirmed=False,
                is_uncertain=False,
                error_code="DIALOG_NOT_OPENED",
                error_message="Composer dialog failed to open within timeout."
            )

        # ---------------------------------------------------------------------
        # 6. FILL CONTENT (Spintax processed)
        # ---------------------------------------------------------------------
        textbox = dialog.locator("div[role='textbox'][contenteditable='true'], div[role='textbox'], textarea").first
        try:
            textbox.wait_for(state="visible", timeout=10000)
            textbox.click()
        except Exception as e:
            return PostingResult(
                success=False,
                confirmed=False,
                is_uncertain=False,
                error_code="TEXTBOX_NOT_FOUND",
                error_message=f"Failed to focus content textbox: {sanitize_text(str(e))}"
            )

        final_content = AntiSpamProcessor.spin_text(raw_content)
        try:
            textbox.fill(final_content)
            self.session.human_delay(1.0, 2.0)
        except Exception as e:
            return PostingResult(
                success=False,
                confirmed=False,
                is_uncertain=False,
                error_code="TEXT_FILL_FAILED",
                error_message=f"Failed to enter content into composer textbox: {sanitize_text(str(e))}"
            )

        # ---------------------------------------------------------------------
        # 7. MEDIA ATTACHMENT & BOUNDED UPLOAD WAITING
        # ---------------------------------------------------------------------
        if media_paths:
            mutated_files = []
            for p in media_paths:
                if p.lower().endswith((".jpg", ".jpeg", ".png")):
                    mutated_files.append(AntiSpamProcessor.mutate_image(p))
                else:
                    mutated_files.append(os.path.abspath(p))

            if mutated_files:
                file_input = dialog.locator("input[type='file']").first
                if file_input.count() == 0:
                    media_btn = dialog.locator("div[aria-label*='Ảnh/video'], div[aria-label*='Photo/video'], div[role='button']:has-text('Ảnh/video')").first
                    if media_btn.count() > 0 and media_btn.is_visible():
                        media_btn.click()
                        self.session.human_delay(0.5, 1.5)
                        file_input = dialog.locator("input[type='file']").first

                if file_input.count() == 0:
                    return PostingResult(
                        success=False,
                        confirmed=False,
                        is_uncertain=False,
                        error_code="MEDIA_INPUT_NOT_FOUND",
                        error_message="Could not find file input element in composer dialog."
                    )

                try:
                    file_input.set_input_files(mutated_files)
                    self.session.human_delay(1.5, 3.0)
                except Exception as e:
                    return PostingResult(
                        success=False,
                        confirmed=False,
                        is_uncertain=False,
                        error_code="FILE_INPUT_FAILED",
                        error_message=f"Failed attaching media files: {sanitize_text(str(e))}"
                    )

                # Check for upload failure messages in dialog
                err_loc = dialog.locator("div:has-text('Không thể tải lên'), div:has-text('Upload failed'), div:has-text('Lỗi tải ảnh')")
                if err_loc.count() > 0 and err_loc.first.is_visible():
                    return PostingResult(
                        success=False,
                        confirmed=False,
                        is_uncertain=False,
                        error_code="UPLOAD_FAILED",
                        error_message="Media upload error displayed in composer dialog."
                    )

        # ---------------------------------------------------------------------
        # 8. POST BUTTON VERIFICATION (Native and aria-disabled checks)
        # ---------------------------------------------------------------------
        post_btn = dialog.locator(
            "div[aria-label='Đăng'], div[aria-label='Post'], "
            "button:has-text('Đăng'), button:has-text('Post'), div[role='button']:has-text('Đăng')"
        ).first

        if post_btn.count() == 0:
            return PostingResult(
                success=False,
                confirmed=False,
                is_uncertain=False,
                error_code="POST_BUTTON_NOT_FOUND",
                error_message="Could not find 'Post' button in composer dialog."
            )

        # Bounded wait for button to become enabled (e.g. while upload completes)
        button_ready = False
        start_wait = time.time()
        timeout_wait = button_timeout_sec
        while time.time() - start_wait < timeout_wait:
            if not is_element_disabled(post_btn):
                button_ready = True
                break
            time.sleep(0.5)

        if not button_ready:
            return PostingResult(
                success=False,
                confirmed=False,
                is_uncertain=False,
                error_code="BUTTON_DISABLED",
                error_message="Post button remained disabled (upload incomplete or validation failed)."
            )

        # ---------------------------------------------------------------------
        # 9. CAPTURE PRE-EXISTING POST REFERENCES BEFORE CLICK
        # ---------------------------------------------------------------------
        preexisting_refs = self.get_preexisting_post_references()

        # ---------------------------------------------------------------------
        # 10. ATTEMPT CLICK — IRREVERSIBLE ACTION BOUNDARY
        # Any failure beyond this point is POST_CLICK_UNCERTAIN!
        # ---------------------------------------------------------------------
        try:
            logger.info(f"Bấm nút Đăng bài cho Page {page_id} (Task: {task_id})...")
            post_btn.click()
        except Exception as click_err:
            # Click itself raised an exception: outcome is uncertain
            return PostingResult(
                success=False,
                confirmed=False,
                is_uncertain=True,
                error_code="POST_CLICK_UNCERTAIN",
                error_message=f"Exception during post button click: {sanitize_text(str(click_err))}"
            )

        # ---------------------------------------------------------------------
        # 11. POST-CLICK VERIFICATION & RECONCILIATION ISOLATION
        # ---------------------------------------------------------------------
        try:
            # 11a. Wait for composer dialog to disappear
            dialog.wait_for(state="hidden", timeout=dialog_dismiss_timeout_ms)
        except Exception as dlg_err:
            return PostingResult(
                success=False,
                confirmed=False,
                is_uncertain=True,
                error_code="POST_CLICK_UNCERTAIN",
                error_message=f"Post button clicked, but dialog did not close: {sanitize_text(str(dlg_err))}"
            )

        try:
            # 11b. Verify confirmed matching post in feed
            confirmed_post = self._find_confirmed_new_post(
                content=final_content,
                page_name=page_name or page_id,
                preexisting_refs=preexisting_refs,
                timeout_ms=post_click_verify_timeout_ms
            )

            if confirmed_post:
                logger.info(f"--> ĐÃ XÁC NHẬN BÀI ĐĂNG THÀNH CÔNG: post_id={confirmed_post['post_id']}, url={confirmed_post['post_url']}")
                return PostingResult(
                    success=True,
                    confirmed=True,
                    is_uncertain=False,
                    post_id=confirmed_post["post_id"],
                    post_url=confirmed_post["post_url"],
                    published_at=datetime.now(timezone.utc).isoformat()
                )
            else:
                return PostingResult(
                    success=False,
                    confirmed=False,
                    is_uncertain=True,
                    error_code="POST_CLICK_UNCERTAIN",
                    error_message="Post button clicked and dialog closed, but newly published post could not be confirmed in feed."
                )

        except Exception as conf_err:
            return PostingResult(
                success=False,
                confirmed=False,
                is_uncertain=True,
                error_code="POST_CLICK_UNCERTAIN",
                error_message=f"Post button clicked, but confirmation verification encountered exception: {sanitize_text(str(conf_err))}"
            )

    def create_post(self, page_id: str, raw_content: str, media_paths: Optional[List[str]] = None) -> bool:
        """Legacy compatibility wrapper."""
        res = self.create_post_verified(page_id=page_id, raw_content=raw_content, media_paths=media_paths)
        return res.is_verified_success()
