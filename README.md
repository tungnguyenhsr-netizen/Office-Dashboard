# Office-Dashboard

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Web monitoring dashboard for Hermes Agent — a self-hosted AI agent orchestration system. View tasks, workers, cron jobs, agent conversations, files, and system health in real-time.

> **Schema note**: This dashboard uses Hermes' SQLite kanban.db schema and is not a generic task manager. The schema is fixed Hermes format (v1.1). If you want to adapt it for other task queues, feel free to fork — the core UI (tables, modals, kanban) is framework-agnostic.

## Prerequisites

- **Python 3.11+** (with `pip`)
- **Hermes Agent** installed and running (provides `kanban.db`, `cron/jobs.json`, `profiles/*/state.db`)
- **Cross-platform**: works on Windows, macOS, and Linux
- **Flask** (`pip install flask`)
- **psutil** (optional, for system health metrics — `pip install psutil`)

## Quick Start

```powershell
# 1. Clone
git clone https://github.com/tungnguyenhsr-netizen/Office-Dashboard.git
cd Office-Dashboard

# 2. Install dependencies
pip install flask psutil

# 3. Run (default port 8093)
python server.py
```

Open **http://localhost:8093** in your browser.

### Change Port

```powershell
$env:DASHBOARD_PORT = "8094"
python server.py
```

## Architecture

```
server.py          → Single Flask app with inline HTML/CSS/JS (no React, no build tools)
scripts/
  dispatcher.py    → Hermes task dispatcher (poll + spawn workers)
  orchestrator.py  → Automated anomaly detection (future)
  worker_model_config.py → Model fallback management
state/             → Runtime state (gitignored)
plans/             → Architecture documentation
```

### Data Sources (read-only)

| Source | Path | Content |
|--------|------|---------|
| `kanban.db` | `%LOCALAPPDATA%\hermes\kanban.db` | Tasks, task_runs, task_events |
| `cron/jobs.json` | `%LOCALAPPDATA%\hermes\cron\jobs.json` | Cron job schedules |
| `profiles/*/state.db` | `%LOCALAPPDATA%\hermes\profiles\{name}\state.db` | Agent conversations |
| `VAULT_ROOT` | `$env:HERMES_VAULT_DIR` (optional) | Obsidian vault markdown files |

## Vault Setup (Optional)

Dashboard includes a File Viewer tab and file search. If you use an Obsidian vault with Hermes, configure:

```powershell
$env:HERMES_VAULT_DIR = "C:\Users\You\Documents\YourVault"
```

Expected structure:

```
{VAULT_DIR}/
  Efforts/          # .md files directory
    project-1.md
    project-2.md
```

**Without vault**: File tab shows empty. Dashboard works fine. Task output comes from kanban.db.
**Default** (no env var set): Vault features disabled, no crash.

## Features

### System Tab
- **4 stat cards**: Total tasks, completed, stale/running, cron errors
- **Analytics**: Health score, completion rate, stale/running counts (collapsible)
- **Recent activity** table + **pie chart** distribution
- **Task sub-tabs**: Treo (stale/running) / Tất cả (all) / Xong (done)
  - Sort columns, filter by status, checkboxes for bulk kill/delete
- **Auto-refresh**: Configurable interval (Off / 5s / 10s / 30s / 60s)

### Kanban Tab
- Board view by status columns (ready / blocked / running / stale / done / archived)
- Click assignee → task list popup
- Search text filters kanban board live

### Cron Tab
- List all cron jobs with schedule, next run, last run, last error
- **ON/OFF toggle** per cron job (writes to `cron/jobs.json`)
- 30s auto-refresh when tab active

### Outputs Tab
- List 50 latest task outputs with preview
- Click → full task detail modal

### Workers Tab
- Card grid + table view of all Hermes agent profiles
- Shows: PID, status, current task, total tasks, last heartbeat

### Files Tab
- Browse Obsidian vault `.md` files
- **Preview modal**: Markdown rendering with toolbar (rendered/raw toggle, copy)
- Click to view full file content

### Conversations Tab
- List latest 100 agent conversations across all profiles
- Shows: profile, title, model, message count, tokens, cost
- **Chat modal**: Role-based bubbles (user/agent), reasoning collapsible, markdown rendering

