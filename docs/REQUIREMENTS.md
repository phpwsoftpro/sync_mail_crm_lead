# Yêu cầu hệ thống Mail → CRM (sổ yêu cầu của Trung)

Nguồn: workflow spec CRM ticket **#462027 "Workflow xử lí email ticket"** + các yêu cầu Trung chốt trực tiếp
trong quá trình vận hành 08/09 → 15/09/2026. Mọi thay đổi code phải giữ đúng các mục dưới đây.
Playbook vận hành chi tiết: `~/.claude/skills/wsoftpro-mail-crm/SKILL.md`.

## 1. Nhận & phân loại mail (inbound) — `smart_mail_daemon.py`, cron 5 phút
1. **Mọi mail đều được Antigravity Pro (`agy`) đọc FULL nội dung để phân loại.** Luật keyword chỉ dùng khi agy lỗi/timeout. Không dùng Gemini API key.
2. Quét 7 hộp Gmail (Helen / Vanessa / Luna / Robert / Yuna / supportteam / jennifer), cửa sổ `newer_than:2d`, bỏ qua mail từ domain nội bộ.
3. **Một cột rác duy nhất: "Z - Mail Rác" (22).** OOO, auto-ack, bounce, DMARC, newsletter, ứng viên, từ chối rõ ràng… đều vào đây.
4. Mail khách thật, chưa có ticket → tạo ở **New (1)**, `type = opportunity` (để hiện trên Pipeline Kanban).
5. Khách đã có ticket mà rep lại (agy: `reply_client` / `proposition` / `checking_meeting`) → **rescue về Reply Client (3)** từ bất cứ cột nào, bỏ archive, `type = opportunity`.
6. Nhãn mặc định `new_lead` **không bao giờ** được phép đổi cột ticket đang có; sender hệ thống (no-reply/dmarc/hubspot…) **không bao giờ** rescue được ticket.
7. Không hardcode mật khẩu trong script — đọc từ `.env`.

## 2. Kanban / ticket
8. Team chỉ làm việc trên **Pipeline Kanban** → mọi ticket cần thấy phải là `type = opportunity`; rác để `lead`.
9. Cột = **trạng thái + số lần follow-up**: `reply_email` gửi → **Send Email Done (35)**; `follow_up_x1` → **Done Follow Up 1 (9)**; `follow_up_x2` → **Done Follow Up 2 (10)**. Không được đẩy reply vào 9.
10. Ticket chuẩn để rep khách = ticket có `[thread::<gmail thread id>]` trong tên; sender đọc id đó để nối đúng thread.
11. Gộp ticket trùng **chỉ khi Trung duyệt từng khách** (10Pearls: gộp; Solar Start: KHÔNG gộp; VESLOG ~35 bản trùng: chưa quyết).

## 3. Chatter
12. Chatter là nguồn sự thật để hiểu deal → **phải có mail khách**, hiển thị **HTML thật** (không escape), **đúng ngày gốc**.
13. Chatter chỉ gồm **mail khách gửi tới + note "📧 Email sent to…"**. Không đổ chuỗi mail mình gửi đi, không gộp nhiều hội thoại vào một ticket ("nhiều quá không hiểu gì").
14. Ticket cũ (trước 10/09) trống chatter → backfill từ Gmail bằng `backfill_chatter_emails.py` (chỉ mail `from:` khách, tối đa 5–10 mail/ticket).

## 4. Tag nguồn mail
15. **Mỗi ticket đúng 1 tag "Mail <persona>"** = hộp đã **nhận** mail của khách (chủ thread; nếu không thì hộp có mail `from:` khách gần nhất, tìm cả Spam/Trash). Không dùng `to:` (gắn nhầm mọi persona từng outreach).
16. Daemon chỉ gắn tag khi ticket **chưa có** tag nguồn.

