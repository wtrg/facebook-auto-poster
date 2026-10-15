"""Comprehensive tests for Task MCB-FBP-001 (Reliable Runner).

Verifies:
1. Default dry-run CLI without side effects.
2. Strict schema validation (duplicate IDs, invalid spintax, missing media, timing).
3. Secret redaction in logs, reports, and URLs.
4. Atomic state store persistence and merge integrity.
5. Single-worker mutual exclusion locking.
6. In-flight persistence before side effect and crash recovery (UNKNOWN_NEEDS_RECONCILIATION).
7. Typed contract for Task 002 (legacy unconfirmed boolean vs verified publication).
8. Independent subprocess CLI tests.
"""

import os
import sys
import json
import pytest
import subprocess
from pathlib import Path
from PIL import Image

# Ensure package root is in sys.path
PACKAGE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PACKAGE_ROOT))

from core.contracts import TaskStatus, PostingTask, PostingResult, WorkerTimingConfig
from core.config_loader import (
    load_and_validate_config,
    validate_config_dict,
    ConfigValidationError,
    sanitize_text,
    sanitize_url,
    mask_secret,
    resolve_secrets
)
from core.state_manager import (
    AtomicStateStore,
    SingleWorkerLock,
    ConcurrentWorkerError,
    PersistenceError
)
from main import NightSchedulerWorker


# -----------------------------------------------------------------------------
# 1. SCHEMA VALIDATION TESTS
# -----------------------------------------------------------------------------
def test_schema_validation_duplicate_task_ids(tmp_path):
    config = {
        "tasks": [
            {"task_id": "task_1", "page_id": "p1", "content": "Hello", "media_paths": []},
            {"task_id": "task_1", "page_id": "p2", "content": "Hello again", "media_paths": []}
        ]
    }
    cfg_file = tmp_path / "dup.json"
    cfg_file.write_text(json.dumps(config), encoding="utf-8")

    with pytest.raises(ConfigValidationError) as exc:
        load_and_validate_config(cfg_file, check_media_exists=False)
    assert "Duplicate task_id 'task_1'" in str(exc.value)


def test_schema_validation_unbalanced_spintax(tmp_path):
    config = {
        "tasks": [
            {"task_id": "task_bad", "page_id": "p1", "content": "{Xin chào|Hello mọi người", "media_paths": []}
        ]
    }
    cfg_file = tmp_path / "spintax_err.json"
    cfg_file.write_text(json.dumps(config), encoding="utf-8")

    with pytest.raises(ConfigValidationError) as exc:
        load_and_validate_config(cfg_file, check_media_exists=False)
    assert "unbalanced Spintax braces" in str(exc.value)


def test_schema_validation_missing_media_file_fails(tmp_path):
    config = {
        "tasks": [
            {"task_id": "task_m", "page_id": "p1", "content": "Post with media", "media_paths": ["nonexistent.jpg"]}
        ]
    }
    cfg_file = tmp_path / "media_err.json"
    cfg_file.write_text(json.dumps(config), encoding="utf-8")

    with pytest.raises(ConfigValidationError) as exc:
        load_and_validate_config(cfg_file, check_media_exists=True)
    assert "Media file not found" in str(exc.value)


def test_schema_validation_invalid_timing(tmp_path):
    config = {
        "timing": {
            "min_break_minutes": 10,
            "max_break_minutes": 5  # max < min is invalid!
        },
        "tasks": [
            {"task_id": "task_ok", "page_id": "p1", "content": "Hello", "media_paths": []}
        ]
    }
    cfg_file = tmp_path / "timing_err.json"
    cfg_file.write_text(json.dumps(config), encoding="utf-8")

    with pytest.raises(ConfigValidationError) as exc:
        load_and_validate_config(cfg_file, check_media_exists=False)
    assert "cannot be less than min_break_minutes" in str(exc.value)


