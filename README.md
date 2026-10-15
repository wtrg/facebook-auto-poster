# Facebook Auto Poster — Multi-Page Night Scheduler (Reliable Runner)

Công cụ tự động hóa xuất bản bài viết và hình ảnh lên hàng loạt Fanpage Facebook (hỗ trợ tới 50+ Page) theo ca đêm an toàn với cơ chế chống checkpoint, biến dị anti-spam, lưu trữ trạng thái nguyên tử (Atomic State), khóa tiến trình đơn (Single-Worker Lock) và phục hồi sau sự cố (Crash Recovery).

---

## 🌟 Tính Năng Nổi Bật & Kiến Trúc Đảm Bảo An Toàn

1. **Chế độ Dry-Run Mặc Định (An toàn 100%)**:
   - Khi chạy lệnh `python main.py` mà không có cờ `--execute`, hệ thống mặc định hoạt động ở chế độ **Dry-Run**.
   - Kiểm tra cú pháp schema, xác thực tính duy nhất của `task_id`, tính cân bằng Spintax `{...}`, kiểm tra sự tồn tại của tệp media trên ổ đĩa và in kế hoạch chạy mô phỏng.
   - Tuyệt đối **không mở trình duyệt**, **không gọi mạng** và **không làm thay đổi hàng đợi trạng thái**.

2. **Lưu Trữ Trạng Thái Nguyên Tử (Atomic State Store)**:
   - Tách biệt hoàn toàn tệp cấu hình chia sẻ (`config/pages_and_posts.json` hoặc `.example.json`) khỏi tệp trạng thái vận hành (`state/runner_state.json`).
   - Mọi thay đổi trạng thái đều được ghi nguyên tử (Atomic Write) qua tệp tạm, `flush()`, `os.fsync()` và `os.replace()`, ngăn ngừa hỏng dữ liệu khi mất điện hoặc dừng đột ngột.
   - Không ghi đè làm mất các tác vụ đã hoàn thành trước đó.

3. **Ghi Nhận In-Flight & Phục Hồi Sau Sự Cố (Crash Recovery)**:
   - Trước khi bắt đầu gọi hành động đăng bài, tác vụ được lưu bền vững ở trạng thái `in_progress`.
   - Nếu tiến trình bị kill, sập mạng hoặc crash giữa chừng, ở lần khởi động kế tiếp, hệ thống tự động phát hiện và chuyển tác vụ đó sang trạng thái `unknown_needs_reconciliation`.
   - **Chống Đăng Trùng Lặp**: Các bài viết ở trạng thái cần đối soát sẽ không bao giờ được tự động phát lại (không replay âm thầm).

4. **Khóa Tiến Trình Đơn (Single-Worker Mutual Exclusion)**:
   - Sử dụng khóa tệp nguyên tử (`runner_state.lock`) ghi nhận PID, máy chủ và thời điểm chiếm giữ.
   - Ngăn chặn hai tiến trình hoặc hai worker chạy đồng thời trên cùng một tệp trạng thái, loại bỏ hoàn toàn rủi ro xung đột đăng trùng.

5. **Hợp Đồng Xác Thực Xuất Bản (Typed Contract cho Task 002)**:
   - Định nghĩa hợp đồng kiểu dữ liệu `PostingResult(success, post_id, post_url, confirmed)`.
   - Thành công boolean cũ không được coi là bằng chứng xuất bản; hệ thống chỉ đánh dấu `completed` khi có xác nhận hoặc ID/URL bài viết được kiểm chứng.

