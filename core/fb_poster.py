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
from urllib.parse import urlparse, parse_qs
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
            # Require account-specific authenticated signals (generic navigation or feeds do NOT count)
            authenticated_selectors = [
                "div[aria-label*='Tài khoản'], div[aria-label*='Account']",
                "div[aria-label*='Trang cá nhân của bạn'], div[aria-label*='Your profile']",
                "a[href*='/me'], a[href*='/profile.php']",
                "div[role='navigation'] [aria-label*='Account'], div[role='navigation'] [aria-label*='Tài khoản']",
                "div[role='button']:has-text('Bạn đang nghĩ gì'), div[role='button']:has-text(\"What's on your mind\")"
            ]

            found_authenticated = False
            for sel in authenticated_selectors:
                locs = page.locator(sel)
                try:
                    for i in range(locs.count()):
                        if locs.nth(i).is_visible():
                            found_authenticated = True
                            break
                except Exception:
                    pass
                if found_authenticated:
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

    @staticmethod
    def _extract_profile_id_from_url(url: str) -> Optional[str]:
        """Extract user or page ID from Facebook profile URL."""
        if not url:
            return None
        parsed = urlparse(url)
        qs = parse_qs(parsed.query)
        if "id" in qs and qs["id"]:
            return qs["id"][0].strip()
        path_parts = [p for p in parsed.path.split('/') if p and p.lower() not in ("pages", "profile.php")]
        if path_parts:
            return path_parts[-1].strip()
        return None

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

        if not page_id and not page_name:
            return False, "No page_id or page_name supplied to verify actor."

        try:
            # Check persona / interacting-as badges ONLY (never headings, synthetic test hooks, or switch buttons)
            badge_locators = page.locator(
                "div[aria-label*='Đang tương tác dưới tên'], "
                "div[aria-label*='Interacting as'], "
                "div[aria-label*='Acting as']"
            ).all()

            if not badge_locators:
                return False, f"Actor unverified: No positive active persona badge found for '{page_name or page_id}'."

            # Filter for visible badges and exclude switch offers
            active_badges = []
            for b in badge_locators:
                try:
                    if not b.is_visible():
                        continue
                    if b.get_attribute("hidden") is not None:
                        continue
                    aria = (b.get_attribute("aria-label") or "").strip()
                    lower_aria = aria.lower()
                    if "switch to" in lower_aria or "chuyển sang" in lower_aria:
                        continue
                    active_badges.append(b)
                except Exception:
                    continue

            if not active_badges:
                return False, f"Actor unverified: No visible active persona badge found for '{page_name or page_id}'."

            # Inspect active badges
            combined_desc = ""
            for b in active_badges:
                aria = (b.get_attribute("aria-label") or "").strip()
                text = (b.inner_text() or "").strip()
                combined_desc = f"{text} {aria}".strip()

                # Check if badge has an <a> link with profile href
                links = b.locator("a[href]").all()
                profile_link_target = None
                for link in links:
                    href = link.get_attribute("href")
                    if href:
                        extracted = self._extract_profile_id_from_url(href)
                        if extracted:
                            profile_link_target = extracted
                            break

                # If badge has a profile link pointing elsewhere, display name cannot override it!
                if profile_link_target:
                    if page_id and profile_link_target.lower() != page_id.lower():
                        return False, (
                            f"Actor mismatch: Badge profile link points to '{profile_link_target}', "
                            f"which does not match target page_id '{page_id}'."
                        )

                # Check page_id with discrete token/word boundary
                matched = False
                matched_token = None
                if page_id:
                    pattern = rf"\b{re.escape(page_id)}\b"
                    if re.search(pattern, combined_desc):
                        matched = True
                        matched_token = page_id

                # If not matched by page_id, check page_name
                if not matched and page_name:
                    if page_name.lower() in combined_desc.lower():
                        digits_in_badge = re.findall(r'\b\d+\b', combined_desc)
                        if page_id and digits_in_badge and page_id not in digits_in_badge:
                            return False, (
                                f"Actor mismatch: Badge contains conflicting ID {digits_in_badge} "
                                f"instead of '{page_id}'."
                            )
                        matched = True
                        matched_token = page_name

                if matched:
                    return True, f"Positive actor badge verified: '{combined_desc}' matches '{matched_token}'"

            return False, f"Actor unverified: Active persona badge '{combined_desc}' does not match '{page_name or page_id}'."

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
        timeout_ms: int = 15000,
        page_id: Optional[str] = None
    ) -> Optional[Dict[str, str]]:
        """
        Locate newly published post in feed that matches content, actor, and has
        an actual post reference not present in preexisting_refs.
        """
        page = self.page
        deadline = time.time() + (timeout_ms / 1000.0)

        # Canonicalize pre-existing references to IDs and full clean URLs
        preexisting_clean = {r.strip() for r in preexisting_refs if r}
        preexisting_ids = {self._extract_post_id_from_url(r) for r in preexisting_clean if self._extract_post_id_from_url(r)}

        # Normalize content for matching
        norm_content = re.sub(r'\s+', ' ', content).strip()

        valid_domains = {"facebook.com", "www.facebook.com", "m.facebook.com", "web.facebook.com", "mbasic.facebook.com"}

        allowed_identities = set()
        if page_name:
            allowed_identities.add(page_name.lower().strip())
        if page_id:
            allowed_identities.add(page_id.lower().strip())

        while time.time() < deadline:
            try:
                articles = page.locator("div[role='article'], div[data-testid*='post'], div[role='feed'] > div").all()
                for art in articles:
                    try:
                        art_text = art.inner_text() or ""
                        norm_art_text = re.sub(r'\s+', ' ', art_text).strip()

                        # 1. Content Verification: complete content match
                        if norm_content and norm_content not in norm_art_text:
                            continue

                        # 2. Freshness Check: reject old/stale posts
                        time_locs = art.locator("time").all()
                        is_stale = False
                        for t in time_locs:
                            try:
                                dt_attr = t.get_attribute("datetime")
                                if dt_attr:
                                    dt_clean = dt_attr.replace("Z", "+00:00")
                                    post_time = datetime.fromisoformat(dt_clean)
                                    if post_time.tzinfo is None:
                                        post_time = post_time.replace(tzinfo=timezone.utc)
                                    now_utc = datetime.now(timezone.utc)
                                    age_seconds = (now_utc - post_time).total_seconds()
                                    if age_seconds > 600 or age_seconds < -300:
                                        is_stale = True
                                        break
                                time_text = (t.inner_text() or "").strip().lower()
                                old_indicators = [
                                    "yesterday", "hôm qua", "ngày trước", "days ago", "months ago",
                                    "years ago", "năm trước", "tháng trước", "january", "february",
                                    "march", "april", "may", "june", "july", "august", "september",
                                    "october", "november", "december"
                                ]
                                if any(ind in time_text for ind in old_indicators):
                                    is_stale = True
                                    break
                            except Exception:
                                pass

                        if is_stale:
                            continue

                        # 3. Extract post links within this article
                        post_links = art.locator("a[href*='/posts/'], a[href*='/permalink/'], a[href*='story_fbid='], a[href*='fbid=']").all()
                        if not post_links:
                            continue

                        for pl in post_links:
                            href = pl.get_attribute("href")
                            if not href:
                                continue
                            href_clean = href.strip()

                            # 4. Domain Check: reject external URLs
                            parsed_url = urlparse(href_clean)
                            if parsed_url.netloc:
                                domain = parsed_url.netloc.lower().split(':')[0]
                                if domain not in valid_domains:
                                    continue

                            # 5. Extract post_id from URL
                            post_id = self._extract_post_id_from_url(href_clean)
                            if not post_id:
                                continue

                            # 6. Canonical Deduplication Check
                            if post_id in preexisting_ids or href_clean in preexisting_clean:
                                continue

                            # 7. Actor / Author Verification
                            # If post link has a page/user slug or ID in path, check it matches allowed identities
                            m_slug = re.search(r'facebook\.com/([^/?#]+)/posts/', href_clean)
                            if m_slug:
                                post_owner = m_slug.group(1).strip()
                                if post_owner.lower() != "groups":
                                    if allowed_identities and post_owner.lower() not in allowed_identities:
                                        continue

                            # Also check author links in article (links that are not post links)
                            author_links = art.locator("a:not([href*='/posts/']):not([href*='/permalink/']):not([href*='story_fbid']):not([href*='fbid='])").all()
                            if author_links:
                                author_match_found = False
                                for alink in author_links:
                                    ahref = alink.get_attribute("href") or ""
                                    atext = (alink.inner_text() or "").strip()
                                    target_id = self._extract_profile_id_from_url(ahref)
                                    if target_id and allowed_identities and target_id.lower() in allowed_identities:
                                        author_match_found = True
                                        break
                                    if any(ident in atext.lower() for ident in allowed_identities):
                                        author_match_found = True
                                        break
                                if not author_match_found:
                                    continue

                            # Verified matching post found!
                            return {
                                "post_id": post_id,
                                "post_url": href_clean
                            }
                    except Exception:
                        continue
            except Exception:
                pass

            time.sleep(0.5)

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
                timeout_ms=post_click_verify_timeout_ms,
                page_id=page_id
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
