# ARCHITECTURE.md — Office Dashboard

> Monitoring dashboard for Hermes Agent. Single-file Flask app with inline HTML/CSS/JS.

## Data Flow

```
┌─────────────────┐     read-only PRAGMA      ┌──────────────┐
│  kanban.db       │◄─────────────────────────│  server.py    │
│  (SQLite, local) │                           │  (Flask, 8093)│
├─────────────────┤     read-write (POST/PATCH)│               │
│  tasks           │◄─────────────────────────►│  HTML_TEMPLATE│
│  task_runs       │                           │  (inline JS)  │
│  task_events     │     json.load / dump      │               │
│  task_comments   │                           └───────┬───────┘
│  task_links      │                                   │
│  kanban_notify   │     os.walk / state.db            │
└─────────────────┘                                   │
                    ┌──────────────┐                   │
                    │ cron/jobs.json│◄─────────────────┤
                    └──────────────┘                   │
                    ┌──────────────┐                   │
                    │ profiles/*/   │◄─────────────────┤
                    │ state.db      │                   │
                    └──────────────┘                   │
                    ┌──────────────┐                   │
                    │ VAULT_ROOT/   │◄─────────────────┤
                    │ Efforts/*.md  │                   │
                    └──────────────┘                   │
                                                       ▼
                                              ┌─────────────────┐
                                              │  Browser         │
                                              │  (fetch + render)│
                                              └─────────────────┘
```

**Read vs Write**: DB opened `PRAGMA query_only=ON` for reads. Write operations use explicit `readonly=False`. Task output retrieved via 3-tier priority: `tasks.result` → `state.db` messages → vault `.md` keyword match.

**Auth**: Flask sessions via `admin`/`admin` defaults. Override with `FLASK_USER`/`FLASK_PASS` env vars. Login page served via `LOGIN_PAGE` template before `HTML_TEMPLATE`.

## Schema Reference

*Dumped from live `%LOCALAPPDATA%\hermes\kanban.db` on the host machine.*

### `tasks` (35 columns — primary task record)

| Column | Type | Key | Notes |
|--------|------|-----|-------|
| `id` | TEXT | PK | UUID |
| `title` | TEXT | | Task name |
| `body` | TEXT | | Description / notes |
| `assignee` | TEXT | | Worker profile name |
| `status` | TEXT | | ready/running/blocked/stale/done/error/killed |
| `priority` | INTEGER | | |
| `workspace_kind` | TEXT | | scratch/worktree/dir/board |
| `workspace_path` | TEXT | | Filesystem path |
| `result` | TEXT | | Final task output |
| `consecutive_failures` | INTEGER | | Retry counter |
| `last_failure_error` | TEXT | | Last crash message |
| `worker_pid` | INTEGER | | OS process ID |
| `current_run_id` | INTEGER | | FK→task_runs.id |
| `session_id` | TEXT | | Hermes agent session |
| `created_at/started_at/completed_at` | INTEGER | | Unix timestamps |
| `block_kind / block_recurrences` | TEXT/INT | | Recurring task support |

*Also:* `created_by`, `claim_lock`, `claim_expires`, `tenant`, `idempotency_key`, `max_runtime_seconds`, `last_heartbeat_at`, `workflow_template_id`, `current_step_key`, `skills`, `model_override`, `max_retries`, `goal_mode`, `goal_max_turns`, `audit_retry_count`, `project_id`

### `task_runs` (16 columns — execution history)

| Column | Type | Key | Notes |
|--------|------|-----|-------|
| `id` | INTEGER | PK | Auto-increment |
| `task_id` | TEXT | FK | FK→tasks.id |
| `profile` | TEXT | | Worker profile |
| `status` | TEXT | | running/stale/killed/done |
| `worker_pid` | INTEGER | | OS process ID |
| `started_at/ended_at` | INTEGER | | Unix timestamps |
| `last_heartbeat_at` | INTEGER | | Liveness pulse |
| `outcome/summary/error` | TEXT | | Completion data |
| `metadata` | TEXT | | JSON with worker_session_id |

### `task_events` (6 columns — event log)

| Column | Type | Key | Notes |
|--------|------|-----|-------|
| `id` | INTEGER | PK | Auto-increment |
| `task_id` | TEXT | FK | FK→tasks.id |
| `run_id` | INTEGER | | FK→task_runs.id |
| `kind` | TEXT | | created/claimed/enqueue/running/done/retry/error |
| `payload` | TEXT | | JSON |
| `created_at` | INTEGER | | Unix timestamp |