6. **Bảo Mật Thông Tin Nhạy Cảm (Secret Redaction)**:
   - Ưu tiên nạp Cookie và Telegram Bot Token từ biến môi trường (`FB_ACCOUNT_COOKIE`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`) hoặc tệp bí mật cục bộ `config/secrets.json` (được đưa vào `.gitignore`).
   - Toàn bộ log ra màn hình, tệp log `night_poster.log` và báo cáo lỗi Telegram đều tự động che chắn (redact) Cookie, Token và URL endpoint.

7. **Cơ Chế Anti-Spam Độc Bản**:
   - **Spintax Text**: Tự động xáo trộn từ ngữ `{Xin chào|Hello|Chào các bạn}` tạo nội dung duy nhất cho từng Page.
   - **Biến Thể Ảnh (MD5 Mutation)**: Crop 1-2 pixel và vi chỉnh độ sáng ngẫu nhiên (±2%), thay đổi chữ ký số MD5/SHA256 của ảnh cho từng Page.

8. **Cầu Chì An Toàn, Giãn Cách & Chống Windows Sleep**:
   - Tự ngắt khẩn cấp khi gặp lỗi 3 Page liên tiếp hoặc phiên Facebook hết hạn.
   - Giãn cách 3-6 phút giữa các Page; nghỉ lớn 20-30 phút sau mỗi lô 15 Page.
   - Giữ Windows thức (`prevent_windows_sleep`) và giải phóng hoàn toàn trong khối `finally`.

---

## 📁 Cấu Trúc Thư Mục

```text
facebook_auto_poster/
├── config/
│   ├── pages_and_posts.example.json # Cấu hình mẫu an toàn (không chứa secret)
│   └── pages_and_posts.json         # Cấu hình bài viết thực tế
├── core/
│   ├── __init__.py
│   ├── anti_spam.py                 # Thuật toán Spintax & Image MD5 Mutation
│   ├── config_loader.py             # Nạp cấu hình, schema validator, secret redaction
│   ├── contracts.py                 # Data models & typed contracts (PostingTask, PostingResult)
│   ├── fb_poster.py                 # Tự động hóa giao diện đăng bài Playwright
│   ├── session_manager.py           # Quản lý trình duyệt, Cookie và đổi Profile Page
│   └── state_manager.py             # AtomicStateStore, SingleWorkerLock, crash recovery
├── state/
│   ├── runner_state.json            # Trạng thái runtime thực thi (tự động sinh)
│   └── runner_state.lock            # Khóa tiến trình đơn chống chạy trùng
├── tests/
│   ├── test_reliable_runner.py      # Bộ test toàn diện cho Runner & Validation
│   └── test_tool.py                 # Test Anti-Spam & Session
├── Ke_Hoach_Trien_Khai_Tool_Facebook_50_Pages.pdf
├── main.py                          # CLI Runner chính
├── page_scanner.py                  # Script quét tự động danh sách Fanpage
├── requirements.txt                 # Phụ thuộc thư viện
└── README.md
```

---

## 🚀 Hướng Dẫn Sử Dụng

### 1. Cài đặt môi trường
Yêu cầu Python 3.10+ trở lên:
```bash
pip install -r requirements.txt
playwright install chromium
```

### 2. Thiết lập thông tin bí mật (Credentials)
Có thể thiết lập qua biến môi trường (khuyến nghị):
```bash
# Windows PowerShell:
$env:FB_ACCOUNT_COOKIE = "datr=...; c_user=...; xs=...;"
$env:TELEGRAM_BOT_TOKEN = "123456789:ABC..."
$env:TELEGRAM_CHAT_ID = "987654321"
```
Hoặc tạo tệp `config/secrets.json` (tự động được `.gitignore` bỏ qua):
```json
{
  "account_cookie": "datr=...; c_user=...; xs=...;",
  "telegram_bot_token": "123456789:ABC...",
  "telegram_chat_id": "987654321"
}
```

### 3. Kiểm tra cấu hình & Chạy thử nghiệm (Dry-Run mặc định)
```bash
python main.py
```
Hoặc chỉ định rõ tệp cấu hình và trạng thái:
```bash
python main.py --config config/pages_and_posts.json --dry-run
```
Lệnh sẽ kiểm tra toàn diện dữ liệu đầu vào và in báo cáo mô phỏng mà không thực hiện bất kỳ hành động nào.

### 4. Khởi chạy ca đêm thực tế (Yêu cầu cờ `--execute`)
```bash
python main.py --execute
```
Tùy chỉnh thời gian nghỉ giãn cách:
```bash
python main.py --execute --min-break 4 --max-break 8
```

### 5. Vận hành Trình duyệt & Đăng nhập Thủ công (Manual Login & Profile Setup)

Khi triển khai trên máy thật hoặc khi tài khoản yêu cầu xác thực 2 bước (2FA), checkpoint bảo mật:
1. **Khởi tạo Profile Trình duyệt Thủ công**:
   - Profile người dùng được lưu trữ cục bộ trong thư mục `fb_master_profile/` (được cách ly bởi `.gitignore`).
   - Có thể khởi động trình duyệt có giao diện (`headless=False`) để đăng nhập và giải quyết captcha/2FA thủ công lần đầu:
   ```bash
   python -c "from core.session_manager import FacebookSessionManager; s = FacebookSessionManager('./fb_master_profile'); s.start_browser(headless=False); input('Sau khi đăng nhập xong trên trình duyệt, nhấn Enter tại đây để lưu profile...'); s.close()"
   ```
   - Sau khi hoàn thành, phiên đăng nhập được lưu bền vững trong `fb_master_profile/`. Các lần chạy tự động tiếp theo sẽ tái sử dụng phiên này mà không cần nhập lại mật khẩu.

2. **Cơ chế Xác minh Phiên & Danh tính Người Đăng (Actor Verification)**:
   - **Xác thực Đăng nhập**: Trước khi tiến hành ca chạy, hệ thống kiểm tra URL và DOM (`verify_logged_in`). Nếu phát hiện chuyển hướng sang `/login`, form đăng nhập hoặc từ khóa `checkpoint`, ca đêm lập tức dừng khẩn cấp để bảo vệ tài khoản.
   - **Xác minh Danh tính Page Đăng bài (Actor Identity)**:
     - Việc đổi cookie `i_user` hay chuyển trang tới `facebook.com/{page_id}` **không phải là bằng chứng danh tính**.
     - Hệ thống yêu cầu bằng chứng UI tích cực: kiểm tra huy hiệu persona đang tương tác (`div[aria-label*='Đang tương tác dưới tên']`, `div[aria-label*='Interacting as']`, `div[aria-label*='Acting as']`).
     - **Giới hạn & An toàn (Limitation)**: Nếu huy hiệu persona hiển thị tài khoản cá nhân hoặc không tìm thấy bằng chứng tích cực, công cụ sẽ từ chối bấm nút Đăng (trả về lỗi `ACTOR_UNVERIFIED`) nhằm tuyệt đối ngăn ngừa đăng nhầm bài của Fanpage lên trang cá nhân của người dùng.

3. **Phân định Thất bại Trước Gửi (Pre-Send) & Bất định Sau Bấm (Post-Click)**:
   - **Trước khi bấm (Pre-send)**: Tệp media bị thiếu trên đĩa (`MISSING_MEDIA`), lỗi upload trong hộp thoại (`UPLOAD_FAILED`), danh tính chưa xác minh (`ACTOR_UNVERIFIED`), hoặc nút Đăng bị khóa (`BUTTON_DISABLED`) -> Tác vụ bị đánh dấu `FAILED` bình thường, hoàn toàn an toàn vì chưa có yêu cầu gửi lên mạng.
   - **Sau khi bấm nút Đăng (Post-click)**: Bất kỳ ngoại lệ, rớt mạng hoặc hết thời gian chờ (timeout) nào xảy ra sau khi nút Đăng đã được click đều được phân loại là `POST_CLICK_UNCERTAIN` và chuyển sang trạng thái `UNKNOWN_NEEDS_RECONCILIATION`. Hệ thống **tuyệt đối không tự động chạy lại (không auto-retry)** nhằm chống đăng trùng lặp.

### 6. Chạy kiểm thử tự động (Unit Tests & Local Playwright Fixtures)
Toàn bộ quy trình trình duyệt được kiểm thử thông qua cơ chế Route Interception cục bộ của Playwright (Chromium thực tế, không gửi bất kỳ yêu cầu mạng nào ra Internet):
```bash
pytest tests/ -v
```
Các bộ kiểm thử bao gồm:
- `tests/test_reliable_runner.py`: Kiểm thử schema, tính nguyên tử, chống ghi đè, và khóa đơn tiến trình.
- `tests/test_page_poster_fixtures.py`: Kiểm thử tương tác Chromium thực tế trên các kịch bản: hết hạn phiên đăng nhập, checkpoint, danh tính không khớp, thiếu tệp media, lỗi tải lên, bài viết cũ trên dòng thời gian, và xử lý timeout sau khi bấm Đăng.

