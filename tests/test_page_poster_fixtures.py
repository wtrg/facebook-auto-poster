"""Faithful LOCAL fixture tests for Facebook Page Poster (Task MCB-FBP-002).

Demonstrates live workflow through Playwright route interception and real Chromium clicks/uploads.
ZERO external network requests leave the local environment.
All tests use real Chromium browser automation on isolated temporary user profiles.
"""

import os
import sys
import json
import pytest
from pathlib import Path
from PIL import Image

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PACKAGE_ROOT))

from core.contracts import TaskStatus, PostingResult, PostingTask, AuthCheckpointError
from core.session_manager import FacebookSessionManager
from core.fb_poster import FacebookPagePoster
from main import NightSchedulerWorker


def _generate_page_html(scenario: str, page_id: str, page_name: str, has_preexisting: bool = False) -> str:
    """Generate faithful Facebook DOM fixture with interactive client-side behavior."""
    preexisting_html = ""
    if has_preexisting:
        preexisting_html = (
            f'<div role="article" id="old_post_99999">'
            f'<p>Nội dung bài viết mẫu cũ trên dòng thời gian</p>'
            f'<a href="https://www.facebook.com/{page_id}/posts/99999">2 ngày trước</a>'
            f'</div>'
        )

    actor_name = "Nguyễn Văn A (Cá nhân)" if scenario == "wrong_actor" else page_name

    return f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>{page_name} | Facebook</title>
</head>
<body>
    <div role="navigation">
        <div aria-label="Trang chủ">Trang chủ</div>
        <div aria-label="Tài khoản">Tài khoản</div>
    </div>

    <div role="main">
        <h1>{page_name}</h1>
        <div aria-label="Đang tương tác dưới tên">{actor_name}</div>
    </div>

    <!-- Trigger button -->
    <div role="button" tabindex="0" id="trigger_btn" style="cursor:pointer; padding:10px; border:1px solid #ccc;">
        Bạn đang nghĩ gì?
    </div>

    <!-- Timeline feed -->
    <div role="feed" id="feed_container" style="margin-top:20px;">
        {preexisting_html}
    </div>

    <!-- Scoped Composer Dialog -->
    <div role="dialog" id="composer_dialog" aria-label="Tạo bài viết" style="display:none; border:2px solid #000; padding:20px; background:#fff;">
        <h2>Tạo bài viết</h2>
        <div role="textbox" contenteditable="true" aria-label="Tạo bài viết" id="composer_textbox" style="min-height:60px; border:1px solid #aaa; padding:5px;"></div>
        
        <div role="button" id="media_btn" aria-label="Ảnh/video" style="margin-top:10px; cursor:pointer;">Ảnh/video</div>
        <input type="file" id="file_input" style="display:none;" multiple accept="image/*" />
        
        <div id="media_preview" style="margin-top:5px;"></div>
        <div id="upload_error_box" style="display:none; color:red; margin-top:5px;">Không thể tải lên ảnh</div>
        
        <div role="button" id="post_btn" aria-label="Đăng" aria-disabled="true" style="margin-top:15px; padding:10px; background:#1877f2; color:#fff; cursor:pointer;">
            Đăng
        </div>
    </div>

    <script>
        window.__SCENARIO__ = "{scenario}";
        const trigger = document.getElementById('trigger_btn');
        const dialog = document.getElementById('composer_dialog');
        const textbox = document.getElementById('composer_textbox');
        const mediaBtn = document.getElementById('media_btn');
        const fileInput = document.getElementById('file_input');
        const preview = document.getElementById('media_preview');
        const errorBox = document.getElementById('upload_error_box');
        const postBtn = document.getElementById('post_btn');
        const feed = document.getElementById('feed_container');

        if (trigger) {{
            trigger.addEventListener('click', () => {{
                dialog.style.display = 'block';
            }});
        }}

        if (mediaBtn) {{
            mediaBtn.addEventListener('click', () => {{
                fileInput.click();
            }});
        }}

        if (fileInput) {{
            fileInput.addEventListener('change', () => {{
                if (window.__SCENARIO__ === 'upload_failed') {{
                    errorBox.style.display = 'block';
                    postBtn.setAttribute('aria-disabled', 'true');
                    return;
                }}
                preview.innerHTML = '<div aria-label="Ảnh preview">1 file ảnh đính kèm đã sẵn sàng</div>';
                if (window.__SCENARIO__ !== 'disabled_upload') {{
                    postBtn.setAttribute('aria-disabled', 'false');
                }}
            }});
        }}

        if (textbox) {{
            textbox.addEventListener('input', () => {{
                if (window.__SCENARIO__ !== 'disabled_upload' && window.__SCENARIO__ !== 'upload_failed') {{
                    postBtn.setAttribute('aria-disabled', 'false');
                }}
            }});
        }}

        if (postBtn) {{
            postBtn.addEventListener('click', () => {{
                if (postBtn.getAttribute('aria-disabled') === 'true') return;

                if (window.__SCENARIO__ === 'post_click_timeout') {{
                    // Simulates network drop or browser hang after click: dialog stays open
                    return;
                }}

                if (window.__SCENARIO__ === 'stale_preexisting') {{
                    // Dialog closes but NO new article is rendered (only old post remains)
                    dialog.style.display = 'none';
                    return;
                }}

                // Read content BEFORE hiding dialog (hidden elements return empty innerText in browser DOM)
                const content = textbox.innerText || textbox.textContent;
                dialog.style.display = 'none';
                const newArticle = document.createElement('div');
                newArticle.setAttribute('role', 'article');
                newArticle.setAttribute('id', 'new_post_100001');
                newArticle.innerHTML = '<p>' + content + '</p><a href="https://www.facebook.com/{page_id}/posts/100001">Vừa xong</a>';
                feed.prepend(newArticle);
            }});
        }}
    </script>
