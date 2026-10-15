"""Main entry point for Facebook Auto Poster multi-page night scheduler.

Provides reliable execution, dry-run validation, atomic persistence,
single-worker mutual exclusion, and crash recovery.
"""

import os
import sys
import json
import time
import shutil
import random
import ctypes
import logging
import argparse
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List, Dict, Any

# Ensure local directory is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.contracts import TaskStatus, PostingTask, PostingResult, WorkerTimingConfig
from core.config_loader import (
    load_and_validate_config,
    ConfigValidationError,
    mask_secret,
    sanitize_text,
    sanitize_url
)
from core.state_manager import (
    AtomicStateStore,
    SingleWorkerLock,
    ConcurrentWorkerError,
    PersistenceError
)

# Ensure stdout uses UTF-8 if available
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# Logging configuration with secret sanitization
class SanitizedStreamHandler(logging.StreamHandler):
    def emit(self, record):
        try:
            record.msg = sanitize_text(str(record.msg))
            try:
                super().emit(record)
            except UnicodeEncodeError:
                safe_str = record.msg.encode("ascii", errors="replace").decode("ascii")
                record.msg = safe_str
                super().emit(record)
        except Exception:
            self.handleError(record)

class SanitizedFileHandler(logging.FileHandler):
    def emit(self, record):
        try:
            record.msg = sanitize_text(str(record.msg))
            super().emit(record)
        except Exception:
            self.handleError(record)

logger = logging.getLogger("facebook_auto_poster")
logger.setLevel(logging.INFO)
_file_handler = SanitizedFileHandler("night_poster.log", encoding="utf-8")
_stream_handler = SanitizedStreamHandler(sys.stdout)
_formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
_file_handler.setFormatter(_formatter)
_stream_handler.setFormatter(_formatter)
logger.addHandler(_file_handler)
logger.addHandler(_stream_handler)


def prevent_windows_sleep():
    """Inhibit Windows automatic sleep during night shift."""
    if sys.platform == "win32":
        try:
            ES_CONTINUOUS = 0x80000000
            ES_SYSTEM_REQUIRED = 0x00000001
            ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
            logger.info("Đã kích hoạt chế độ ngăn máy tính Sleep (Windows Sleep Inhibition).")
        except Exception as e:
            logger.warning(f"Không thể thiết lập trạng thái chống Sleep: {e}")


def allow_windows_sleep():
    """Release Windows sleep inhibition."""
    if sys.platform == "win32":
        try:
            ES_CONTINUOUS = 0x80000000
            ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
            logger.info("Đã giải phóng chế độ ngăn Sleep, máy tính có thể tự ngủ theo cấu hình hệ thống.")
        except Exception as e:
            logger.warning(f"Không thể giải phóng trạng thái chống Sleep: {e}")


def send_telegram_report(bot_token: str, chat_id: str, message: str):
    """Send formatted notification via Telegram with credentials redacted."""
    if not bot_token or not chat_id:
        return
    import requests
    safe_msg = sanitize_text(message)
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    try:
        requests.post(url, json={"chat_id": chat_id, "text": safe_msg}, timeout=10)
    except Exception as e:
        safe_err = sanitize_text(str(e))
        logger.warning(f"Không thể gửi thông báo Telegram: {safe_err}")