### Task Management
- **Create task**: `+` button in header → modal form with title, assignee (autocomplete from workers), description
- **Update status**: Pencil icon next to status badge → inline dropdown (ready/running/blocked/stale/done/error/killed)
- **Update output**: Edit button in output toolbar → inline markdown textarea with save/cancel
- **Task notes**: Notes tab in detail modal → save to `body` field
- **Bulk actions**: Checkbox multi-select → Kill selected / Delete selected

### Task Detail Modal
- **Metadata chips**: Status (editable inline), ID, PID, error count
- **Action bar**: Claim, Enqueue, Complete, Retry, Delete
- **Tabbed content**: Output / Events (timeline) / Runs (timeline) / Notes
- **Output toolbar**: Rendered/Raw toggle, Copy, Edit inline, Fullscreen
- **Delete**: Requires typing `CONFIRM` in popup

### Global Features
- **Dark/Light mode** toggle (persisted via localStorage)
- **Global search** (Ctrl+K): searches tasks, files, workers across all data sources
- **Export**: Download all tasks as CSV or JSON
- **System health**: CPU/RAM/Disk indicators in header → click for detail modal
- **Toast notifications** for all actions

## API Reference

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/dashboard` | Task summary + cron data |
| GET | `/api/analytics` | Health score, completion rate, stats |
| GET | `/api/workers` | All agent profiles with current tasks |
| GET | `/api/files` | List `.md` files from vault |
| GET | `/api/tasks?assignee=` | Tasks by assignee |
| GET | `/api/tasks/all?status=&sort=&order=&limit=` | Paginated task list with filters |
| GET | `/api/task/{id}` | Task detail + events + runs + output |
| GET | `/api/task-outputs` | 50 latest outputs with previews |
| GET | `/api/conversations` | 100 latest agent sessions |
| GET | `/api/conversation/{profile}/{session_id}` | Full conversation messages |
| GET | `/api/search?q=` | Search tasks + files + workers |
| GET | `/api/export/tasks?format=csv\|json` | Download task data |
| GET | `/api/system-health` | CPU, RAM, disk, uptime, Hermes status |
| POST | `/api/tasks` | Create new task |
| POST | `/api/tasks/bulk-kill` | Kill multiple tasks by PID |
| POST | `/api/tasks/bulk-delete` | Delete multiple tasks (permanently) |
| POST | `/api/task/{id}/retry` | Reset task to ready |
| POST | `/api/task/{id}/kill` | Kill task worker process |
| POST | `/api/task/{id}/claim` | Atomically claim task for dispatch |
| POST | `/api/task/{id}/enqueue` | Enqueue task with prompt |
| POST | `/api/task/{id}/complete` | Mark task as done |
| POST | `/api/cron/{name}/toggle` | Toggle cron job on/off |
| PATCH | `/api/task/{id}` | Update task status, assignee, body |
| PATCH | `/api/task/{id}/output` | Update task result/output |
| DELETE | `/api/task/{id}` | Delete task (requires `confirm: "CONFIRM"`) |

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `DASHBOARD_PORT` | `8093` | Server port |
| `FLASK_USER` | *(empty)* | Basic auth username (optional) |
| `FLASK_PASS` | *(empty)* | Basic auth password (optional) |
| `HERMES_VAULT_DIR` | *(empty)* | Obsidian vault path for File Viewer |
| `%LOCALAPPDATA%\hermes\kanban.db` | — | SQLite database path |
| `%LOCALAPPDATA%\hermes\cron\jobs.json` | — | Cron jobs config path |

## Security

- **Basic auth**: Set `FLASK_USER` + `FLASK_PASS` env vars to require login. Without them, dashboard runs without auth (localhost only recommended).
- Dashboard connects to local DB only (no network DB)
- DB is opened with `PRAGMA query_only = ON` for read operations
- Write operations (create, update, delete) use explicit `readonly=False`
- Delete task requires user to type `CONFIRM` in UI
- Bulk kill/delete requires user confirmation
- `.gitignore` excludes `.env`, `*.db`, `state/`, logs

## Development

```powershell
# Branch and test
git checkout -b feature/my-feature
$env:DASHBOARD_PORT = "8094"
python server.py           # test on 8094

# Master (stable) runs on 8093
git checkout master
python server.py           # production on 8093
```

No build tools, no npm, no webpack. Edit `server.py` → refresh browser.

## Related

- **Hermes Agent** — Self-hosted AI agent orchestration system this dashboard monitors
- [Bootstrap 5.3](https://getbootstrap.com/) — UI framework (loaded from CDN)
- [Bootstrap Icons](https://icons.getbootstrap.com/) — Icon library (loaded from CDN)