</body>
</html>
"""


def _setup_route_interception(session: FacebookSessionManager, scenario: str, page_id: str = "123456", page_name: str = "Page 123456", has_preexisting: bool = False):
    """Intercept all requests to https://www.facebook.com/** and serve faithful local fixtures."""
    def route_handler(route):
        url = route.request.url
        if url == "https://www.facebook.com/" or url == "https://www.facebook.com":
            if scenario == "auth_expired":
                body = (
                    "<!DOCTYPE html><html><body>"
                    "<h1>Đăng nhập Facebook</h1>"
                    "<form action='/login'><input name='email'><input name='pass'><button name='login'>Đăng nhập</button></form>"
                    "</body></html>"
                )
                route.fulfill(status=200, content_type="text/html; charset=utf-8", body=body)
            elif scenario == "checkpoint":
                body = (
                    "<!DOCTYPE html><html><head><title>Checkpoint</title></head><body>"
                    "<h1>Xác minh danh tính</h1>"
                    "<p>Tài khoản của bạn đã bị Checkpoint. Vui lòng xác minh để tiếp tục.</p>"
                    "</body></html>"
                )
                route.fulfill(status=200, content_type="text/html; charset=utf-8", body=body)
            else:
                body = (
                    "<!DOCTYPE html><html><body>"
                    "<div role='navigation'>"
                    "<div aria-label='Trang chủ'>Trang chủ</div>"
                    "<div aria-label='Tài khoản'>Tài khoản cá nhân</div>"
                    "</div>"
                    "<div role='feed'>"
                    "<div role='button'>Bạn đang nghĩ gì?</div>"
                    "</div>"
                    "</body></html>"
                )
                route.fulfill(status=200, content_type="text/html; charset=utf-8", body=body)
        elif page_id in url:
            html = _generate_page_html(scenario=scenario, page_id=page_id, page_name=page_name, has_preexisting=has_preexisting)
            route.fulfill(status=200, content_type="text/html; charset=utf-8", body=html)
        else:
            route.fulfill(status=200, content_type="text/html; charset=utf-8", body="<html><body>OK</body></html>")

    session.context.route("https://www.facebook.com/**", route_handler)


