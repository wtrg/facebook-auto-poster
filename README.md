# Facebook Auto Poster — Multi-Page Night Scheduler

Công cụ tự động hóa đăng bài viết và hình ảnh lên hàng loạt Fanpage Facebook (hỗ trợ tới 50+ Page) theo ca đêm an toàn với cơ chế chống checkpoint và anti-spam.

## 🌟 Tính Năng Nổi Bật

1. **Quản lý phiên Playwright thông minh (`session_manager.py`)**:
   - Sử dụng Chrome Persistent Context (`user_data_dir`) để duy trì đăng nhập bền vững.
   - Hỗ trợ nạp Cookie định dạng chuẩn Facebook (`datr`, `c_user`, `xs`, ...).
   - Tự động chuyển ngữ cảnh Fanpage (`i_user`) trước khi thao tác đăng bài.

2. **Cơ chế Anti-Spam độc bản (`anti_spam.py`)**:
   - **Spintax Text**: Tự động xáo trộn từ ngữ `{Xin chào|Hello|Chào các bạn}` để mỗi bài viết đăng lên đều có nội dung duy nhất.
   - **Biến thể Ảnh (MD5 Mutation)**: Tự động crop 1-2 pixel và điều chỉnh độ sáng ngẫu nhiên (±2%), thay đổi hash MD5/SHA256 của ảnh cho từng Fanpage để Facebook không nhận diện là ảnh trùng lặp.

3. **Cầu chì an toàn & Nghỉ giải lao (`main.py`)**:
   - **Tự ngắt khẩn cấp**: Nếu gặp lỗi 3 Page liên tiếp hoặc phát hiện Checkpoint/Login logout, hệ thống lập tức dừng ngay để bảo vệ tài khoản mẹ.
   - **Giãn cách ngẫu nhiên (Human-like Delay)**: Nghỉ 3-6 phút giữa mỗi Page, nghỉ giải lao dài 20-30 phút sau mỗi lô 15 Page.
   - **Ngăn Windows Sleep**: Giữ máy tính Windows luôn thức khi chạy qua đêm (`prevent_windows_sleep`).
   - **Báo cáo tức thì qua Telegram**: Gửi thông báo kết quả tổng hợp ca đêm về Telegram Bot.

4. **Trình quét Fanpage tự động (`page_scanner.py`)**:
   - Tự động cuộn và trích xuất danh sách liên kết/ID của 50+ Fanpage bạn đang quản trị ra file JSON `discovered_pages.json`.

---

## 📁 Cấu Trúc Thư Mục

```text
facebook_auto_poster/
├── config/
│   └── pages_and_posts.json    # Cấu hình danh sách Page, nội dung spintax, media và cookie
├── core/
│   ├── __init__.py
│   ├── anti_spam.py            # Thuật toán Spintax & Image MD5 Mutation
│   ├── fb_poster.py            # Logic tự động hóa giao diện đăng bài Playwright
│   └── session_manager.py      # Quản lý trình duyệt, Cookie và chuyển đổi Profile Page
├── Ke_Hoach_Trien_Khai_Tool_Facebook_50_Pages.pdf # Tài liệu kế hoạch chi tiết
├── main.py                     # Entry point chạy ca đêm (NightSchedulerWorker)
├── page_scanner.py             # Script quét tự động danh sách Fanpage
├── requirements.txt            # Thư viện phụ thuộc
└── README.md
```

---

## 🚀 Cài Đặt & Hướng Dẫn Sử Dụng

### 1. Cài đặt môi trường
Yêu cầu Python 3.10+ trở lên:
```bash
pip install -r requirements.txt
playwright install chromium
```

### 2. Cấu hình bài viết & Tài khoản
Mở file `config/pages_and_posts.json`:
- Điền `account_cookie` của nick Facebook cầm quyền quản trị Page.
- (Tùy chọn) Điền `telegram_bot_token` và `telegram_chat_id` để nhận báo cáo.
- Cấu hình danh sách các `tasks` với `page_id`, nội dung Spintax (`content`) và đường dẫn ảnh (`media_paths`).

### 3. Quét danh sách Page (nếu chưa có ID)
```bash
python page_scanner.py
```

### 4. Khởi chạy ca đêm
```bash
python main.py
```
Hệ thống sẽ tự động bật Chrome, xác thực phiên đăng nhập, duyệt qua từng bài viết, biến đổi ảnh/text và xuất bản lên từng Page an toàn.
