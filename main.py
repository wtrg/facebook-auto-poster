import os
import sys
import json
import time
import shutil
import random
import ctypes
import logging
import requests
from datetime import datetime
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from core.session_manager import FacebookSessionManager
from core.fb_poster import FacebookPagePoster

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("night_poster.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)

def prevent_windows_sleep():
    """Giữ máy tính Windows không tự động Sleep khi chạy ban đêm."""
    if sys.platform == "win32":
        ES_CONTINUOUS = 0x80000000
        ES_SYSTEM_REQUIRED = 0x00000001
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)

def send_telegram_report(bot_token: str, chat_id: str, message: str):
    if not bot_token or not chat_id:
        return
    try:
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        requests.post(url, json={"chat_id": chat_id, "text": message}, timeout=10)
    except Exception as e:
        logging.warning(f"Không thể gửi thông báo Telegram: {e}")

class NightSchedulerWorker:
    def __init__(self, data_file: str, profile_dir: str):
        self.data_file = data_file
        self.profile_dir = profile_dir

    def load_data(self):
        with open(self.data_file, "r", encoding="utf-8") as f:
            return json.load(f)

    def save_data(self, data):
        with open(self.data_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def run_night_shift(self, min_break_minutes: int = 3, max_break_minutes: int = 6):
        prevent_windows_sleep()
        data = self.load_data()
        raw_cookie = data.get("account_cookie", "")
        bot_token = data.get("telegram_bot_token", "")
        chat_id = data.get("telegram_chat_id", "")
        tasks = data.get("tasks", [])

        pending_tasks = [t for t in tasks if t.get("status") == "pending"]
        if not pending_tasks:
            logging.info("Không có bài viết nào trong hàng đợi cần đăng.")
            return

        logging.info(f"=== BẮT ĐẦU CA CHẠY ĐÊM: {len(pending_tasks)} BÀI VIẾT ===")
        session = FacebookSessionManager(profile_dir=self.profile_dir, raw_cookie=raw_cookie)
        success_count = 0
        fail_count = 0
        consecutive_failures = 0

        try:
            session.start_browser(headless=False)
            poster = FacebookPagePoster(session)

            if not poster.verify_logged_in():
                send_telegram_report(bot_token, chat_id, "🚨 CẢNH BÁO: Phiên đăng nhập Facebook đã hết hạn hoặc bị Checkpoint! Đã dừng ca đêm.")
                return

            for index, task in enumerate(tasks):
                if task.get("status") != "pending":
                    continue

                # Cầu chì tự ngắt nếu lỗi 3 bài liên tiếp
                if consecutive_failures >= 3:
                    logging.error("🚨 CẦU CHÌ TỰ NGẮT: 3 Page liên tiếp thất bại. Dừng ca đêm để bảo vệ tài khoản!")
                    break

                page_id = task["page_id"]
                page_name = task.get("page_name", page_id)
                logging.info(f"[{index+1}/{len(tasks)}] Đang xử lý Page: {page_name} ({page_id})")

                ok = poster.create_post(
                    page_id=page_id,
                    raw_content=task["content"],
                    media_paths=task.get("media_paths", [])
                )

                if ok:
                    task["status"] = "completed"
                    task["posted_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    success_count += 1
                    consecutive_failures = 0
                    logging.info(f"--> THÀNH CÔNG: {page_name}")
                else:
                    task["status"] = "failed"
                    fail_count += 1
                    consecutive_failures += 1
                    logging.warning(f"--> THẤT BẠI: {page_name}")

                self.save_data(data)

                if index < len(tasks) - 1:
                    # Nghỉ giải lao dài sau mỗi 15 Page
                    if (success_count + fail_count) % 15 == 0:
                        long_sleep = random.randint(20 * 60, 30 * 60)
                        logging.info(f"Đã chạy đủ 1 lô 15 Page. Nghỉ giải lao dài {long_sleep // 60} phút...")
                        time.sleep(long_sleep)
                    else:
                        sleep_sec = random.randint(min_break_minutes * 60, max_break_minutes * 60)
                        logging.info(f"Nghỉ {sleep_sec // 60} phút {sleep_sec % 60} giây trước khi sang Page tiếp theo...")
                        time.sleep(sleep_sec)

        finally:
            try:
                session.context.clear_cookies(name="i_user")
            except Exception:
                pass
            session.close()
            if os.path.exists("./temp_media"):
                shutil.rmtree("./temp_media", ignore_errors=True)

            summary = (
                f"📊 BÁO CÁO CA ĐÊM ({datetime.now().strftime('%d/%m/%Y %H:%M')})\n"
                f"- Thành công: {success_count} Page ✅\n"
                f"- Thất bại: {fail_count} Page ❌\n"
                f"- Trạng thái Cầu chì: {'An toàn 🟢' if consecutive_failures < 3 else 'Đã ngắt khẩn cấp 🔴'}"
            )
            logging.info("\n" + summary)
            send_telegram_report(bot_token, chat_id, summary)
            logging.info("=== KẾT THÚC CA CHẠY ĐÊM ===")

if __name__ == "__main__":
    worker = NightSchedulerWorker(
        data_file="./config/pages_and_posts.json",
        profile_dir="./fb_master_profile"
    )
    worker.run_night_shift(min_break_minutes=3, max_break_minutes=6)
