# -*- coding: utf-8 -*-
"""SQLite database helpers — connections, task fetchers, cron reader, file index."""

import io, json, os, sqlite3, sys, time
from datetime import datetime, timezone, timedelta

# ── Paths (shared with server.py via module-level vars) ──
DB = os.path.expandvars(r'%LOCALAPPDATA%\hermes\kanban.db')
CRON_JSON = os.path.expandvars(r'%LOCALAPPDATA%\hermes\cron\jobs.json')
HERMES_HOME = os.path.expandvars(r'%LOCALAPPDATA%\hermes')
TZ = timezone(timedelta(hours=7))

# Vault — imported from vault.py at runtime
VAULT_ROOT = ''

# ── File index cache ──
_FILE_INDEX = []
_FILE_INDEX_TIME = 0
_FILE_INDEX_TTL = 30


def _get_file_index():
    global _FILE_INDEX, _FILE_INDEX_TIME
    now = time.time()
    if _FILE_INDEX and now - _FILE_INDEX_TIME < _FILE_INDEX_TTL:
        return _FILE_INDEX
    _FILE_INDEX = []
    efforts_dir = os.path.join(VAULT_ROOT, 'Efforts')
    if os.path.exists(efforts_dir):
        for root, dirs, files in os.walk(efforts_dir):
            for fn in files:
                if fn.endswith('.md'):
                    path = os.path.join(root, fn)
                    rel = os.path.relpath(path, VAULT_ROOT)
                    try:
                        st = os.stat(path)
                        _FILE_INDEX.append({
                            'name': fn, 'path': rel,
                            'modified': st.st_mtime, 'size': st.st_size,
                        })
                    except Exception:
                        pass
    _FILE_INDEX_TIME = now
    return _FILE_INDEX


# ── Database connection ──
def db_conn(readonly=True):
    conn = sqlite3.connect(DB, timeout=5)
    conn.row_factory = sqlite3.Row
    if readonly:
        conn.execute("PRAGMA query_only = ON")
    return conn


DB_EXISTS = os.path.exists(DB)


# ── Task fetchers ──
def fetch_tasks_summary():
    if not DB_EXISTS:
        return {
            'total': 0, 'done_count': 0, 'active_count': 0,
            'stale_count': 0, 'running_workers': 0,
            'board_summary': [], 'stale_running': [],
        }
    conn = db_conn()
    c = conn.cursor()

    overview = c.execute("""
        SELECT
            COUNT(1) as total_tasks,
            SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) as done_count,
            SUM(CASE WHEN status IN ('ready','running','blocked','stale') THEN 1 ELSE 0 END) as active_count,
            SUM(CASE WHEN status='stale' THEN 1 ELSE 0 END) as stale_count
        FROM tasks
    """).fetchone()

    board_summary = c.execute("""
        SELECT status, assignee, COUNT(1) as cnt
        FROM tasks
        GROUP BY status, assignee
        ORDER BY status, assignee
    """).fetchall()

    stale_running = c.execute("""
        SELECT t.id, t.title, t.assignee, COALESCE(r.status, t.status) as status,
               r.worker_pid,
               CASE
                   WHEN r.status = 'stale' THEN
                       printf('%.1fh', (strftime('%s','now') - r.last_heartbeat_at) / 3600.0)
                   WHEN r.status = 'running' THEN
                       printf('%.1fh', (strftime('%s','now') - r.started_at) / 3600.0)
                   ELSE NULL
               END as age_human,
               CASE
                   WHEN r.status IS NULL THEN 'no run'
                   WHEN r.status = 'stale' AND r.last_heartbeat_at IS NULL THEN 'no heartbeat'
                   WHEN r.status = 'stale' AND r.worker_pid IS NULL THEN 'pid dead'
                   WHEN r.status = 'stale' THEN 'timeout'
                   ELSE NULL
               END as reason,
               CASE
                   WHEN r.status = 'stale' AND (r.last_heartbeat_at IS NULL OR r.worker_pid IS NULL) THEN 'dead'
                   WHEN r.status = 'running' THEN 'alive'
                   WHEN r.status IS NULL AND t.status = 'running' THEN 'zombie'
                   ELSE '?'
               END as alive,
               r.last_heartbeat_at, r.started_at, t.last_failure_error
        FROM tasks t
        LEFT JOIN task_runs r ON r.task_id = t.id AND r.status IN ('running','stale')
        WHERE t.status IN ('running','stale')
        ORDER BY age_human DESC
    """).fetchall()

    running_count = c.execute("""
        SELECT COUNT(1) as running_workers
        FROM task_runs
        WHERE status = 'running'
    """).fetchone()

    conn.close()
    stale_list = [dict(r) for r in stale_running]

    return {
        'total': overview['total_tasks'],
        'done_count': overview['done_count'],
        'active_count': overview['active_count'],
        'stale_count': overview['stale_count'],
        'running_workers': running_count['running_workers'],
        'board_summary': [dict(r) for r in board_summary],
        'stale_running': stale_list,
    }