# -----------------------------------------------------------------------------
# 1. AUTH EXPIRED & CHECKPOINT TESTS
# -----------------------------------------------------------------------------
def test_fixture_auth_expired_halts_shift(tmp_path):
    """Auth expired returns login form -> verify_logged_in returns False -> shift halts."""
    session = FacebookSessionManager(profile_dir=str(tmp_path / "prof"), delay_multiplier=0)
    session.start_browser(headless=True)
    try:
        _setup_route_interception(session, scenario="auth_expired")
        poster = FacebookPagePoster(session)

        assert poster.verify_logged_in(timeout_ms=1000) is False

        # Verify runner halts with exit code 3
        config = tmp_path / "config.json"
        state = tmp_path / "state.json"
        config.write_text(json.dumps({
            "tasks": [{"task_id": "t1", "page_id": "123456", "content": "Hello", "media_paths": []}]
        }), encoding="utf-8")

        worker = NightSchedulerWorker(config_file=str(config), state_file=str(state))
        code = worker.run_night_shift(execute=True, poster_instance=poster, session_instance=session)
        assert code == 3, "Auth expired did not halt shift immediately with code 3"
        # Task must not have been attempted
        assert "t1" not in worker.state_store.tasks or worker.state_store.tasks["t1"].status == TaskStatus.PENDING
    finally:
        session.close()


def test_fixture_checkpoint_halts_shift(tmp_path):
    """Checkpoint detected -> verify_logged_in returns False -> shift halts."""
    session = FacebookSessionManager(profile_dir=str(tmp_path / "prof"), delay_multiplier=0)
    session.start_browser(headless=True)
    try:
        _setup_route_interception(session, scenario="checkpoint")
        poster = FacebookPagePoster(session)

        assert poster.verify_logged_in(timeout_ms=1000) is False

        config = tmp_path / "config.json"
        state = tmp_path / "state.json"
        config.write_text(json.dumps({
            "tasks": [{"task_id": "t2", "page_id": "123456", "content": "Hello", "media_paths": []}]
        }), encoding="utf-8")

        worker = NightSchedulerWorker(config_file=str(config), state_file=str(state))
        code = worker.run_night_shift(execute=True, poster_instance=poster, session_instance=session)
        assert code == 3, "Checkpoint did not halt shift with code 3"
    finally:
        session.close()


# -----------------------------------------------------------------------------
# 2. PRE-SEND ACTOR IDENTITY & MEDIA VALIDATION TESTS
# -----------------------------------------------------------------------------
def test_fixture_wrong_actor_fails_presend_without_clicking(tmp_path):
    """Active actor does not match expected Page -> pre-send failure ACTOR_UNVERIFIED -> no click attempted."""
    session = FacebookSessionManager(profile_dir=str(tmp_path / "prof"), delay_multiplier=0)
    session.start_browser(headless=True)
    try:
        _setup_route_interception(session, scenario="wrong_actor", page_id="123456", page_name="Page 123456")
        poster = FacebookPagePoster(session)

        result = poster.create_post_verified(
            page_id="123456",
            page_name="Page 123456",
            raw_content="Test content",
            media_paths=[]
        )
        assert result.success is False
        assert result.confirmed is False
        assert result.is_uncertain is False
        assert result.error_code == "ACTOR_UNVERIFIED"
        assert "mismatch" in result.error_message.lower() or "unverified" in result.error_message.lower()
    finally:
        session.close()


def test_fixture_missing_media_fails_presend_without_clicking(tmp_path):
    """Configured media file does not exist on disk -> pre-send failure MISSING_MEDIA -> no click attempted."""
    session = FacebookSessionManager(profile_dir=str(tmp_path / "prof"), delay_multiplier=0)
    session.start_browser(headless=True)
    try:
        _setup_route_interception(session, scenario="success", page_id="123456", page_name="Page 123456")
        poster = FacebookPagePoster(session)

        result = poster.create_post_verified(
            page_id="123456",
            page_name="Page 123456",
            raw_content="Post with missing media",
            media_paths=["C:/nonexistent_fixture_image_12345.png"]
        )
        assert result.success is False
        assert result.confirmed is False
        assert result.is_uncertain is False
        assert result.error_code == "MISSING_MEDIA"
    finally:
        session.close()


def test_fixture_disabled_upload_button_fails_presend(tmp_path):
    """Post button remains aria-disabled -> pre-send failure BUTTON_DISABLED -> no click attempted."""
    session = FacebookSessionManager(profile_dir=str(tmp_path / "prof"), delay_multiplier=0)
    session.start_browser(headless=True)
    try:
        _setup_route_interception(session, scenario="disabled_upload", page_id="123456", page_name="Page 123456")
        poster = FacebookPagePoster(session)

        # Create dummy image
        img_path = tmp_path / "upload_test.png"
        Image.new("RGB", (20, 20), "blue").save(img_path)

        result = poster.create_post_verified(
            page_id="123456",
            page_name="Page 123456",
            raw_content="Content with disabled button",
            media_paths=[str(img_path)],
            button_timeout_sec=1.5
        )
        assert result.success is False
        assert result.confirmed is False
        assert result.is_uncertain is False
        assert result.error_code == "BUTTON_DISABLED"
    finally:
        session.close()