### Other tables

| Table | Purpose |
|-------|---------|
| `task_comments` | id, task_id, author, body, created_at |
| `task_links` | parent_id ↔ child_id relationship |
| `task_attachments` | File metadata linked to tasks |
| `kanban_notify_subs` | Notification subscriptions per task/chat |

### Per-profile `state.db` (under `%LOCALAPPDATA%\hermes\profiles\{name}\`)

| Table | Key columns |
|-------|-------------|
| `sessions` | id, title, model, started_at, message_count, estimated_cost_usd |
| `messages` | id, session_id (FK), role (user/assistant/tool), content, timestamp, reasoning, tool_calls |
| `state_meta` | key-value store |

## Route Map

| Method | Endpoint | Read/Write | Description |
|--------|----------|:---:|-------------|
| GET | `/` | — | Dashboard HTML (or login page) |
| GET | `/api/dashboard` | R | Task summary + cron data |
| GET | `/api/analytics` | R | Health score, completion rate |
| GET | `/api/tasks?assignee=` | R | Tasks by assignee |
| GET | `/api/tasks/all?status=&sort=&order=&limit=` | R | Paginated task list |
| GET | `/api/task/<id>` | R | Task detail + events + runs + output |
| GET | `/api/task-outputs` | R | 50 latest outputs |
| GET | `/api/workers` | R | Active agents & profiles |
| GET | `/api/files` | R | Vault .md files |
| GET | `/api/search?q=` | R | Full-text across tasks/files/workers |
| GET | `/api/conversations` | R | Agent session list (all profiles) |
| GET | `/api/conversation/<p>/<s>` | R | Full conversation messages |
| GET | `/api/system-health` | R | CPU/RAM/Disk/Hermes status |
| GET | `/api/config` | R | Vault config |
| GET | `/api/export/tasks?format=` | R | CSV/JSON download |
| POST | `/api/tasks` | **W** | Create task (workspace_kind=scratch) |
| POST | `/api/tasks/bulk-kill` | **W** | Kill N tasks by PID |
| POST | `/api/tasks/bulk-delete` | **W** | Delete N tasks permanently |
| POST | `/api/task/<id>/retry` | **W** | Reset to ready, clear failures |
| POST | `/api/task/<id>/kill` | **W** | Kill worker PID |
| POST | `/api/task/<id>/claim` | **W** | Atomically claim for dispatch |
| POST | `/api/task/<id>/enqueue` | **W** | Enqueue with prompt |
| POST | `/api/task/<id>/complete` | **W** | Mark done, save result |
| POST | `/api/cron/<name>/toggle` | **W** | Enable/disable cron job |
| POST | `/api/config/vault` | **W** | Save vault directory |
| POST | `/login` | — | Auth: set session |
| PATCH | `/api/task/<id>` | **W** | Update status/assignee/body |
| PATCH | `/api/task/<id>/output` | **W** | Update task result |
| DELETE | `/api/task/<id>` | **W** | Delete task (confirm=CONFIRM required) |
| GET | `/logout` | — | Auth: clear session |

## Extension Points

For contributors adapting this dashboard to other agent systems:

1. **DB Adapter** — All queries go through `db_conn()` (line ~40). Replace the SQL and `PRAGMA` calls to point at a different SQLite schema or a different DB engine entirely. The route handlers only expect `jsonify()`-able dicts back.

2. **Kill Implementation** — `_kill_by_pid()` (line ~305) is the only platform-specific function. Already handles `taskkill` (Windows) and `os.kill(SIGTERM)` (Unix). Extend for container-based systems via a `DASHBOARD_KILL_CMD` env var.

3. **Output Source** — `fetch_task_output()` (line ~185) uses 3-tier priority. Add a 4th source (S3, API callback, etc.) by inserting before the `return None` fallback.

4. **Auth Backend** — Currently uses hardcoded `FLASK_USER`/`FLASK_PASS`. The `check_auth()` before_request hook (line ~22) can be replaced with OAuth, LDAP, or a reverse-proxy header check.

5. **i18n** — 100+ keys in the `LANG` dict (line ~2750). Add `LANG.{lang_code}` with the same key structure for a new language. The `_i()` function resolves at runtime from `currentLang` (localStorage).

6. **New Tabs** — Each tab follows the pattern: `<li>` nav button → `tab-content` pane → JS `loadXxx()` function. Search for `pane-conversations` as the template to copy for a new feature tab.