## 5. Gửi mail (outbound) — `send_reply_crm.py`
17. Cột **Send Email to Client (7)** phải **tự gửi trong ≤5 phút, đúng thread** (reply vào `[thread::id]` trong tên ticket): launchd `com.syncmail.sendreply` trên Mac 32 chạy `send_reply_crm.py --auto` mỗi 300 s (từ 15/09; thay cho crontab — crontab đã bị xoá trắng ngày 15/09 và **không được** thêm lại job mail vào đó). **Chỉ một sender duy nhất.** Timeout Gmail tạm thời → retry 1 lần trên đúng hộp sở hữu thread; thất bại thật → Unable to Send Email (19) + note. Sender thứ hai từng tồn tại là launchd `com.wsoftpro.sendreply` trên mac8868 (bản cũ 07/08, `STAGE_FOLLOWUP = 9` → đẩy cả reply vào Done Follow Up 1) — đã tắt 15/09; không bao giờ bật lại bản cũ đó.
18. Persona gửi: cache JSON → `user_id` của ticket (pin) → tên/email persona trong draft hoặc mail đầu → Vanessa mặc định; gửi từ hộp có thread. Pin persona bằng `crm_lead.user_id` (Vanessa 25, Robert 34).
19. Ticket ở cột 7 mà **không có nội dung** (vd #431213) → không gửi, cần kéo ra xử lý tay.

## 6. Follow-up
20. **Mail hệ thống gửi > 3 ngày, khách chưa rep → chuyển về Reply Client (3)** để Sales soạn follow-up tiếp — `followup_reminder.py`, launchd `com.syncmail.followup_reminder`, **hằng ngày 08:00**. Không đẩy về "Old Lead cần Follow-up" (34) nữa; Bot Cảnh Sát cũ trong `send_reply_crm.py` đã tắt.
21. Mặc định chỉ áp dụng cho Send Email Done (→ nhắc lần 1) và Done Follow Up 1 (→ nhắc lần 2). **Done Follow Up 2 (82 ticket im, nhiều tháng): Trung chưa quyết** (bỏ qua / nhắc lần 3 nếu ≤30 ngày / kéo hết).
22. Khách rep nhưng ticket kẹt ở cột ngủ (do Odoo fetchmail chỉ post mail, không đổi cột) → **Bot Cứu Hộ** `rescue_replies.py` kéo về Reply Client (chưa lên lịch — chờ duyệt).

## 7. Báo cáo & kênh
23. Báo cáo bot → payroll channel **188** theo template Bot Report (🤖 / ✅ Kết quả / 📊 Chi tiết / 👉 Hành động / 📍 Link). Kênh 252 là chat người, không đổ báo cáo. Admin WSP → 190 với prefix `[admin_wsp]`.

## 8. Quy tắc an toàn (tuyệt đối)
24. Không restart OrbStack/k3s. Không in giá trị biến bí mật (chỉ tên). Không nhập mật khẩu/API key thay người. Không tạo stage "Auto Reply"/"Follow Up x1" khi chưa duyệt. Không bật lại `syncOdooData()` legacy ở Admin WSP. `sed` xong phải `grep` kiểm tra. Sửa script cron xong phải test ngay.
25. Mọi thay đổi hàng loạt trên CRM: **plan → show bảng → apply từng nhóm an toàn**; rescue phải kiểm tra mốc thời gian (mail khách mới hơn lần gửi cuối).
26. Sau mỗi lỗi/fix: cập nhật skill `wsoftpro-mail-crm` + memory ("update skill vào agent").

## 9. Việc còn mở (chờ Trung quyết)
- Done Follow Up 2 im lặng (mục 21).
- Lên lịch Bot Cứu Hộ (mục 22).
- Xác nhận đã thêm `--auto` vào crontab (mục 17).
- VESLOG ~35 ticket trùng; #431213 trống nội dung ở cột 7; #426843 (iciparisxl) ở Explicit Rejection.
- Retire `sync_mail_api.py` (autopilot treo từ 27/08) và gate `full_sync_reconciler.py` (nguồn tạo ticket trùng).
- Workflow v2 (tách trạng thái khỏi `followup_count`) — đề xuất tại artifact "Luồng Ticket Email v2", chờ chốt với Trung.