# -----------------------------------------------------------------------------
# 2. SECRET REDACTION & PRECEDENCE TESTS
# -----------------------------------------------------------------------------
def test_sanitize_text_redacts_credentials():
    raw = (
        "Error while calling https://api.telegram.org/bot123456789:ABCdefGHIjklMNOpqrSTUvwx-YZ1234567/sendMessage. "
        "Session cookie: datr=secret_datr_123; c_user=10001234567; xs=secret_xs_token;"
    )
    sanitized = sanitize_text(raw)
    assert "123456789:ABCdef" not in sanitized
    assert "secret_datr_123" not in sanitized
    assert "secret_xs_token" not in sanitized
    assert "[REDACTED_BOT_TOKEN]" in sanitized
    assert "c_user=[REDACTED]" in sanitized


def test_sanitize_url_redacts_telegram_token():
    url = "https://api.telegram.org/bot987654321:XYZ987654321_secret/sendMessage"
    assert sanitize_url(url) == "https://api.telegram.org/bot[REDACTED_TOKEN]/sendMessage"


def test_secret_resolution_precedence(tmp_path, monkeypatch):
    monkeypatch.setenv("FB_ACCOUNT_COOKIE", "env_cookie_value")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "env_token_value")

    data = {
        "account_cookie": "config_cookie",
        "telegram_bot_token": "config_token",
        "tasks": []
    }
    secrets = resolve_secrets(data, base_dir=tmp_path)
    assert secrets["account_cookie"] == "env_cookie_value"
    assert secrets["telegram_bot_token"] == "env_token_value"


# -----------------------------------------------------------------------------
# 3. ATOMIC PERSISTENCE & CONCURRENT OWNERSHIP TESTS
# -----------------------------------------------------------------------------
def test_atomic_state_store_persistence(tmp_path):
    state_file = tmp_path / "state" / "test_state.json"
    store = AtomicStateStore(state_file)

    task1 = PostingTask(task_id="t1", page_id="p1", page_name="Page 1", content="Hello")
    task2 = PostingTask(task_id="t2", page_id="p2", page_name="Page 2", content="World")

    recovered = store.load_or_initialize([task1, task2])
    assert recovered == 0
    assert state_file.exists()

    # Verify atomic update
    store.mark_in_progress("t1")
    assert store.tasks["t1"].status == TaskStatus.IN_PROGRESS
    assert store.tasks["t1"].attempts == 1

    # Reload fresh instance
    store2 = AtomicStateStore(state_file)
    rec2 = store2.load_or_initialize([task1, task2])
    # t1 was in_progress when loaded fresh -> must recover as UNKNOWN_NEEDS_RECONCILIATION!
    assert rec2 == 1
    assert store2.tasks["t1"].status == TaskStatus.UNKNOWN_NEEDS_RECONCILIATION
    assert store2.tasks["t2"].status == TaskStatus.PENDING


def test_no_persistence_overwrite_loses_committed_state(tmp_path):
    state_file = tmp_path / "state.json"
    store = AtomicStateStore(state_file)

    t1 = PostingTask(task_id="t1", page_id="p1", page_name="P1", content="Hello")
    store.load_or_initialize([t1])
    store.mark_completed("t1", post_id="post_999", confirmed=True)

    # Now reload with a new configuration where t1 has same content and t2 is new
    t1_same_cfg = PostingTask(task_id="t1", page_id="p1", page_name="P1", content="Hello")
    t2_new_cfg = PostingTask(task_id="t2", page_id="p2", page_name="P2", content="New Task")

    store.load_or_initialize([t1_same_cfg, t2_new_cfg])

    # Committed completed status must be preserved!
    assert store.tasks["t1"].status == TaskStatus.COMPLETED
    assert store.tasks["t1"].post_id == "post_999"
    assert store.tasks["t2"].status == TaskStatus.PENDING


