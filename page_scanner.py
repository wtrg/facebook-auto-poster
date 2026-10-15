import json
import time
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from core.session_manager import FacebookSessionManager

def scan_all_pages(profile_dir: str = "./fb_master_profile", cookie_str: str = ""):
    """
    Tự động mở trang danh sách Fanpage của bạn để bạn lấy danh sách Page ID.
    """
    session = FacebookSessionManager(profile_dir=profile_dir, raw_cookie=cookie_str)
    try:
        session.start_browser(headless=False)
        session.page.goto("https://www.facebook.com/pages/?category=your_pages", wait_until="domcontentloaded")
        print("[*] Đang cuộn trang để tải toàn bộ 50 Page...")
        for _ in range(6):
            session.page.mouse.wheel(0, 1200)
            time.sleep(2.5)

        links = session.page.locator("a[href*='facebook.com/']").all()
        discovered = {}
        for link in links:
            href = link.get_attribute("href") or ""
            text = (link.inner_text() or "").strip()
            if text and ("profile.php?id=" in href or "/pages/" not in href):
                discovered[href] = text

        with open("discovered_pages.json", "w", encoding="utf-8") as f:
            json.dump(discovered, f, ensure_ascii=False, indent=2)
        print(f"[+] Đã xuất danh sách liên kết Page ra file discovered_pages.json ({len(discovered)} mục).")
    finally:
        session.close()

if __name__ == "__main__":
    scan_all_pages()