def test_fixture_upload_failed_in_composer(tmp_path):
    """Upload error alert appears in dialog -> pre-send failure UPLOAD_FAILED -> no click attempted."""
    session = FacebookSessionManager(profile_dir=str(tmp_path / "prof"), delay_multiplier=0)
    session.start_browser(headless=True)
    try:
        _setup_route_interception(session, scenario="upload_failed", page_id="123456", page_name="Page 123456")
        poster = FacebookPagePoster(session)

        img_path = tmp_path / "bad_upload.png"
        Image.new("RGB", (20, 20), "green").save(img_path)

        result = poster.create_post_verified(
            page_id="123456",
            page_name="Page 123456",
            raw_content="Upload failure post",
            media_paths=[str(img_path)],
            button_timeout_sec=1.0
        )
        assert result.success is False
        assert result.confirmed is False
        assert result.is_uncertain is False
        assert result.error_code in ("UPLOAD_FAILED", "BUTTON_DISABLED")
    finally:
        session.close()


# -----------------------------------------------------------------------------
# 3. POST-CLICK UNCERTAINTY & STALE POST ISOLATION
# -----------------------------------------------------------------------------
def test_fixture_stale_preexisting_post_not_falsely_confirmed(tmp_path):
    """Pre-existing post with same page reference is ignored; missing new post becomes UNCERTAIN."""
    session = FacebookSessionManager(profile_dir=str(tmp_path / "prof"), delay_multiplier=0)
    session.start_browser(headless=True)
    try:
        _setup_route_interception(session, scenario="stale_preexisting", page_id="123456", page_name="Page 123456", has_preexisting=True)
        poster = FacebookPagePoster(session)

        result = poster.create_post_verified(
            page_id="123456",
            page_name="Page 123456",
            raw_content="Nội dung bài viết mới",
            media_paths=[],
            button_timeout_sec=2.0,
            dialog_dismiss_timeout_ms=2000,
            post_click_verify_timeout_ms=1500
        )
        # Click happened, but no new post was found -> UNCERTAIN, never confirmed!
        assert result.success is False
        assert result.confirmed is False
        assert result.is_uncertain is True
        assert result.error_code == "POST_CLICK_UNCERTAIN"
        assert result.post_id != "99999", "Must never confirm stale pre-existing post ID!"
    finally:
        session.close()


def test_fixture_post_click_timeout_becomes_unknown_needs_reconciliation(tmp_path):
    """Click occurs but dialog hangs or verification times out -> UNKNOWN_NEEDS_RECONCILIATION -> never replayed."""
    session = FacebookSessionManager(profile_dir=str(tmp_path / "prof"), delay_multiplier=0)
    session.start_browser(headless=True)
    try:
        _setup_route_interception(session, scenario="post_click_timeout", page_id="123456", page_name="Page 123456")
        poster = FacebookPagePoster(session)

        config = tmp_path / "config.json"
        state = tmp_path / "state.json"
        config.write_text(json.dumps({
            "tasks": [{"task_id": "timeout_task", "page_id": "123456", "content": "Timeout post", "media_paths": []}]
        }), encoding="utf-8")

        worker = NightSchedulerWorker(config_file=str(config), state_file=str(state))
        # Override poster call to use short timeout
        class FastTimeoutPoster(FacebookPagePoster):
            def create_post_verified(self, **kwargs):
                kwargs["dialog_dismiss_timeout_ms"] = 1000
                kwargs["post_click_verify_timeout_ms"] = 1000
                return super().create_post_verified(**kwargs)

        fast_poster = FastTimeoutPoster(session)
        worker.run_night_shift(execute=True, poster_instance=fast_poster, session_instance=session)

        task = worker.state_store.tasks["timeout_task"]
        assert task.status == TaskStatus.UNKNOWN_NEEDS_RECONCILIATION, "Post-click timeout must transition to UNKNOWN_NEEDS_RECONCILIATION"

        # Verify that subsequent execution will NEVER automatically replay this task
        worker2 = NightSchedulerWorker(config_file=str(config), state_file=str(state))
        worker2.load_and_validate()
        assert not worker2.state_store.get_pending_tasks(), "Interrupted post-click task must never be scheduled for auto-retry!"
    finally:
        session.close()