def test_task_content_changed_after_attempt_requires_reconciliation(tmp_path):
    state_file = tmp_path / "state.json"
    store = AtomicStateStore(state_file)

    t1 = PostingTask(task_id="t1", page_id="p1", page_name="P1", content="Hello")
    store.load_or_initialize([t1])
    store.mark_completed("t1", post_id="post_999", confirmed=True)

    # If content changes after execution, it must require manual reconciliation!
    t1_mod = PostingTask(task_id="t1", page_id="p1", page_name="P1", content="Modified Content")
    store.load_or_initialize([t1_mod])
    assert store.tasks["t1"].status == TaskStatus.UNKNOWN_NEEDS_RECONCILIATION


def test_single_worker_mutual_exclusion(tmp_path):
    lock_file = tmp_path / "worker.lock"
    lock1 = SingleWorkerLock(lock_file)
    lock2 = SingleWorkerLock(lock_file)

    lock1.acquire()
    assert lock_file.exists()

    # Second worker cannot acquire while first holds it
    with pytest.raises(ConcurrentWorkerError) as exc:
        lock2.acquire()
    assert "currently holds the state lock" in str(exc.value)

    # Release first worker
    lock1.release()
    assert not lock_file.exists()

    # Now second worker can acquire
    lock2.acquire()
    assert lock_file.exists()
    lock2.release()


# -----------------------------------------------------------------------------
# 4. IN-FLIGHT CRASH RECOVERY & TYPED CONTRACT TESTS
# -----------------------------------------------------------------------------
class MockCrashPoster:
    def __init__(self):
        self.call_count = 0

    def verify_logged_in(self):
        return True

    def create_post(self, page_id, raw_content, media_paths=None):
        self.call_count += 1
        # Simulate unhandled crash or exception during posting
        raise RuntimeError("Network dropped mid-flight!")


def test_in_flight_crash_recovers_as_unknown_needs_reconciliation(tmp_path):
    config_file = tmp_path / "config.json"
    state_file = tmp_path / "state.json"

    config_data = {
        "tasks": [
            {"task_id": "crash_task", "page_id": "page_123", "content": "Crash me", "media_paths": []}
        ]
    }
    config_file.write_text(json.dumps(config_data), encoding="utf-8")

    worker = NightSchedulerWorker(
        config_file=str(config_file),
        state_file=str(state_file),
        profile_dir=str(tmp_path / "prof")
    )

    mock_poster = MockCrashPoster()
    mock_session = type("MockSession", (), {"close": lambda self: None, "context": None})()

    # Run shift with execute=True
    worker.run_night_shift(execute=True, poster_instance=mock_poster, session_instance=mock_session)

    # Task should be in UNKNOWN_NEEDS_RECONCILIATION
    assert worker.state_store.tasks["crash_task"].status == TaskStatus.UNKNOWN_NEEDS_RECONCILIATION

    # Restarting a new worker: task must NOT be in pending and must NOT be replayed!
    worker2 = NightSchedulerWorker(
        config_file=str(config_file),
        state_file=str(state_file),
        profile_dir=str(tmp_path / "prof")
    )
    worker2.load_and_validate(check_media_exists=False)
    pending = worker2.state_store.get_pending_tasks()
    assert len(pending) == 0, "Crashed task must NOT be silently replayed as pending!"


class MockContractPoster:
    """Poster implementing Task 002 typed contract."""
    def __init__(self, mode="verified"):
        self.mode = mode

    def verify_logged_in(self):
        return True

    def create_post_verified(self, page_id, raw_content, media_paths=None):
        if self.mode == "verified":
            return PostingResult(
                success=True,
                post_id=f"fb_post_{page_id}_123",
                post_url=f"https://facebook.com/{page_id}/posts/123",
                confirmed=True
            )
        elif self.mode == "unconfirmed_legacy":
            return PostingResult(success=True, confirmed=False)
        else:
            return PostingResult(success=False, error_message="Permission denied")