def fetch_task_detail(task_id):
    conn = db_conn()
    c = conn.cursor()
    task = c.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not task:
        conn.close()
        return None
    events = c.execute(
        "SELECT * FROM task_events WHERE task_id = ? ORDER BY created_at DESC LIMIT 50",
        (task_id,)
    ).fetchall()
    runs = c.execute(
        "SELECT * FROM task_runs WHERE task_id = ? ORDER BY started_at DESC LIMIT 10",
        (task_id,)
    ).fetchall()
    conn.close()
    output = fetch_task_output(task_id)
    return {
        'task': dict(task),
        'events': [dict(e) for e in events],
        'runs': [dict(r) for r in runs],
        'output': output,
    }


def fetch_task_output(task_id):
    conn = db_conn()
    c = conn.cursor()
    try:
        row = c.execute(
            "SELECT result, title, assignee FROM tasks WHERE id=?",
            (task_id,)
        ).fetchone()
        if not row:
            return None
        if row['result'] and row['result'].strip():
            return row['result'].strip()
        title = row['title'] or ''
        assignee = row['assignee'] or ''

        run = c.execute(
            "SELECT profile, metadata FROM task_runs "
            "WHERE task_id=? AND metadata LIKE '%worker_session_id%' "
            "ORDER BY started_at DESC LIMIT 1",
            (task_id,)
        ).fetchone()
        if run:
            try:
                meta = json.loads(run['metadata'])
                session_id = (meta or {}).get('worker_session_id')
                if session_id:
                    profile = run['profile'] or assignee
                    state_path = os.path.join(
                        HERMES_HOME, 'profiles', profile, 'state.db'
                    )
                    if os.path.exists(state_path):
                        sconn = sqlite3.connect(state_path)
                        sconn.row_factory = sqlite3.Row
                        sc = sconn.cursor()
                        sc.execute(
                            "SELECT content FROM messages "
                            "WHERE session_id=? AND role='assistant' "
                            "AND content IS NOT NULL AND content != '' "
                            "ORDER BY timestamp DESC LIMIT 1",
                            (session_id,)
                        )
                        msg = sc.fetchone()
                        sconn.close()
                        if msg and msg['content']:
                            return msg['content'][:8000]
            except (json.JSONDecodeError, sqlite3.Error):
                pass

        if title:
            keywords = [w for w in title.lower().split() if len(w) > 3]
            for kw in keywords:
                for root, dirs, files in os.walk(
                    os.path.join(VAULT_ROOT, 'Efforts')
                ):
                    for fn in files:
                        if kw in fn.lower() and fn.endswith('.md'):
                            path = os.path.join(root, fn)
                            try:
                                text = open(
                                    path, encoding='utf-8', errors='ignore'
                                ).read()
                                if text.strip():
                                    return text[:8000]
                            except Exception:
                                pass
        return None
    finally:
        conn.close()


# ── Cron jobs ──
def fetch_crons():
    try:
        with open(CRON_JSON, 'r', encoding='utf-8') as f:
            data = json.load(f)
        print(
            f"[cron] reading fallback: {CRON_JSON} "
            f"({len(data.get('jobs', []))} jobs)"
        )
        return data.get('jobs', [])
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"[cron] fallback failed: {e}")
        return []