# -----------------------------------------------------------------------------
# 4. SUCCESSFUL VERIFIED POSTING WORKFLOW
# -----------------------------------------------------------------------------
def test_fixture_successful_posting_with_chromium_clicks_and_upload(tmp_path):
    """Full live Chromium workflow: auth check, actor check, upload file, real click, feed verified post ID."""
    session = FacebookSessionManager(profile_dir=str(tmp_path / "prof"), delay_multiplier=0)
    session.start_browser(headless=True)
    try:
        _setup_route_interception(session, scenario="success", page_id="123456", page_name="Page 123456")
        poster = FacebookPagePoster(session)

        # Pre-flight auth check
        assert poster.verify_logged_in() is True

        img_path = tmp_path / "real_upload.png"
        Image.new("RGB", (50, 50), "yellow").save(img_path)

        config = tmp_path / "config.json"
        state = tmp_path / "state.json"
        config.write_text(json.dumps({
            "tasks": [{
                "task_id": "success_task_1",
                "page_id": "123456",
                "page_name": "Page 123456",
                "content": "{Xin chào|Hello} các bạn trên Facebook!",
                "media_paths": [str(img_path)]
            }]
        }), encoding="utf-8")

        worker = NightSchedulerWorker(config_file=str(config), state_file=str(state))
        code = worker.run_night_shift(execute=True, poster_instance=poster, session_instance=session)

        assert code == 0, "Night shift with successful post returned non-zero code"
        task = worker.state_store.tasks["success_task_1"]
        assert task.status == TaskStatus.COMPLETED
        assert task.confirmed is True
        assert task.post_id == "100001"
        assert task.post_url == "https://www.facebook.com/123456/posts/100001"
    finally:
        session.close()


# -----------------------------------------------------------------------------
# 5. BROWSER STARTUP FAILURE & CLEANUP INTEGRITY TESTS
# -----------------------------------------------------------------------------
def test_browser_startup_failure_releases_resources(tmp_path, monkeypatch):
    """Startup failure cleanly closes context and playwright before raising."""
    session = FacebookSessionManager(profile_dir=str(tmp_path / "prof"))

    class FakeChromium:
        def launch_persistent_context(self, *a, **kw):
            raise OSError("SYNTHETIC_CHROMIUM_LAUNCH_FAILURE")

    class FakePlaywright:
        chromium = FakeChromium()
        def stop(self):
            pass

    monkeypatch.setattr("core.session_manager.sync_playwright", lambda: type("P", (), {"start": lambda self: FakePlaywright()})())

    with pytest.raises(RuntimeError) as exc:
        session.start_browser()
    assert "failed to start browser" in str(exc.value).lower()
    assert session.context is None
    assert session.page is None
    assert session.playwright is None


def test_cleanup_errors_preserve_persisted_intent(tmp_path):
    """Errors during session cleanup in finally block do not corrupt persisted state."""
    config = tmp_path / "config.json"
    state = tmp_path / "state.json"
    config.write_text(json.dumps({
        "tasks": [{"task_id": "cleanup_task", "page_id": "p1", "content": "Cleanup test", "media_paths": []}]
    }), encoding="utf-8")

    class CrashingCleanupSession:
        context = None
        def close(self):
            raise RuntimeError("SYNTHETIC_CLEANUP_CRASH")

    class SuccessfulMockPoster:
        def verify_logged_in(self): return True
        def create_post_verified(self, **kwargs):
            return PostingResult(success=True, confirmed=True, post_id="post_999", post_url="https://fb.com/p/999")

    worker = NightSchedulerWorker(config_file=str(config), state_file=str(state))
    # run_night_shift must still finish and preserve state even if session.close raises
    worker.run_night_shift(
        execute=True,
        poster_instance=SuccessfulMockPoster(),
        session_instance=CrashingCleanupSession()
    )

    task = worker.state_store.tasks["cleanup_task"]
    assert task.status == TaskStatus.COMPLETED
    assert task.post_id == "post_999"
    # State file on disk exists and contains completed task
    saved_state = json.loads(state.read_text(encoding="utf-8"))
    assert saved_state["tasks"][0]["status"] == "completed"