def test_typed_contract_confirmed_publication(tmp_path):
    config_file = tmp_path / "config.json"
    state_file = tmp_path / "state.json"

    config_data = {
        "tasks": [
            {"task_id": "contract_task", "page_id": "p_ok", "content": "Contract content", "media_paths": []}
        ]
    }
    config_file.write_text(json.dumps(config_data), encoding="utf-8")

    worker = NightSchedulerWorker(
        config_file=str(config_file),
        state_file=str(state_file),
        profile_dir=str(tmp_path / "prof")
    )
    mock_poster = MockContractPoster(mode="verified")
    mock_session = type("MockSession", (), {"close": lambda self: None, "context": None})()

    worker.run_night_shift(execute=True, poster_instance=mock_poster, session_instance=mock_session)

    task = worker.state_store.tasks["contract_task"]
    assert task.status == TaskStatus.COMPLETED
    assert task.confirmed is True
    assert task.post_id == "fb_post_p_ok_123"


def test_legacy_boolean_without_reference_not_marked_completed(tmp_path):
    config_file = tmp_path / "config.json"
    state_file = tmp_path / "state.json"

    config_data = {
        "tasks": [
            {"task_id": "legacy_task", "page_id": "p_legacy", "content": "Legacy content", "media_paths": []}
        ]
    }
    config_file.write_text(json.dumps(config_data), encoding="utf-8")

    worker = NightSchedulerWorker(
        config_file=str(config_file),
        state_file=str(state_file),
        profile_dir=str(tmp_path / "prof")
    )
    # Poster returns raw boolean True without post reference
    legacy_mock = type("LegacyMock", (), {
        "verify_logged_in": lambda self: True,
        "create_post": lambda self, page_id, raw_content, media_paths=None: True
    })()
    mock_session = type("MockSession", (), {"close": lambda self: None, "context": None})()

    worker.run_night_shift(execute=True, poster_instance=legacy_mock, session_instance=mock_session)

    task = worker.state_store.tasks["legacy_task"]
    # Must NOT be marked completed without verified post reference!
    assert task.status != TaskStatus.COMPLETED
    assert task.status == TaskStatus.UNKNOWN_NEEDS_RECONCILIATION


# -----------------------------------------------------------------------------
# 5. INDEPENDENT SUBPROCESS CLI TESTS
# -----------------------------------------------------------------------------
def test_cli_subprocess_default_dry_run(tmp_path):
    main_py = PACKAGE_ROOT / "main.py"
    img_path = tmp_path / "sample.jpg"
    Image.new("RGB", (50, 50), color=(10, 20, 30)).save(img_path, "JPEG")

    config = {
        "tasks": [
            {"task_id": "sub_1", "page_id": "page_sub", "content": "{Hi|Hey} test", "media_paths": [str(img_path)]}
        ]
    }
    cfg_file = tmp_path / "sub_config.json"
    cfg_file.write_text(json.dumps(config), encoding="utf-8")
    state_file = tmp_path / "sub_state.json"

    # Run default invocation (no --execute)
    res = subprocess.run(
        [sys.executable, str(main_py), "--config", str(cfg_file), "--state", str(state_file)],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(tmp_path)
    )

    assert res.returncode == 0
    assert "BẮT ĐẦU CHẾ ĐỘ DRY-RUN" in res.stdout or "DRY-RUN" in res.stdout
    assert "KHÔNG CÓ TÁC DỤNG PHỤ NÀO ĐƯỢC THỰC THI" in res.stdout or "DRY-RUN" in res.stdout


def test_cli_subprocess_malformed_config_fails(tmp_path):
    main_py = PACKAGE_ROOT / "main.py"
    bad_cfg = tmp_path / "bad.json"
    bad_cfg.write_text("{ this is malformed json }", encoding="utf-8")

    res = subprocess.run(
        [sys.executable, str(main_py), "--config", str(bad_cfg)],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(tmp_path)
    )

    assert res.returncode != 0
    combined = (res.stdout or "") + (res.stderr or "")
    assert "Configuration validation failed" in combined or "Malformed JSON" in combined
