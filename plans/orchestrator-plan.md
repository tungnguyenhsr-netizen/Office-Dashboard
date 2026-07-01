# Orchestrator Loop Plan — Hermes Auto Pilot for Monitor / Kanban

Date: 2026-06-24
Owner: default profile
Repo: Office-Dashboard

## Problem
- Monitor đang chỉ lưu trữ dữ liệu, không tự phản ứng
- Task-runs crash hàng loạt nhưng tasks vẫn done → data dirty, mất visibility thật
- Không có trợ giúp tự động để reclaim / retry / notify khi có bất thường

## Objectives
1. Đọc Monitor UI API (port 8093) mỗi N phút
2. Phát hiện bất thường: crashed, blocked, stale, no-output, pid zombie
3. Thực thi hành động khép kín: retry / reclaim / assign / notify
4. Ghi log vào vault (Office-Dashboard/logs/) để audit
5. Có giới hạn an toàn và cooldown để không flood

## Architecture
- Orchestrator = 1 cronjob + 1 Python watcher script
- Script: `Office-Dashboard/scripts/orchestrator.py`
- Cron: chạy mỗi 5 phút (viết DB/log nhanh) + 1 chiến dịch nặng mỗi 1 giờ (retry + notify)
- JSON state: `Office-Dashboard/state/last-action.json` để dedup + cooldown
- Output log: `Office-Dashboard/logs/orchestrator-YYYY-MM-DD.log`

## Data sources
1. `http://localhost:8093/api/dashboard` → summary + crons + stale_running
2. `http://localhost:8093/api/task/<id>` → detail + output preview
3. `%LOCALAPPDATA%\hermes\kanban.db` → raw fallback
4. `C:\Users\YOURNAME\Documents\YourVault\Efforts\Office-Dashboard\logs\` → persists

## Automated actions
| Signal | Condition | Action |
|--------|-----------|--------|
| Stale run | `status=stale` > 60 phút | gửi thông báo + tăng consecutive_failures |
| Blocked | run `blocked` hoặc task `blocked` | đăng comment + chuyển assignee cho ops |
| No output done | task `done` mà output NULL/blank | thêm tag, mở brief lại |
| Priority crash | priority 1-2 + lần thử < 3 | chạy lại job qua dispatcher |
| PID zombie | task `running` nhưng không có run | reclaim task về ready, ghi log |
| Repeated crash | cùng worker + crash > 3 | chuyển nhánh sang manual |

## Endpoints sẽ dùng (đã có trong server.py)
- `POST /api/task/<id>/kill` → dừng chạy treo
- `POST /api/task/<id>/retry` → retry (cần patch để thật)
- Read-only tất cả APIs khác

## Eligibility gate (input quality) — NEW
Trước khi 1 hành động tự động (retry/assign/run) được phép, orchestrator kiểm tra task input gắn với board rule:
1. Thiếu input: không có body hoặc body rỗng, không có attachments, không có expected_output
2. Sai context: tag/board/parent_id không khớp với domain (PLACEHOLDER vs ExampleBrand vs Hatitude)
3. Không đủ hấp dẫn: tiêu đề goals quá chung chung, không có acceptance criteria, không có link ràng buộc vault (related:)

Gate result:
- PASS → cho qua pipeline
- FAIL → gắn tag `needs-info`, notify ops, KHÔNG retry, KHÔNG claim

## Safety / Cooldown
- Mỗi task chỉ được retry tối đa 3 lần/ngày
- Stale tối thiểu 30 phút mới notify
- Chỉ retry priority >= 3 với mật độ cao
- Cooldown có đánh dấu theo profile/board

## Output plan
- Dung lượng: 5-10 hàng mỗi tick
- Mỗi bản ghi ghi: `timestamp`, `task_id`, `action`, `result`, `next_retry_at`
- Reset về ready sau mỗi vòng

## Implementation steps
1. Tạo `scripts/orchestrator.py` đọc API + rules + output
2. Thêm retry thật trong `server.py` (chỉ update local + re-enqueue)
3. Tạo cron mới: `office-orchestrator`
4. Chạy thử bằng `hermes cron run office-orchestrator`
5. Tie với kanban board: board = `office-monitor` hoặc `%` tùy profile
6. Thêm notify bằng Slack webhook hoặc Telegram