class NightSchedulerWorker:
    """
    Coordinates safe, resilient batch posting to Facebook Pages.
    Separates configuration from runtime state, guarantees atomic state transitions,
    and enforces single-worker execution.
    """
    def __init__(
        self,
        config_file: Optional[str] = None,
        state_file: Optional[str] = None,
        profile_dir: str = "./fb_master_profile",
        timing_override: Optional[WorkerTimingConfig] = None,
        data_file: Optional[str] = None
    ):
        actual_config = config_file or data_file or "./config/pages_and_posts.json"
        actual_state = state_file or "./state/runner_state.json"

        self.config_file = Path(actual_config).resolve()
        self.state_file = Path(actual_state).resolve()
        self.profile_dir = Path(profile_dir).resolve()
        self.timing_override = timing_override

        self.lock_file = self.state_file.parent / f"{self.state_file.stem}.lock"
        self.lock = SingleWorkerLock(self.lock_file)
        self.state_store = AtomicStateStore(self.state_file)

        self.raw_config: Dict[str, Any] = {}
        self.configured_tasks: List[PostingTask] = []
        self.secrets: Dict[str, str] = {}
        self.timing: WorkerTimingConfig = WorkerTimingConfig()

    def load_data(self) -> Dict[str, Any]:
        """Legacy helper for loading raw json data."""
        with open(self.config_file, "r", encoding="utf-8") as f:
            return json.load(f)

    def save_data(self, data: Dict[str, Any]) -> None:
        """Legacy helper for saving data."""
        with open(self.config_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def load_and_validate(self, check_media_exists: bool = True) -> Dict[str, Any]:
        """
        Validate configuration schema and initialize/recover state store.
        Does NOT launch browser or perform network calls.
        """
        raw_cfg, tasks, secrets, timing_cfg = load_and_validate_config(
            self.config_file,
            check_media_exists=check_media_exists
        )
        self.raw_config = raw_cfg
        self.configured_tasks = tasks
        self.secrets = secrets
        self.timing = self.timing_override if self.timing_override else timing_cfg

        recovered_count = self.state_store.load_or_initialize(self.configured_tasks)
        summary = self.state_store.get_summary()

        return {
            "valid": True,
            "config_file": str(self.config_file),
            "state_file": str(self.state_file),
            "total_configured": len(self.configured_tasks),
            "recovered_crashed_tasks": recovered_count,
            "status_summary": summary,
            "cookie_configured": bool(self.secrets.get("account_cookie")),
            "telegram_configured": bool(self.secrets.get("telegram_bot_token") and self.secrets.get("telegram_chat_id")),
            "timing": {
                "min_break_minutes": self.timing.min_break_minutes,
                "max_break_minutes": self.timing.max_break_minutes,
                "batch_size": self.timing.batch_size,
            }
        }

    def run_dry_run(self) -> int:
        """
        Execute dry-run validation without browser or network side effects.
        Outputs execution plan and exits cleanly.
        """
        logger.info("=== BẮT ĐẦU CHẾ ĐỘ DRY-RUN (KIỂM TRA CẤU HÌNH & TRẠNG THÁI) ===")
        try:
            info = self.load_and_validate(check_media_exists=True)
        except ConfigValidationError as e:
            logger.error(str(e))
            return 1
        except Exception as e:
            logger.error(f"Lỗi kiểm tra cấu hình: {e}")
            return 1

        logger.info(f"[*] Tệp cấu hình: {info['config_file']}")
        logger.info(f"[*] Tệp trạng thái: {info['state_file']}")
        logger.info(f"[*] Cookie Facebook: {'[ĐÃ CẤU HÌNH]' if info['cookie_configured'] else '[CHƯA CẤU HÌNH (Cần thiết khi chạy thật)]'}")
        logger.info(f"[*] Thông báo Telegram: {'[ĐÃ CẤU HÌNH]' if info['telegram_configured'] else '[TẮT]'}")
        logger.info(f"[*] Tổng số bài viết trong cấu hình: {info['total_configured']}")
        logger.info(f"[*] Phân loại trạng thái hiện tại: {info['status_summary']}")

        if info["recovered_crashed_tasks"] > 0:
            logger.warning(
                f"🚨 PHÁT HIỆN {info['recovered_crashed_tasks']} TÁC VỤ BỊ GIÁN ĐOẠN Ở LẦN CHẠY TRƯỚC! "
                f"Đã chuyển sang trạng thái '{TaskStatus.UNKNOWN_NEEDS_RECONCILIATION.value}'. "
                f"Hệ thống sẽ KHÔNG tự động chạy lại các bài này để chống trùng lặp."
            )

        pending = self.state_store.get_pending_tasks()
        logger.info(f"[*] Số bài viết sẵn sàng chờ đăng (pending): {len(pending)}")
        for idx, t in enumerate(pending[:5]):
            logger.info(f"    - Task [{idx+1}/{len(pending)}]: id={t.task_id}, page={t.page_name} ({t.page_id}), media={len(t.media_paths)} files")
        if len(pending) > 5:
            logger.info(f"    ... và {len(pending)-5} bài viết khác.")

        logger.info(
            f"[*] Kế hoạch giãn cách: Nghỉ {self.timing.min_break_minutes}-{self.timing.max_break_minutes} phút giữa các Page; "
            f"nghỉ lớn {self.timing.long_break_min_minutes}-{self.timing.long_break_max_minutes} phút sau mỗi {self.timing.batch_size} Page."
        )
        logger.info("=== KẾT THÚC DRY-RUN: CẤU HÌNH HỢP LỆ, KHÔNG CÓ TÁC DỤNG PHỤ NÀO ĐƯỢC THỰC THI ===")
        return 0

    def run_night_shift(
        self,
        execute: bool = False,
        poster_instance: Any = None,
        session_instance: Any = None
    ) -> int:
        """
        Execute live night shift with strict safety controls, in-flight persistence,
        and single-worker locking.
        """
        if not execute:
            return self.run_dry_run()

        # 1. Acquire single-worker mutual exclusion lock
        try:
            self.lock.acquire()
        except ConcurrentWorkerError as e:
            logger.error(f"🚨 TỪ CHỐI THỰC THI DO XUNG ĐỘT TIẾN TRÌNH: {e}")
            return 2

        # 2. Inhibit Windows Sleep
        prevent_windows_sleep()

        # 3. Validate configuration and initialize state
        try:
            info = self.load_and_validate(check_media_exists=True)
        except Exception as e:
            logger.error(f"Lỗi khởi tạo ca đêm: {e}")
            self.lock.release()
            allow_windows_sleep()
            return 1

        cookie = self.secrets.get("account_cookie", "")
        bot_token = self.secrets.get("telegram_bot_token", "")
        chat_id = self.secrets.get("telegram_chat_id", "")

        pending_tasks = self.state_store.get_pending_tasks()
        if not pending_tasks:
            logger.info("Không có bài viết nào ở trạng thái 'pending' cần xử lý.")
            self.lock.release()
            allow_windows_sleep()
            return 0

        logger.info(f"=== BẮT ĐẦU CA CHẠY ĐÊM THẬT: {len(pending_tasks)} BÀI VIẾT ĐANG CHỜ ===")
        session = session_instance
        poster = poster_instance

        success_count = 0
        fail_count = 0
        consecutive_failures = 0

        try:
            # Initialize browser session if not injected
            if session is None:
                from core.session_manager import FacebookSessionManager
                from core.fb_poster import FacebookPagePoster
                session = FacebookSessionManager(profile_dir=str(self.profile_dir), raw_cookie=cookie)
                session.start_browser(headless=False)
                poster = FacebookPagePoster(session)

            # Pre-flight login verification
            if hasattr(poster, "verify_logged_in") and not poster.verify_logged_in():
                msg = "🚨 CẢNH BÁO: Phiên đăng nhập Facebook hết hạn hoặc dính Checkpoint! Đã dừng ca đêm ngay lập tức."
                logger.error(msg)
                send_telegram_report(bot_token, chat_id, msg)
                return 3

            for index, task in enumerate(pending_tasks):
                if consecutive_failures >= self.timing.max_consecutive_failures:
                    logger.error(
                        f"🚨 CẦU CHÌ TỰ NGẮT ĐÃ KÍCH HOẠT: {consecutive_failures} Page liên tiếp thất bại. "
                        f"Dừng ca đêm khẩn cấp để bảo vệ an toàn tài khoản!"
                    )
                    break

                logger.info(f"[{index+1}/{len(pending_tasks)}] Đang xử lý: {task.page_name} ({task.page_id})")

                # In-flight record persisted atomically BEFORE side effect attempt
                try:
                    self.state_store.mark_in_progress(task.task_id)
                except PersistenceError as pe:
                    logger.error(f"Lỗi lưu trạng thái in-flight cho task {task.task_id}: {pe}. Dừng trước khi gọi mạng.")
                    break

                # Execute posting side effect
                try:
                    # Check if poster supports verified contract (Task 002)
                    if hasattr(poster, "create_post_verified"):
                        result: PostingResult = poster.create_post_verified(
                            page_id=task.page_id,
                            raw_content=task.content,
                            media_paths=task.media_paths
                        )
                    else:
                        # Legacy poster adapter call
                        raw_ok = poster.create_post(
                            page_id=task.page_id,
                            raw_content=task.content,
                            media_paths=task.media_paths
                        )
                        if isinstance(raw_ok, PostingResult):
                            result = raw_ok
                        elif raw_ok is True:
                            # Acceptance criterion: Legacy boolean success is not proof of confirmed publication;
                            # do not mark completed without verified outcome and post reference.
                            # We mark as verified only if confirmed post reference is provided.
                            # In Task 001 compatibility mode, record unverified reference:
                            result = PostingResult(
                                success=True,
                                confirmed=False,
                                error_message="Legacy unverified publication (missing post reference contract for Task 002)"
                            )
                        else:
                            result = PostingResult(success=False, error_message="Legacy poster returned failure")

                    if result.is_verified_success():
                        self.state_store.mark_completed(
                            task_id=task.task_id,
                            post_id=result.post_id,
                            post_url=result.post_url,
                            confirmed=True
                        )
                        success_count += 1
                        consecutive_failures = 0
                        logger.info(f"--> THÀNH CÔNG (Xác nhận): {task.page_name} (post_id={result.post_id})")
                    elif result.success and not result.confirmed and not result.post_id and not result.post_url:
                        # Legacy unconfirmed success: transition to unknown_needs_reconciliation to avoid unverified completion
                        self.state_store.mark_unknown_reconciliation(
                            task_id=task.task_id,
                            reason="Legacy unconfirmed publication: missing verified post reference"
                        )
                        logger.warning(f"--> CẦN ĐỐI SOÁT: {task.page_name} (Thành công legacy nhưng chưa có post_id xác nhận)")
                    else:
                        self.state_store.mark_failed(
                            task_id=task.task_id,
                            error_message=result.error_message or "Posting failed"
                        )
                        fail_count += 1
                        consecutive_failures += 1
                        logger.warning(f"--> THẤT BẠI: {task.page_name} ({result.error_message})")

                except Exception as post_exc:
                    err_msg = sanitize_text(str(post_exc))
                    logger.error(f"Lỗi ngoại lệ khi đăng bài cho {task.page_name}: {err_msg}")
                    # Uncertain outcome -> UNKNOWN_NEEDS_RECONCILIATION
                    self.state_store.mark_unknown_reconciliation(
                        task_id=task.task_id,
                        reason=f"Exception during posting: {err_msg}"
                    )
                    fail_count += 1
                    consecutive_failures += 1

                # Interval pacing between tasks
                if index < len(pending_tasks) - 1:
                    if (success_count + fail_count) % self.timing.batch_size == 0:
                        long_sleep = random.randint(
                            self.timing.long_break_min_minutes * 60,
                            self.timing.long_break_max_minutes * 60
                        )
                        logger.info(f"Đã chạy đủ lô {self.timing.batch_size} Page. Nghỉ dài {long_sleep // 60} phút...")
                        time.sleep(long_sleep)
                    else:
                        sleep_sec = random.randint(
                            self.timing.min_break_minutes * 60,
                            self.timing.max_break_minutes * 60
                        )
                        logger.info(f"Nghỉ {sleep_sec // 60} phút {sleep_sec % 60} giây trước khi sang Page tiếp...")
                        time.sleep(sleep_sec)

        finally:
            # Safe cleanup in all cases: session, lock, temp_media, sleep inhibition
            if session:
                try:
                    if hasattr(session, "context") and session.context:
                        session.context.clear_cookies(name="i_user")
                except Exception:
                    pass
                try:
                    session.close()
                except Exception:
                    pass

            if os.path.exists("./temp_media"):
                shutil.rmtree("./temp_media", ignore_errors=True)

            self.lock.release()
            allow_windows_sleep()

            summary = (
                f"📊 BÁO CÁO CA ĐÊM ({datetime.now().strftime('%d/%m/%Y %H:%M')})\n"
                f"- Thành công: {success_count} Page ✅\n"
                f"- Thất bại: {fail_count} Page ❌\n"
                f"- Trạng thái Cầu chì: {'An toàn 🟢' if consecutive_failures < self.timing.max_consecutive_failures else 'Đã ngắt khẩn cấp 🔴'}"
            )
            logger.info("\n" + summary)
            send_telegram_report(bot_token, chat_id, summary)
            logger.info("=== KẾT THÚC CA CHẠY ĐÊM ===")

        return 0


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Facebook Auto Poster — Multi-Page Night Scheduler (Safe & Resilient Runner)"
    )
    parser.add_argument(
        "--config",
        type=str,
        default="./config/pages_and_posts.json",
        help="Path to tasks configuration JSON file (default: ./config/pages_and_posts.json)"
    )
    parser.add_argument(
        "--state",
        type=str,
        default="./state/runner_state.json",
        help="Path to persistent state JSON file (default: ./state/runner_state.json)"
    )
    parser.add_argument(
        "--profile-dir",
        type=str,
        default="./fb_master_profile",
        help="Path to Chrome user data profile directory (default: ./fb_master_profile)"
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        default=False,
        help="Explicitly enable live browser execution (default is dry-run validation mode)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Run schema validation and execution simulation without side effects (default mode)"
    )
    parser.add_argument(
        "--min-break",
        type=int,
        default=None,
        help="Override minimum break minutes between posts"
    )
    parser.add_argument(
        "--max-break",
        type=int,
        default=None,
        help="Override maximum break minutes between posts"
    )
    return parser


def main():
    parser = build_arg_parser()
    args = parser.parse_args()

    script_dir = Path(__file__).resolve().parent

    # Resolve config path
    config_path = Path(args.config)
    if not config_path.exists():
        # Check relative to script directory
        candidate = script_dir / args.config
        if candidate.exists():
            config_path = candidate
        elif args.config == "./config/pages_and_posts.json":
            example = script_dir / "config" / "pages_and_posts.example.json"
            if example.exists():
                config_path = example

    state_path = Path(args.state)
    if not state_path.is_absolute() and not state_path.parent.exists():
        state_path = script_dir / args.state

    timing_override = None
    if args.min_break is not None or args.max_break is not None:
        min_b = args.min_break or 3
        max_b = args.max_break or 6
        timing_override = WorkerTimingConfig(min_break_minutes=min_b, max_break_minutes=max_b)

    worker = NightSchedulerWorker(
        config_file=str(config_path),
        state_file=str(state_path),
        profile_dir=args.profile_dir,
        timing_override=timing_override
    )

    if args.execute:
        exit_code = worker.run_night_shift(execute=True)
    else:
        exit_code = worker.run_dry_run()

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
