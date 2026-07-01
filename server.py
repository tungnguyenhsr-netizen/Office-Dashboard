# -*- coding: utf-8 -*-
import io, json, os, sqlite3, subprocess, sys, time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from flask import Flask, jsonify, render_template_string, request

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

app = Flask(__name__)

DB = os.path.expandvars(r'%LOCALAPPDATA%\hermes\kanban.db')
CRON_JSON = os.path.expandvars(r'%LOCALAPPDATA%\hermes\cron\jobs.json')
BOARD = "%"
TZ = timezone(timedelta(hours=7))

def db_conn(readonly=True):
    conn = sqlite3.connect(DB, timeout=5)
    conn.row_factory = sqlite3.Row
    if readonly:
        conn.execute("PRAGMA query_only = ON")
    return conn

def fetch_tasks_summary():
    conn = db_conn()
    c = conn.cursor()

    # Đếm trực tiếp từ tasks — không JOIN, không WHERE workspace_path
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

    # LEFT JOIN để lấy tasks đang running/stale kèm thông tin run
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

HERMES_HOME = os.path.expandvars(r'%LOCALAPPDATA%\hermes')
VAULT_ROOT = r'C:\Users\YOURNAME\Documents\YourVault'

def fetch_task_output(task_id):
    conn = db_conn()
    c = conn.cursor()
    try:
        # Priority 1: tasks.result field
        row = c.execute("SELECT result, title, assignee FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not row:
            return None
        if row['result'] and row['result'].strip():
            return row['result'].strip()
        title = row['title'] or ''
        assignee = row['assignee'] or ''

        # Priority 2: worker_session_id from task_runs metadata -> session messages
        run = c.execute(
            "SELECT profile, metadata FROM task_runs WHERE task_id=? AND metadata LIKE '%worker_session_id%' ORDER BY started_at DESC LIMIT 1",
            (task_id,)
        ).fetchone()
        if run:
            try:
                meta = json.loads(run['metadata'])
                session_id = (meta or {}).get('worker_session_id')
                if session_id:
                    profile = run['profile'] or assignee
                    state_path = os.path.join(HERMES_HOME, 'profiles', profile, 'state.db')
                    if os.path.exists(state_path):
                        sconn = sqlite3.connect(state_path)
                        sconn.row_factory = sqlite3.Row
                        sc = sconn.cursor()
                        sc.execute("SELECT content FROM messages WHERE session_id=? AND role='assistant' AND content IS NOT NULL AND content != '' ORDER BY timestamp DESC LIMIT 1", (session_id,))
                        msg = sc.fetchone()
                        sconn.close()
                        if msg and msg['content']:
                            return msg['content'][:8000]
            except (json.JSONDecodeError, sqlite3.Error):
                pass

        # Priority 3: vault file keyword match
        if title:
            keywords = [w for w in title.lower().split() if len(w) > 3]
            for kw in keywords:
                for root, dirs, files in os.walk(os.path.join(VAULT_ROOT, 'Efforts')):
                    for fn in files:
                        if kw in fn.lower() and fn.endswith('.md'):
                            path = os.path.join(root, fn)
                            try:
                                text = open(path, encoding='utf-8', errors='ignore').read()
                                if text.strip():
                                    return text[:8000]
                            except Exception:
                                pass
        return None
    finally:
        conn.close()

def fetch_crons():
    fallback_path = CRON_JSON
    try:
        with open(fallback_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        print(f"[cron] reading fallback: {fallback_path} ({len(data.get('jobs',[]))} jobs)")
        return data.get('jobs', [])
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"[cron] fallback failed: {e}")
        return []

@app.route('/api/dashboard')
def api_dashboard():
    tasks = fetch_tasks_summary()
    crons = fetch_crons()
    cron_errors = sum(1 for c in crons if c.get('last_status') == 'error')
    return jsonify({
        'tasks_summary': tasks,
        'crons': crons,
        'cron_errors': cron_errors,
    })

@app.route('/api/tasks')
def api_tasks_by_assignee():
    assignee = request.args.get('assignee', '')
    conn = db_conn()
    c = conn.cursor()
    rows = c.execute("""
        SELECT id, title, status, assignee, consecutive_failures, worker_pid, last_failure_error
        FROM tasks WHERE assignee = ?
        ORDER BY status, title
    """, (assignee,)).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route('/api/task/<task_id>')
def api_task_detail(task_id):
    data = fetch_task_detail(task_id)
    if not data:
        return jsonify({'error': 'Not found'}), 404
    return jsonify(data)

@app.route('/api/task-outputs')
def api_task_outputs():
    conn = db_conn()
    c = conn.cursor()
    rows = c.execute("""
        SELECT id, title, assignee, status, completed_at, started_at
        FROM tasks
        ORDER BY COALESCE(completed_at, started_at, 0) DESC
        LIMIT 50
    """).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        output = fetch_task_output(r['id'])
        if output:
            d['output_preview'] = output[:200]
        else:
            d['output_preview'] = None
        result.append(d)
    conn.close()
    return jsonify(result)

def _kill_by_pid(pid):
    if not pid or pid == 0:
        return False, 'không có PID'
    try:
        r = subprocess.run(['taskkill', '/F', '/PID', str(pid)], capture_output=True, text=True, timeout=10)
        if r.returncode == 0:
            return True, 'đã kill'
        return False, r.stderr.strip() or r.stdout.strip() or 'taskkill thất bại'
    except subprocess.TimeoutExpired:
        return False, 'timeout'
    except Exception as e:
        return False, str(e)

def _update_task_killed(task_id):
    try:
        conn = db_conn(readonly=False)
        conn.execute("UPDATE task_runs SET status = 'killed' WHERE task_id = ? AND status IN ('running','stale')", (task_id,))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"[kill] DB update failed for {task_id}: {e}")
        return False

@app.route('/api/task/<task_id>/retry', methods=['POST'])
def api_task_retry(task_id):
    return jsonify({'ok': True, 'message': f'Retry {task_id} (no-op, read-only DB)'})

@app.route('/api/task/<task_id>/kill', methods=['POST'])
def api_task_kill(task_id):
    conn = db_conn()
    c = conn.cursor()
    row = c.execute("""
        SELECT worker_pid FROM task_runs
        WHERE task_id = ? AND status = 'running'
        ORDER BY started_at DESC LIMIT 1
    """, (task_id,)).fetchone()
    conn.close()
    pid = int(row['worker_pid']) if row and row['worker_pid'] else None
    ok, msg = _kill_by_pid(pid)
    db_ok = _update_task_killed(task_id)
    return jsonify({'ok': ok, 'task_id': task_id, 'pid': pid, 'message': f'Kill {task_id}: {msg}', 'db_updated': db_ok})

@app.route('/api/task/<task_id>/claim', methods=['POST'])
def api_task_claim(task_id):
    """Atomically claim a task for a profile/worker and emit a task_event+task_run row."""
    conn = db_conn(readonly=False)
    c = conn.cursor()
    try:
        row = c.execute("SELECT id, status, claim_lock, claim_expires FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not row:
            conn.close()
            return jsonify({'ok': False, 'task_id': task_id, 'message': 'task not found'}), 404
        if row['status'] != 'ready':
            conn.close()
            return jsonify({'ok': False, 'task_id': task_id, 'message': f'task not ready: {row["status"]}'}), 409

        now = datetime.now(TZ).timestamp()
        if row['claim_lock'] and row['claim_expires'] and now < row['claim_expires']:
            conn.close()
            return jsonify({'ok': False, 'task_id': task_id, 'message': 'already claimed', 'claim_lock': row['claim_lock'], 'claim_expires': row['claim_expires']}), 409

        claim_lock = 'orchestrator'
        claim_expires = int(now + 120)
        c.execute("UPDATE tasks SET status='running', claim_lock=?, claim_expires=? WHERE id=?", (claim_lock, claim_expires, task_id))
        run_id = c.execute("INSERT INTO task_runs (task_id, profile, status, started_at, worker_pid) VALUES (?,?,?,?,?)", (task_id, 'orchestrator', 'running', int(now), 0)).lastrowid
        c.execute("INSERT INTO task_events (task_id, run_id, kind, payload, created_at) VALUES (?,?,?,?,?)", (task_id, run_id, 'claim', json.dumps({'by':'orchestrator'}), int(now)))
        conn.commit()
        conn.close()
        return jsonify({'ok': True, 'task_id': task_id, 'status': 'running', 'run_id': run_id, 'claim_lock': claim_lock})
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass
        return jsonify({'ok': False, 'task_id': task_id, 'message': str(e)}), 500

@app.route('/api/task/<task_id>/complete', methods=['POST'])
def api_task_complete(task_id):
    """Persist the successful output and move task to done."""
    payload = request.get_json(silent=True) or {}
    result = (payload.get('result') or '').strip()
    profile = (payload.get('profile') or 'orchestrator').strip()
    conn = db_conn(readonly=False)
    c = conn.cursor()
    now = int(datetime.now(TZ).timestamp())
    try:
        run = c.execute("SELECT id, status FROM task_runs WHERE task_id=? AND status='running' ORDER BY started_at DESC LIMIT 1", (task_id,)).fetchone()
        if run:
            c.execute("UPDATE task_runs SET status='done', ended_at=?, outcome=?, summary=? WHERE id=?", (now, 'completed', 'completed', run['id']))
            run_id = run['id']
        else:
            run_id = c.execute("INSERT INTO task_runs (task_id, profile, status, started_at, ended_at, outcome, summary) VALUES (?,?,?,?,?,?,?)", (task_id, profile, 'done', now, now, 'completed', 'completed')).lastrowid
        c.execute("UPDATE tasks SET status='done', result=?, completed_at=? WHERE id=?", (result, now, task_id))
        c.execute("INSERT INTO task_events (task_id, run_id, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (task_id, run_id, 'complete', json.dumps({'result_len': len(result), 'profile': profile}), now))
        conn.commit()
        conn.close()
        return jsonify({'ok': True, 'task_id': task_id, 'run_id': run_id, 'result_len': len(result)})
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass
        return jsonify({'ok': False, 'task_id': task_id, 'message': str(e)}), 500

@app.route('/api/task/<task_id>/enqueue', methods=['POST'])
def api_task_enqueue(task_id):
    """Claim a ready task and assign it to the task's existing assignee profile, create a task_run entry, return a prompt/context stub."""
    payload = request.get_json(silent=True) or {}
    profile = (payload.get('profile') or '').strip()
    prompt = (payload.get('prompt') or '').strip()
    model_override = payload.get('model_override')
    conn = db_conn(readonly=False)
    c = conn.cursor()
    try:
        row = c.execute("SELECT id, title, status, assignee, claim_lock, claim_expires FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not row:
            conn.close()
            return jsonify({'ok': False, 'task_id': task_id, 'message': 'task not found'}), 404
        if row['status'] != 'ready':
            conn.close()
            return jsonify({'ok': False, 'task_id': task_id, 'message': f'task not ready: {row["status"]}'}), 409
        now = datetime.now(TZ).timestamp()
        if row['claim_lock'] and row['claim_expires'] and now < row['claim_expires']:
            conn.close()
            return jsonify({'ok': False, 'task_id': task_id, 'message': 'already claimed', 'claim_lock': row['claim_lock'], 'claim_expires': row['claim_expires']}), 409

        assignee = (row['assignee'] or profile or 'ops').strip()
        claim_lock = assignee or 'orchestrator'
        claim_expires = int(now + 120)
        c.execute("UPDATE tasks SET status='running', claim_lock=?, claim_expires=? WHERE id=?", (claim_lock, claim_expires, task_id))
        # Store model_override on task if provided (flat model name string for -m flag)
        if model_override is not None:
            c.execute("UPDATE tasks SET model_override=? WHERE id=?", (str(model_override), task_id))
        run_id = c.execute("INSERT INTO task_runs (task_id, profile, status, started_at, worker_pid) VALUES (?,?,?,?,?)", (task_id, assignee, 'running', int(now), 0)).lastrowid
        c.execute("INSERT INTO task_events (task_id, run_id, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (task_id, run_id, 'enqueue', json.dumps({'assignee': assignee, 'prompt_len': len(prompt), 'model_override': model_override}), int(now)))
        conn.commit()
        conn.close()
        return jsonify({'ok': True, 'task_id': task_id, 'status': 'running', 'run_id': run_id, 'assignee': assignee, 'prompt': prompt or row['title'], 'model_override': model_override})
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass
        return jsonify({'ok': False, 'task_id': task_id, 'message': str(e)}), 500

@app.route('/api/tasks/bulk-kill', methods=['POST'])
def api_bulk_kill():
    ids = request.json.get('ids', []) if request.is_json else []
    conn = db_conn()
    c = conn.cursor()
    results = []
    for tid in ids:
        row = c.execute("""
            SELECT worker_pid FROM task_runs
            WHERE task_id = ? AND status = 'running'
            ORDER BY started_at DESC LIMIT 1
        """, (tid,)).fetchone()
        pid = int(row['worker_pid']) if row and row['worker_pid'] else None
        ok, msg = _kill_by_pid(pid)
        db_ok = _update_task_killed(tid)
        results.append({'id': tid, 'ok': ok, 'pid': pid, 'msg': msg, 'db_updated': db_ok})
    conn.close()
    ok_count = sum(1 for r in results if r['ok'])
    return jsonify({'ok': True, 'count': len(ids), 'killed': ok_count, 'results': results, 'message': f'Kill {ok_count}/{len(ids)} tasks thành công'})

@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="vi" data-bs-theme="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Monitoring</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
<link href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.min.css" rel="stylesheet">
<style>
:root {
  /* === Design Tokens === */
  /* Background & Surface */
  --bg: #08080f;
  --surface: #111127;
  --surface2: #18183a;
  --surface3: #20204a;
  --bg2: #0c0c1a;

  /* Borders */
  --border: #252550;
  --border2: #35356a;
  --border-light: #1e1e40;

  /* Text */
  --text: #d0d0ec;
  --text2: #7878aa;
  --text3: #5858aa;

  /* Accent */
  --accent: #818cf8;
  --accent2: #6366f1;
  --accent-subtle: rgba(129, 140, 248, 0.08);
  --accent-glow: rgba(129, 140, 248, 0.12);

  /* Semantic Colors */
  --green: #4ade80;
  --green-subtle: rgba(74, 222, 128, 0.1);
  --yellow: #fbbf24;
  --yellow-subtle: rgba(251, 191, 36, 0.1);
  --red: #f87171;
  --red-subtle: rgba(248, 113, 113, 0.1);
  --orange: #fb923c;
  --orange-subtle: rgba(251, 146, 60, 0.1);
  --blue: #38bdf8;
  --blue-subtle: rgba(56, 189, 248, 0.1);
  --purple: #a78bfa;
  --purple-subtle: rgba(167, 139, 250, 0.1);

  /* Shadows */
  --shadow-xs: 0 1px 2px rgba(0, 0, 0, 0.3);
  --shadow-sm: 0 2px 6px rgba(0, 0, 0, 0.35);
  --shadow-md: 0 4px 14px rgba(0, 0, 0, 0.4);
  --shadow-lg: 0 8px 28px rgba(0, 0, 0, 0.45);
  --shadow-glow-accent: 0 0 24px rgba(129, 140, 248, 0.12);
  --shadow-glow-green: 0 0 24px rgba(74, 222, 128, 0.1);
  --shadow-glow-red: 0 0 24px rgba(248, 113, 113, 0.1);

  /* Radii */
  --radius: 10px;
  --radius-sm: 6px;
  --radius-lg: 14px;

  /* Transitions */
  --transition-fast: 0.12s ease;
  --transition-base: 0.2s ease;
  --transition-slow: 0.3s cubic-bezier(0.4, 0, 0.2, 1);

  /* Typography */
  --font-sans: 'Inter', 'Segoe UI', system-ui, -apple-system, sans-serif;
  --font-mono: 'Cascadia Code', 'Fira Code', 'Consolas', monospace;

  /* Z-index */
  --z-dropdown: 100;
  --z-modal: 1050;
  --z-toast: 9999;

  /* Spacing scale */
  --space-1: .25rem;
  --space-2: .5rem;
  --space-3: .75rem;
  --space-4: 1rem;
  --space-5: 1.5rem;
  --space-6: 2rem;
  --space-7: 3rem;

  /* Typography scale */
  --text-xs: .68rem;
  --text-sm: .76rem;
  --text-base: .82rem;
  --text-md: .9rem;
  --text-lg: 1rem;
  --text-xl: 1.25rem;
  --text-2xl: 1.6rem;

  /* Avatar palette */
  --av-1: #818cf8;
  --av-2: #4ade80;
  --av-3: #fbbf24;
  --av-4: #f87171;
  --av-5: #38bdf8;
  --av-6: #a78bfa;
  --av-7: #fb923c;
  --av-8: #2dd4bf;

  /* Bootstrap overrides */
  --bs-body-bg: var(--bg);
  --bs-body-color: var(--text);
  --bs-border-color: var(--border);
  --bs-primary: var(--accent);
  --bs-primary-rgb: 129, 140, 248;
}

/* === Light Mode === */
[data-bs-theme="light"] {
  --bg: #f5f5fa;
  --surface: #ffffff;
  --surface2: #eeeef5;
  --surface3: #e2e2ec;
  --bg2: #f0f0f6;

  --border: #d4d4e0;
  --border2: #b8b8cc;
  --border-light: #e8e8f0;

  --text: #1a1a2e;
  --text2: #6b6b8a;
  --text3: #9a9ab0;

  --accent: #6366f1;
  --accent2: #4f46e5;
  --accent-subtle: rgba(99, 102, 241, 0.07);
  --accent-glow: rgba(99, 102, 241, 0.1);

  --green-subtle: rgba(34, 197, 94, 0.08);
  --yellow-subtle: rgba(234, 179, 8, 0.08);
  --red-subtle: rgba(239, 68, 68, 0.08);
  --orange-subtle: rgba(249, 115, 22, 0.08);
  --blue-subtle: rgba(14, 165, 233, 0.08);
  --purple-subtle: rgba(168, 85, 247, 0.08);

  --shadow-xs: 0 1px 2px rgba(0, 0, 0, 0.06);
  --shadow-sm: 0 2px 6px rgba(0, 0, 0, 0.08);
  --shadow-md: 0 4px 14px rgba(0, 0, 0, 0.1);
  --shadow-lg: 0 8px 28px rgba(0, 0, 0, 0.12);
  --shadow-glow-accent: 0 0 24px rgba(99, 102, 241, 0.15);
  --shadow-glow-green: 0 0 24px rgba(34, 197, 94, 0.12);
  --shadow-glow-red: 0 0 24px rgba(239, 68, 68, 0.12);

  --bs-body-bg: var(--bg);
  --bs-body-color: var(--text);
  --bs-border-color: var(--border);
  --bs-primary: var(--accent);
  --bs-primary-rgb: 99, 102, 241;
}

[data-bs-theme="light"] .app-header {
  background: rgba(255, 255, 255, 0.88);
  border-bottom-color: var(--border);
}

[data-bs-theme="light"] ::selection {
  color: #fff;
}

[data-bs-theme="light"] .stale-row {
  background: var(--red-subtle);
}

[data-bs-theme="light"] .modal-backdrop {
  background: rgba(0, 0, 0, .35);
}

[data-bs-theme="light"] .output-block pre {
  background: rgba(0, 0, 0, 0.05);
}

[data-bs-theme="light"] .output-raw {
  background: var(--surface2);
}

[data-bs-theme="light"] pre {
  background: rgba(0, 0, 0, 0.04);
}

* { box-sizing: border-box; }

body {
  font-size: .82rem;
  background: var(--bg);
  color: var(--text);
  font-family: var(--font-sans);
  line-height: 1.6;
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
}

::selection { background: var(--accent); color: #fff; }

::-webkit-scrollbar { width: 5px; height: 5px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: var(--border2); border-radius: 3px; border: 1px solid transparent; }
::-webkit-scrollbar-thumb:hover { background: var(--text3); }

/* === App Layout === */
.app { display: flex; flex-direction: column; min-height: 100vh; }

.app-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: .85rem 2rem;
  border-bottom: 1px solid var(--border);
  background: rgba(17, 17, 39, 0.85);
  backdrop-filter: blur(16px);
  -webkit-backdrop-filter: blur(16px);
  position: sticky;
  top: 0;
  z-index: 50;
}

.app-header h5 {
  font-weight: 700;
  font-size: 1rem;
  letter-spacing: -.3px;
  background: linear-gradient(135deg, var(--text), var(--accent));
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
  background-clip: text;
}

.app-main { flex: 1; padding: 1.5rem 2rem; max-width: 1440px; width: 100%; margin: 0 auto; }

/* === Navigation Tabs (Astryx-inspired) === */
.nav-tabs {
  border-bottom: 1px solid var(--border);
  gap: 0;
}

.nav-tabs .nav-link {
  color: var(--text2);
  border: none;
  padding: .6rem 1rem;
  font-size: .78rem;
  font-weight: 500;
  border-radius: 0;
  margin-bottom: -1px;
  border-bottom: 2px solid transparent;
  transition: var(--transition-base);
  position: relative;
}

.nav-tabs .nav-link::after {
  content: '';
  position: absolute;
  bottom: -1px;
  left: 50%;
  width: 0;
  height: 2px;
  background: var(--accent);
  transition: var(--transition-base);
  transform: translateX(-50%);
  border-radius: 1px;
}

.nav-tabs .nav-link:hover {
  color: var(--text);
  background: var(--accent-subtle);
}

.nav-tabs .nav-link.active {
  color: var(--accent);
  background: transparent;
}

.nav-tabs .nav-link.active::after {
  width: 60%;
}

.nav-tabs .nav-link i { margin-right: 5px; }

/* === Cards (Astryx-inspired) === */
.card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-xs);
  transition: var(--transition-base);
}

.card:hover {
  box-shadow: var(--shadow-sm);
  border-color: var(--border2);
}

.card-body { padding: 1.25rem; }

.card-header {
  background: transparent;
  border-bottom: 1px solid var(--border-light);
  padding: .65rem 1rem;
  font-weight: 600;
  font-size: .72rem;
  color: var(--text2);
  text-transform: uppercase;
  letter-spacing: .6px;
}

/* === Stat Cards (Astryx-inspired metric tiles) === */
.stat-wrap {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: .9rem;
  margin-bottom: 1.25rem;
}

.stat-card {
  position: relative;
  padding: 1.1rem 1.1rem .9rem;
  border-radius: var(--radius-lg);
  border: 1px solid var(--border);
  background: linear-gradient(160deg, var(--surface) 0%, var(--bg2) 100%);
  overflow: hidden;
  transition: var(--transition-base);
  cursor: default;
  box-shadow: var(--shadow-xs);
  animation: fadeUp 0.4s ease backwards;
}

.stat-wrap .stat-card:nth-child(1) { animation-delay: 0.05s; }
.stat-wrap .stat-card:nth-child(2) { animation-delay: 0.1s; }
.stat-wrap .stat-card:nth-child(3) { animation-delay: 0.15s; }
.stat-wrap .stat-card:nth-child(4) { animation-delay: 0.2s; }

@keyframes fadeUp {
  from { opacity: 0; transform: translateY(10px); }
  to { opacity: 1; transform: translateY(0); }
}

.stat-card:hover {
  border-color: var(--border2);
  transform: translateY(-3px);
  box-shadow: var(--shadow-md);
}

.stat-card .stat-glow {
  position: absolute;
  top: -50%;
  right: -25%;
  width: 140px;
  height: 140px;
  border-radius: 50%;
  opacity: .08;
  pointer-events: none;
  filter: blur(12px);
  transition: var(--transition-slow);
}

.stat-card:hover .stat-glow {
  opacity: .14;
  transform: scale(1.25);
}

.stat-card .stat-row {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  position: relative;
  z-index: 1;
}

.stat-card .stat-value {
  font-size: var(--text-2xl);
  font-weight: 800;
  line-height: 1.1;
  letter-spacing: -1px;
}

.stat-card .stat-label {
  font-size: var(--text-xs);
  color: var(--text2);
  text-transform: uppercase;
  letter-spacing: .5px;
  margin-top: 4px;
  font-weight: 500;
}

.stat-card .stat-sub {
  font-size: .65rem;
  color: var(--text3);
  margin-top: 2px;
}

.stat-card .stat-icon {
  font-size: 1.5rem;
  opacity: .2;
  transition: var(--transition-base);
  width: 36px;
  height: 36px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: var(--radius-sm);
  background: rgba(255,255,255,0.03);
}

.stat-card:hover .stat-icon {
  opacity: .5;
  transform: scale(1.08);
  background: rgba(255,255,255,0.06);
}

/* === Tables (Astryx-inspired) === */
.table-wrap {
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  overflow: hidden;
  box-shadow: var(--shadow-xs);
}

.table {
  margin: 0;
  color: var(--text);
  font-size: .8rem;
}

.table thead {
  background: var(--surface2);
  position: sticky;
  top: 0;
  z-index: 2;
  box-shadow: 0 1px 0 var(--border);
}

.table th {
  border: none;
  color: var(--text2);
  font-weight: 600;
  font-size: var(--text-xs);
  text-transform: uppercase;
  letter-spacing: .5px;
  padding: .6rem .75rem;
  white-space: nowrap;
}

.table td {
  border-top: 1px solid var(--border-light);
  padding: .55rem .75rem;
  vertical-align: middle;
}

.table tbody tr {
  transition: background var(--transition-fast);
}

.table tbody tr:hover {
  background: var(--accent-subtle);
}

.table tbody tr:last-child td { border-bottom: none; }

.row-idx {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 20px;
  height: 20px;
  border-radius: 5px;
  background: var(--surface3);
  color: var(--text3);
  font-size: .62rem;
  font-weight: 600;
  padding: 0 5px;
}

.stale-row {
  border-left: 3px solid var(--red);
  background: var(--red-subtle);
}

.stale-row:hover {
  background: var(--red-subtle) !important;
}

/* === Badges (Astryx-inspired) === */
.badge {
  font-weight: 500;
  font-size: .68rem;
  padding: .25em .6em;
  border-radius: 50px;
  letter-spacing: .2px;
  line-height: 1.4;
}

/* === Section Title === */
.sec-title {
  font-size: .72rem;
  color: var(--text2);
  text-transform: uppercase;
  letter-spacing: .7px;
  font-weight: 600;
  margin: 1rem 0 .5rem;
  display: flex;
  align-items: center;
  gap: 6px;
}

.sec-title i { font-size: .8rem; }

/* === Search Input (Astryx-inspired) === */
.search-wrap { position: relative; }
.search-wrap i { position: absolute; left: 10px; top: 50%; transform: translateY(-50%); color: var(--text3); font-size: .85rem; pointer-events: none; transition: var(--transition-base); }

.search-box {
  background: var(--surface2);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: .4rem 2.2rem .4rem 1.85rem;
  color: var(--text);
  font-size: var(--text-sm);
  width: 220px;
  transition: var(--transition-base);
}

.search-box::placeholder {
  color: var(--text3);
  font-size: .74rem;
}

.search-box:focus {
  outline: none;
  border-color: var(--accent);
  width: 280px;
  box-shadow: var(--shadow-glow-accent);
}

.search-wrap:focus-within i {
  color: var(--accent);
}

.search-wrap .search-box { padding-left: 1.85rem; }

.kbd {
  position: absolute;
  right: 8px;
  top: 50%;
  transform: translateY(-50%);
  background: var(--surface3);
  border: 1px solid var(--border2);
  border-radius: 4px;
  padding: 1px 5px;
  font-size: .6rem;
  color: var(--text3);
  font-family: var(--font-sans);
  pointer-events: none;
  line-height: 1.4;
}

/* === Toast (Astryx-inspired) === */
.toast-container {
  position: fixed;
  bottom: 1.25rem;
  right: 1.25rem;
  z-index: var(--z-toast);
  display: flex;
  flex-direction: column;
  gap: .5rem;
  max-width: 360px;
}

.toast-msg {
  background: rgba(24, 24, 58, 0.92);
  border: 1px solid var(--border2);
  color: var(--text);
  border-radius: var(--radius-sm);
  padding: .65rem .9rem;
  font-size: var(--text-sm);
  display: flex;
  align-items: center;
  gap: 8px;
  animation: toastIn var(--transition-slow);
  box-shadow: var(--shadow-lg);
  backdrop-filter: blur(12px);
  -webkit-backdrop-filter: blur(12px);
}

@keyframes toastIn {
  from { transform: translateY(20px) scale(0.95); opacity: 0; }
  to { transform: translateY(0) scale(1); opacity: 1; }
}

@keyframes slideIn {
  from { transform: translateX(100%); opacity: 0; }
  to { transform: translateX(0); opacity: 1; }
}

/* === Refresh Dot === */
.refresh-dot {
  display: inline-block;
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--green);
  box-shadow: var(--shadow-glow-green);
}

.refresh-dot.loading {
  background: var(--yellow);
  animation: pulse 1s infinite;
  box-shadow: var(--shadow-glow-red);
}

@keyframes pulse {
  0%, 100% { opacity: 1; transform: scale(1); }
  50% { opacity: .4; transform: scale(.85); }
}

/* === Empty State === */
.empty-state {
  color: var(--text3);
  text-align: center;
  padding: 1.75rem 1.5rem;
  font-size: var(--text-sm);
}

.empty-state .empty-icon {
  display: block;
  font-size: 1.6rem;
  opacity: .4;
  margin-bottom: .5rem;
}

/* === Buttons (Astryx-inspired) === */
.btn-sm {
  font-size: .7rem;
  border-radius: var(--radius-sm);
  padding: .22rem .55rem;
  transition: var(--transition-base);
}

.btn-sm:active {
  transform: scale(.95);
}

.btn-outline-secondary {
  border-color: var(--border2);
  color: var(--text2);
}

.btn-outline-secondary:hover {
  background: var(--accent-subtle);
  border-color: var(--accent2);
  color: var(--accent);
}

.btn-outline-warning {
  border-color: var(--yellow);
  color: var(--yellow);
}

.btn-outline-warning:hover {
  background: var(--yellow-subtle);
  color: var(--yellow);
}

.btn-outline-danger {
  border-color: var(--red);
  color: var(--red);
}

.btn-outline-danger:hover {
  background: var(--red-subtle);
  color: var(--red);
}

.btn:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
  box-shadow: none;
}

/* === Modals (Astryx-inspired) === */
.modal-content {
  background: var(--surface);
  color: var(--text);
  border: 1px solid var(--border2);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-lg);
}

.modal-fullscreen .modal-content { border-radius: 0; }

.modal-header {
  border-color: var(--border-light);
  padding: 1.1rem 1.5rem;
  position: sticky;
  top: 0;
  background: var(--surface);
  z-index: 3;
  border-radius: var(--radius-lg) var(--radius-lg) 0 0;
}

.modal-body { padding: 1.5rem; }
.modal-fullscreen .modal-body { padding: 1.75rem 2.25rem; }

.modal-backdrop {
  background: rgba(0, 0, 0, .65);
  backdrop-filter: blur(3px);
  -webkit-backdrop-filter: blur(3px);
}

.btn-close {
  filter: brightness(0.7);
  transition: var(--transition-base);
}

.btn-close:hover {
  filter: brightness(1);
}

/* === Modal Tabs === */
.modal-tabs {
  display: flex;
  gap: 0;
  border-bottom: 1px solid var(--border-light);
  margin-bottom: 1.25rem;
  padding: 0 .5rem;
}

.modal-tab {
  background: none;
  border: none;
  color: var(--text2);
  padding: .55rem 1rem;
  font-size: var(--text-sm);
  font-weight: 500;
  cursor: pointer;
  border-bottom: 2px solid transparent;
  margin-bottom: -1px;
  transition: var(--transition-base);
  position: relative;
  display: flex;
  align-items: center;
  gap: 6px;
}

.modal-tab:hover {
  color: var(--text);
  background: var(--accent-subtle);
}

.modal-tab.active {
  color: var(--accent);
  border-bottom-color: var(--accent);
}

.modal-tab .tab-count {
  font-size: .62rem;
  background: var(--surface3);
  border-radius: 10px;
  padding: 1px 7px;
  color: var(--text2);
  font-weight: 600;
}

.modal-tab.active .tab-count {
  background: var(--accent-subtle);
  color: var(--accent);
}

.modal-tab-pane { display: none; }
.modal-tab-pane.active { display: block; animation: tabFade .18s ease; }

/* === Output Toolbar === */
.output-toolbar {
  display: flex;
  align-items: center;
  gap: .35rem;
  margin-bottom: .6rem;
  padding: .4rem .6rem;
  background: var(--surface2);
  border-radius: var(--radius-sm);
  border: 1px solid var(--border-light);
}

.output-toolbar .toolbar-sep {
  width: 1px;
  height: 18px;
  background: var(--border2);
  margin: 0 .2rem;
}

.output-toolbar .toolbar-btn {
  background: transparent;
  border: 1px solid transparent;
  color: var(--text2);
  padding: .22rem .5rem;
  border-radius: var(--radius-sm);
  font-size: .65rem;
  cursor: pointer;
  transition: var(--transition-fast);
  display: flex;
  align-items: center;
  gap: 3px;
  white-space: nowrap;
}

.output-toolbar .toolbar-btn:hover {
  background: var(--accent-subtle);
  color: var(--accent);
  border-color: var(--border2);
}

.output-toolbar .toolbar-btn.active {
  background: var(--accent-subtle);
  color: var(--accent);
  border-color: var(--accent2);
}

.output-toolbar .toolbar-btn i {
  font-size: .75rem;
}

/* Copy feedback */
.toolbar-btn.copied {
  color: var(--green) !important;
  border-color: var(--green) !important;
  background: var(--green-subtle) !important;
}

/* === Timeline === */
.timeline {
  position: relative;
  padding-left: 24px;
}

.timeline::before {
  content: '';
  position: absolute;
  left: 7px;
  top: 4px;
  bottom: 4px;
  width: 1px;
  background: var(--border2);
}

.timeline-item {
  position: relative;
  padding-bottom: .65rem;
  font-size: var(--text-sm);
}

.timeline-item:last-child { padding-bottom: 0; }

.timeline-item::before {
  content: '';
  position: absolute;
  left: -17px;
  top: 5px;
  width: 7px;
  height: 7px;
  border-radius: 50%;
  border: 2px solid var(--border2);
  background: var(--surface);
  z-index: 1;
}

.timeline-item.timeline-claim::before { border-color: var(--blue); background: var(--blue); }
.timeline-item.timeline-enqueue::before { border-color: var(--accent); background: var(--accent); }
.timeline-item.timeline-complete::before { border-color: var(--green); background: var(--green); }
.timeline-item.timeline-error::before { border-color: var(--red); background: var(--red); }

.timeline-time {
  font-size: .65rem;
  color: var(--text3);
  display: block;
}

.timeline-content {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 1px;
}

/* === Metadata chips === */
.meta-chips {
  display: flex;
  flex-wrap: wrap;
  gap: .4rem;
  padding: .4rem .5rem;
  background: var(--bg2);
  border-radius: var(--radius-sm);
  border: 1px solid var(--border-light);
  margin-bottom: .75rem;
}

.meta-chip {
  font-size: .65rem;
  color: var(--text2);
  display: inline-flex;
  align-items: center;
  gap: 3px;
  white-space: nowrap;
}

.meta-chip strong {
  color: var(--text);
  font-weight: 600;
}

.meta-chip-sep {
  width: 1px;
  height: 12px;
  background: var(--border2);
}

/* === Collapsible error === */
.error-collapse {
  border: 1px solid var(--red);
  border-radius: var(--radius-sm);
  margin-bottom: .75rem;
  overflow: hidden;
}

.error-collapse-header {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: .5rem .65rem;
  background: var(--red-subtle);
  cursor: pointer;
  font-size: var(--text-xs);
  color: var(--red);
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: .4px;
  user-select: none;
  transition: var(--transition-fast);
}

.error-collapse-header:hover {
  background: rgba(248, 113, 113, 0.15);
}

.error-collapse-header::after {
  content: '\F282';
  font-family: 'bootstrap-icons';
  margin-left: auto;
  font-size: .65rem;
  transition: var(--transition-fast);
}

.error-collapse.open .error-collapse-header::after {
  transform: rotate(180deg);
}

.error-collapse-body {
  padding: .5rem .65rem;
  font-size: .72rem;
  font-family: var(--font-mono);
  color: var(--text);
  background: var(--bg);
  border-top: 1px solid var(--red);
  max-height: 200px;
  overflow-y: auto;
  white-space: pre-wrap;
  word-break: break-word;
  display: none;
}

.error-collapse.open .error-collapse-body {
  display: block;
}

/* === Output Block === */
.output-block {
  background: var(--bg2);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  font-size: .8rem;
  max-height: 500px;
  overflow-y: auto;
  white-space: normal;
  word-break: break-word;
  color: var(--text);
  margin-top: 4px;
  padding: .5rem .75rem;
}

.modal-fullscreen .output-block { max-height: calc(100vh - 220px); font-size: .85rem; }
.modal-fullscreen .output-raw { max-height: calc(100vh - 220px); }

.output-block pre {
  margin: 4px 0;
  padding: 6px 8px;
  background: rgba(0, 0, 0, .4);
  border-radius: 6px;
  font-size: .72rem;
  overflow-x: auto;
  border: 1px solid var(--border-light);
}

.output-block code {
  background: var(--surface3);
  border-radius: 3px;
  padding: 1px 4px;
  font-size: .72rem;
}

.output-block table { font-size: .72rem; border-collapse: collapse; width: 100%; }
.output-block th, .output-block td { border: 1px solid var(--border); padding: 3px 6px; text-align: left; }
.output-block th { background: var(--surface3); font-weight: 600; }
.output-block hr { border: none; border-top: 1px solid var(--border); margin: 8px 0; }
.output-block ul, .output-block ol { margin: 4px 0; padding-left: 18px; }
.output-block li { margin: 1px 0; }
.output-block p { margin: 6px 0; }
.output-block a { color: var(--accent); text-decoration: underline; }
.output-block a:hover { color: var(--accent2); text-decoration: none; }

.output-raw {
  background: var(--bg2);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: .75rem;
  font-size: .76rem;
  max-height: 500px;
  overflow-y: auto;
  white-space: pre-wrap;
  word-break: break-word;
  font-family: var(--font-mono);
  color: var(--text2);
  margin-top: 4px;
}

/* === Modal XL === */
.modal-xl .modal-dialog { max-width: 1400px; transition: all .25s ease; }
.modal-fullscreen .modal-dialog { transition: all .25s ease; }
.modal-sidebar { border-left: 1px solid var(--border); padding-left: 1rem; }
.modal-sidebar .sec-title { margin-top: 1rem; }
.modal-sidebar .sec-title:first-child { margin-top: 0; }

/* === Pre === */
pre {
  background: rgba(0, 0, 0, .4);
  padding: .75rem;
  border-radius: var(--radius-sm);
  max-height: 180px;
  overflow-y: auto;
  font-size: .76rem;
  color: var(--text);
  border: 1px solid var(--border);
  font-family: var(--font-mono);
}

/* === Kanban Board (Astryx-inspired) === */
.kanban-scroll { overflow-x: auto; padding-bottom: 8px; }

.kanban-row {
  display: flex;
  gap: 1rem;
  min-width: max-content;
  margin: 0 auto;
}

.kanban-col {
  flex: 0 0 270px;
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  background: var(--surface);
  box-shadow: var(--shadow-xs);
}

.kanban-col-head {
  padding: .7rem .85rem;
  border-bottom: 2px solid var(--border);
  font-weight: 600;
  font-size: var(--text-xs);
  text-transform: uppercase;
  letter-spacing: .5px;
  display: flex;
  justify-content: space-between;
  align-items: center;
  border-radius: var(--radius-lg) var(--radius-lg) 0 0;
}

.kanban-col-body { padding: .6rem .65rem; }

.kanban-item {
  display: flex;
  align-items: center;
  gap: .55rem;
  padding: .55rem .65rem;
  border-radius: var(--radius-sm);
  border: 1px solid var(--border-light);
  margin-bottom: 7px;
  cursor: pointer;
  transition: var(--transition-base);
  font-size: .73rem;
  background: var(--bg2);
}

.kanban-item:hover {
  background: var(--surface2);
  border-color: var(--border2);
  transform: translateX(3px);
  box-shadow: var(--shadow-sm);
}

.kanban-item:last-child { margin-bottom: 0; }

.kanban-item-text {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.kanban-item-count {
  font-weight: 700;
  font-size: .72rem;
  flex-shrink: 0;
}

/* === Avatar (Astryx-inspired) === */
.avatar {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border-radius: 50%;
  font-weight: 600;
  font-size: .6rem;
  color: #fff;
  flex-shrink: 0;
  text-transform: uppercase;
  letter-spacing: .3px;
  user-select: none;
}

.avatar-sm { width: 22px; height: 22px; font-size: .58rem; }
.avatar-md { width: 28px; height: 28px; font-size: .68rem; }

/* === Status Dot === */
.status-dot {
  display: inline-block;
  width: 7px;
  height: 7px;
  border-radius: 50%;
  flex-shrink: 0;
}

/* === Badge with dot === */
.badge-dot {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-weight: 500;
  font-size: var(--text-xs);
  padding: .2em .55em .2em .45em;
  border-radius: 50px;
  letter-spacing: .2px;
  line-height: 1.4;
}

.badge-dot .status-dot {
  width: 6px;
  height: 6px;
}

/* === Tooltip overrides === */
.tooltip-inner {
  background: var(--surface3);
  color: var(--text);
  border-radius: var(--radius-sm);
  font-size: .68rem;
  padding: .3rem .55rem;
  box-shadow: var(--shadow-md);
}

.tooltip .tooltip-arrow::before {
  border-top-color: var(--surface3);
}

/* === Skeleton loading === */
.skeleton {
  background: linear-gradient(90deg, var(--surface2) 25%, var(--surface3) 50%, var(--surface2) 75%);
  background-size: 200% 100%;
  animation: shimmer 1.4s infinite;
  border-radius: var(--radius-sm);
}

@keyframes shimmer {
  0% { background-position: 200% 0; }
  100% { background-position: -200% 0; }
}

/* === Icon button === */
.icon-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 30px;
  height: 30px;
  border-radius: var(--radius-sm);
  border: 1px solid var(--border2);
  background: var(--surface2);
  color: var(--text2);
  cursor: pointer;
  transition: var(--transition-base);
}

.icon-btn:hover {
  background: var(--accent-subtle);
  border-color: var(--accent2);
  color: var(--accent);
}

.icon-btn:active {
  transform: scale(.92);
}

/* === Code chip (task ID) === */
code.task-id {
  cursor: pointer;
  color: var(--accent);
  background: var(--accent-subtle);
  border: 1px solid var(--border-light);
  border-radius: 4px;
  padding: 1px 5px;
  font-size: .68rem;
  font-family: var(--font-mono);
  transition: var(--transition-fast);
}

code.task-id:hover {
  background: var(--accent-glow);
  border-color: var(--accent);
}

/* === Assignee inline === */
.assignee {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  color: var(--text2);
  font-size: .7rem;
}

/* === Focus Visible === */
:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}

/* === Animations === */
.modal.fade .modal-dialog {
  transition: transform var(--transition-slow), opacity var(--transition-base);
}

.tab-pane {
  animation: tabFade 0.25s ease;
}

@keyframes tabFade {
  from { opacity: 0; transform: translateY(4px); }
  to { opacity: 1; transform: translateY(0); }
}

/* === Checkbox styling === */
input[type="checkbox"] {
  accent-color: var(--accent);
  cursor: pointer;
  width: 14px;
  height: 14px;
}

/* === Link reset === */
a.task-link {
  color: var(--accent);
  text-decoration: none;
  transition: var(--transition-fast);
}

a.task-link:hover {
  color: var(--accent2);
  text-decoration: underline;
}

/* === Pie chart === */
#pieChart {
  position: relative;
  box-shadow: 0 0 0 3px var(--surface), 0 0 0 4px var(--border-light);
}

/* === Responsive === */
@media (max-width: 1200px) { .stat-wrap { grid-template-columns: repeat(4,1fr); } }
@media (max-width: 992px) {
  .stat-wrap { grid-template-columns: repeat(2, 1fr); }
  .app-main { padding: 1rem 1.25rem; }
  .app-header { padding: .75rem 1.25rem; }
}
@media (max-width: 768px) {
  .stat-wrap { grid-template-columns: repeat(2, 1fr); }
  .search-box { width: 160px; }
  .search-box:focus { width: 200px; }
}
@media (max-width: 576px) {
  .stat-wrap { grid-template-columns: 1fr; gap: .5rem; }
  .search-box { width: 120px; }
  .search-box:focus { width: 160px; }
  .app-header { flex-wrap: wrap; gap: .5rem; padding: .6rem 1rem; }
  .toast-container { left: 1rem; right: 1rem; max-width: none; }
}
</style>
</head>
<body>
<div class="toast-container" id="toastContainer"></div>

<div class="app">
  <div class="app-header">
    <div class="d-flex align-items-center gap-2">
      <span style="width:20px;height:20px;border-radius:6px;background:linear-gradient(135deg,var(--accent),var(--accent2));display:flex;align-items:center;justify-content:center;color:#fff;font-size:.7rem;font-weight:700">M</span>
      <h5 class="m-0">Monitor</h5>
      <span class="text-secondary fw-light" style="font-size:.7rem" id="lastUpdate"></span>
    </div>
    <div class="d-flex align-items-center gap-2">
      <div class="search-wrap">
        <i class="bi bi-search"></i>
        <input type="text" class="search-box" id="searchInput" placeholder="Tìm task...">
        <span class="kbd" id="searchKbd">Ctrl K</span>
      </div>
      <button class="icon-btn" onclick="loadDashboard()" title="Refresh" data-bs-toggle="tooltip" data-bs-placement="bottom"><i class="bi bi-arrow-clockwise"></i></button>
      <button class="icon-btn" id="themeToggle" onclick="toggleTheme()" title="Chế độ sáng/tối" data-bs-toggle="tooltip" data-bs-placement="bottom"><i class="bi bi-moon-stars"></i></button>
    </div>
  </div>

  <div class="app-main">
    <ul class="nav nav-tabs" id="mainTabs">
      <li class="nav-item"><button class="nav-link active" id="tab-system" data-bs-toggle="tab" data-bs-target="#pane-system"><i class="bi bi-cpu"></i>Hệ thống</button></li>
      <li class="nav-item"><button class="nav-link" id="tab-kanban" data-bs-toggle="tab" data-bs-target="#pane-kanban"><i class="bi bi-columns-gap"></i>Kanban</button></li>
      <li class="nav-item"><button class="nav-link" id="tab-cron" data-bs-toggle="tab" data-bs-target="#pane-cron"><i class="bi bi-alarm"></i>Cron <span class="badge bg-secondary ms-1" id="cronBadge">0</span></button></li>
      <li class="nav-item"><button class="nav-link" id="tab-outputs" data-bs-toggle="tab" data-bs-target="#pane-outputs"><i class="bi bi-file-text"></i>Outputs</button></li>
    </ul>

    <div class="tab-content mt-3">
      <!-- System -->
      <div class="tab-pane fade show active" id="pane-system">
        <div class="stat-wrap" id="statCards"></div>
        <div class="row g-2 mb-3">
          <div class="col-lg-7">
            <div class="card h-100"><div class="card-header py-2 px-3"><i class="bi bi-activity me-1"></i>Hoạt động gần đây</div><div class="card-body p-0"><div class="table-wrap" style="border:none;border-radius:0"><table class="table"><thead><tr><th style="width:36px">#</th><th>Task</th><th style="width:70px">Trạng thái</th><th style="width:80px">Thời gian</th></tr></thead><tbody id="recentTable"></tbody></table></div></div></div>
          </div>
          <div class="col-lg-5">
            <div class="card h-100"><div class="card-header py-2 px-3"><i class="bi bi-pie-chart me-1"></i>Phân bố</div><div class="card-body py-2 px-3 d-flex align-items-center gap-3" id="distribContent">
              <div id="pieChart" style="width:100px;height:100px;border-radius:50%;flex-shrink:0"></div>
              <div id="pieLegend" style="flex:1;font-size:.72rem"></div>
            </div></div>
          </div>
        </div>
        <div class="d-flex justify-content-between align-items-center mb-2">
          <div class="sec-title m-0"><i class="bi bi-exclamation-triangle"></i>Tác vụ treo <span class="badge bg-danger ms-1" id="staleCountBadge">0</span></div>
          <div class="d-flex gap-2">
            <button class="btn btn-sm btn-outline-warning d-none" id="killSelectedBtn" onclick="killSelected()"><i class="bi bi-x-lg me-1"></i>Kill đã chọn (<span id="selectedCount">0</span>)</button>
            <button class="btn btn-sm btn-outline-danger" onclick="killAllDead()"><i class="bi bi-trash3 me-1"></i>Kill all</button>
          </div>
        </div>
        <div class="table-wrap"><table class="table"><thead><tr><th style="width:28px"><input type="checkbox" id="selectAll" onchange="toggleSelectAll()"></th><th>ID</th><th>Tiêu đề</th><th>Người phụ trách</th><th style="width:50px">PID</th><th style="width:55px">Age</th><th style="width:80px">Lý do</th><th style="width:110px"></th></tr></thead><tbody id="staleTable"></tbody></table></div>
      </div>

      <!-- Kanban -->
      <div class="tab-pane fade" id="pane-kanban">
        <div class="d-flex justify-content-between align-items-center mb-2">
          <div class="sec-title m-0"><i class="bi bi-columns-gap"></i>Kanban theo trạng thái</div>
        </div>
        <div class="kanban-scroll"><div class="kanban-row" id="kanbanBoard"></div></div>
      </div>

      <!-- Cron -->
      <div class="tab-pane fade" id="pane-cron">
        <div class="d-flex justify-content-between align-items-center mb-2">
          <div class="sec-title m-0"><i class="bi bi-alarm"></i>Lịch trình Cron <span class="badge bg-secondary ms-1" id="cronCountBadge">0</span></div>
          <span><span class="refresh-dot" id="cronRefreshDot"></span><small class="text-secondary" style="font-size:.7rem" id="cronRefreshLabel">30s auto</small></span>
        </div>
        <div class="table-wrap"><table class="table"><thead><tr><th>Tên</th><th>Lịch</th><th>Lần chạy tới</th><th>Lần cuối</th><th>Trạng thái</th><th>Lỗi</th></tr></thead><tbody id="cronTable"></tbody></table></div>
      </div>

      <!-- Outputs -->
      <div class="tab-pane fade" id="pane-outputs">
        <div class="d-flex justify-content-between align-items-center mb-2">
          <div class="sec-title m-0"><i class="bi bi-file-text"></i>Kết quả tác vụ <span class="badge bg-secondary ms-1" id="outputCountBadge">0</span></div>
          <span><span class="refresh-dot" id="outputRefreshDot"></span><small class="text-secondary" style="font-size:.7rem" id="outputRefreshLabel"></small></span>
        </div>
        <div class="table-wrap"><table class="table"><thead><tr><th style="width:36px">STT</th><th>Tiêu đề</th><th>Người phụ trách</th><th style="width:70px">Trạng thái</th><th style="width:120px">Thời gian</th></tr></thead><tbody id="outputTable"></tbody></table></div>
      </div>
    </div>
  </div>
</div>

<div class="modal fade" id="taskModal" tabindex="-1" data-bs-keyboard="false"><div class="modal-dialog modal-xl modal-dialog-centered modal-dialog-scrollable"><div class="modal-content">
  <div class="modal-header"><h5 class="modal-title" id="modalTitle"></h5><button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal"></button></div>
  <div class="modal-body" id="modalBody"></div>
</div></div></div>

<div class="modal fade" id="tasksModal" tabindex="-1"><div class="modal-dialog modal-lg modal-dialog-scrollable"><div class="modal-content">
  <div class="modal-header"><h5 class="modal-title" id="tasksModalTitle"></h5><button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal"></button></div>
  <div class="modal-body p-0"><div class="table-wrap m-0" style="border:none;border-radius:0"><table class="table"><thead><tr><th>ID</th><th>Tiêu đề</th><th>Trạng thái</th><th>PID</th><th>Lỗi</th></tr></thead><tbody id="tasksModalBody"></tbody></table></div></div>
</div></div></div>

<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script>
<script>
const API = '/api/dashboard';
let cronTimer = null;

const S_LABEL = {ready:'Sẵn sàng',running:'Đang chạy',stale:'Treo',blocked:'Chặn',done:'Xong',error:'Lỗi',ok:'OK',completed:'Hoàn tất',gave_up:'Bỏ',spawn_failed:'Lỗi KT',killed:'Đã kill'};
const S_COLOR = {ready:'#4ade80',running:'#fbbf24',stale:'#f87171',blocked:'#6b7280',done:'#6b7280',error:'#f87171',ok:'#4ade80',completed:'#38bdf8',gave_up:'#a78bfa',spawn_failed:'#f87171',killed:'#6b7280'};

const AVATAR_COLORS = ['var(--av-1)','var(--av-2)','var(--av-3)','var(--av-4)','var(--av-5)','var(--av-6)','var(--av-7)','var(--av-8)'];

function avatarColor(name) {
  if (!name) return AVATAR_COLORS[0];
  let hash = 0;
  for (let i = 0; i < name.length; i++) hash = name.charCodeAt(i) + ((hash << 5) - hash);
  return AVATAR_COLORS[Math.abs(hash) % AVATAR_COLORS.length];
}

function avatar(name, size) {
  size = size || 'sm';
  if (!name || name === '—') return `<span class="avatar avatar-${size}" style="background:var(--surface3);color:var(--text3)">?</span>`;
  const initial = name.charAt(0).toUpperCase();
  return `<span class="avatar avatar-${size}" style="background:${avatarColor(name)}">${initial}</span>`;
}

function assigneeCell(name) {
  if (!name || name === '—') return '<span style="color:var(--text3)">—</span>';
  return `<span class="assignee">${avatar(name,'sm')}${h(name)}</span>`;
}

function h(t, v) { return (t === null || t === undefined) ? (v || '—') : t; }

function badge(status) {
  const c = S_COLOR[status] || '#6b7280';
  return `<span class="badge-dot" style="background:${c}1a;color:${c};border:1px solid ${c}33"><span class="status-dot" style="background:${c}"></span>${S_LABEL[status]||status}</span>`;
}

function fmtTime(ts) {
  if (!ts) return '—';
  try { const ms = typeof ts === 'number' && ts < 1e12 ? ts * 1000 : ts; const d = new Date(ms); return isNaN(d.getTime()) ? ts : d.toLocaleString('vi-VN',{hour:'2-digit',minute:'2-digit',day:'2-digit',month:'2-digit'}); }
  catch(e) { return ts; }
}

function fmtRelative(ts) {
  if (!ts) return '—';
  try {
    const ms = typeof ts === 'number' && ts < 1e12 ? ts * 1000 : ts;
    const diff = Date.now() - ms;
    if (isNaN(diff)) return fmtTime(ts);
    const sec = Math.floor(diff/1000);
    if (sec < 60) return sec + ' giây trước';
    const min = Math.floor(sec/60);
    if (min < 60) return min + ' phút trước';
    const hr = Math.floor(min/60);
    if (hr < 24) return hr + ' giờ trước';
    const day = Math.floor(hr/24);
    if (day < 7) return day + ' ngày trước';
    return fmtTime(ts);
  } catch(e) { return fmtTime(ts); }
}

const EMPTY_ICONS = {
  noTasks: '<i class="bi bi-check-circle empty-icon"></i>',
  noData: '<i class="bi bi-inbox empty-icon"></i>',
  noCron: '<i class="bi bi-alarm empty-icon"></i>',
  noOutput: '<i class="bi bi-file-text empty-icon"></i>',
  noEvents: '<i class="bi bi-clock-history empty-icon"></i>',
  error: '<i class="bi bi-exclamation-triangle empty-icon"></i>',
};

function renderMd(t) {
  if (!t) return '';
  let s = String(t);
  s = s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  s = s.replace(/```([\s\S]*?)```/g, '<pre><code>$1</code></pre>');
  s = s.replace(/`([^`]+)`/g, '<code>$1</code>');
  s = s.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  s = s.replace(/\*([^*]+)\*/g, '<em>$1</em>');
  s = s.replace(/__([^_]+)__/g, '<u>$1</u>');
  s = s.replace(/~~([^~]+)~~/g, '<s>$1</s>');
  s = s.replace(/^#####?\s+(.*)$/gm, '<h6>$1</h6>');
  s = s.replace(/^####\s+(.*)$/gm, '<h6>$1</h6>');
  s = s.replace(/^###\s+(.*)$/gm, '<h6>$1</h6>');
  s = s.replace(/^##\s+(.*)$/gm, '<h6>$1</h6>');
  s = s.replace(/^#\s+(.*)$/gm, '<h6>$1</h6>');
  s = s.replace(/^-{3,}$/gm, '<hr>');
  s = s.replace(/^[\s]*[-*]\s+(.*)$/gm, '<li>$1</li>');
  s = s.replace(/(<li[\s>][\s\S]*?<\/li>)/g, '<ul>$1</ul>');
  s = s.replace(/<\/ul>\s*<ul>/g, '');
  s = s.replace(/^[\s]*\d+\.\s+(.*)$/gm, '<li>$1</li>');
  s = s.replace(/<\/ul>\s*<ul>/g, '');
  // tables: iterate lines to handle last row without trailing \n
  ;(function(){
    var lines = s.split('\n'), out = [], i = 0;
    while (i < lines.length) {
      if (/^\|.*\|$/.test(lines[i])) {
        var tbl = [], j = i;
        while (j < lines.length && /^\|.*\|$/.test(lines[j])) { tbl.push(lines[j]); j++; }
        if (tbl.length >= 2) {
          var hasSep = tbl[1] && /^\|[\s:-]+\|$/.test(tbl[1].trim());
          var hRows = hasSep ? tbl.slice(0,1) : [];
          var dRows = hasSep ? tbl.slice(2) : tbl;
          var html = '<div class="table-wrap" style="margin:4px 0"><table>';
          if (hRows.length) html += '<thead><tr>'+hRows[0].split('|').filter(function(c){return c.trim()}).map(function(c){return '<th>'+c.trim()+'</th>'}).join('')+'</tr></thead>';
          if (dRows.length) {
            html += '<tbody>';
            for (var k=0;k<dRows.length;k++) html += '<tr>'+dRows[k].split('|').filter(function(c){return c.trim()}).map(function(c){return '<td>'+c.trim()+'</td>'}).join('')+'</tr>';
            html += '</tbody>';
          }
          out.push(html+'</table></div>');
        } else { out.push(tbl.join('\n')); }
        i = j;
      } else { out.push(lines[i]); i++; }
    }
    s = out.join('\n');
  })();
  s = s.replace(/\n{2,}/g, '</p><p>');
  s = s.replace(/\n/g, '<br>');
  s = '<p>' + s + '</p>';
  s = s.replace(/<p><pre/g, '<pre').replace(/<\/pre><\/p>/g, '</pre>');
  s = s.replace(/<p><li/g, '<li').replace(/<\/li><\/p>/g, '</li>');
  s = s.replace(/<p><ul/g, '<ul').replace(/<\/ul><\/p>/g, '</ul>');
  s = s.replace(/<p><h6/g, '<h6').replace(/<\/h6><\/p>/g, '</h6>');
  s = s.replace(/<p><hr/g, '<hr').replace(/<\/p>/g, '');
  s = s.replace(/<p><div/g, '<div').replace(/<\/div><\/p>/g, '</div>');
  return s;
}

function toast(msg, type) {
  const icons = {success:'bi-check-circle-fill',danger:'bi-x-circle-fill',warning:'bi-exclamation-circle-fill',info:'bi-info-circle-fill'};
  const colors = {success:'var(--green)',danger:'var(--red)',warning:'var(--yellow)',info:'var(--accent)'};
  const c = document.getElementById('toastContainer');
  const el = document.createElement('div');
  el.className = 'toast-msg';
  el.innerHTML = `<i class="bi ${icons[type]||icons.info}" style="color:${colors[type]||colors.info}"></i> ${msg}`;
  c.appendChild(el);
  setTimeout(() => { el.style.opacity = '0'; el.style.transform = 'translateX(100%)'; el.style.transition = 'all .2s'; setTimeout(() => el.remove(), 200); }, 3000);
}

async function loadDashboard() {
  try {
    const resp = await fetch(API);
    const data = await resp.json();
    const t = data.tasks_summary, crons = data.crons;
    const s = t.stale_running || [];
    const board = t.board_summary || [];

    const activeTasks = board.filter(x => x.status==='running'||x.status==='ready'||x.status==='blocked'||x.status==='stale').reduce((a,b)=>a+b.cnt,0);
    const doneTotal = t.done_count || 0;
    const pct = t.total ? Math.round(doneTotal/t.total*100) : 0;
    const cards = [
      {label:'Tổng tasks', value:t.total, sub:pct+'% hoàn thành', icon:'bi-list-task', color:'#818cf8'},
      {label:'Đã hoàn thành', value:doneTotal, sub:activeTasks+' đang hoạt động', icon:'bi-check-circle', color:'#4ade80'},
      {label:'Treo / Đang chạy', value:t.stale_count+' / '+t.running_workers, icon:'bi-exclamation-triangle', color:t.stale_count>0?'#f87171':'#fbbf24'},
      {label:'Cron lỗi', value:data.cron_errors, sub:'/'+crons.length+' jobs', icon:'bi-x-circle', color:data.cron_errors>0?'#f87171':'#7878aa'},
    ];
    document.getElementById('statCards').innerHTML = cards.map(c => `
      <div class="stat-card">
        <div class="stat-glow" style="background:${c.color}"></div>
        <div class="stat-row">
          <div>
            <div class="stat-value" style="color:${c.color}">${c.value}</div>
            <div class="stat-label">${c.label}</div>
            ${c.sub ? '<div class="stat-sub">'+c.sub+'</div>' : ''}
          </div>
          <div class="stat-icon" style="color:${c.color}"><i class="bi ${c.icon}"></i></div>
        </div>
      </div>
    `).join('');

    document.getElementById('staleTable').innerHTML = s.length
      ? s.map(r => {
          const isStale = r.status === 'stale';
          return `<tr class="${isStale?'stale-row':''}"><td><input type="checkbox" class="stale-check" value="${r.id}" onchange="updateSelected()"></td><td><code class="task-id" onclick="openTaskDetail('${r.id}')">${h(r.id,'').substring(0,12)}</code></td><td>${h(r.title,'(no title)')}</td><td>${assigneeCell(r.assignee)}</td><td>${h(r.worker_pid)}</td><td>${h(r.age_human)}</td><td>${r.reason ? '<span class="badge-dot" style="background:'+(r.reason==='timeout'?'var(--yellow)':'var(--red)')+'1a;color:'+(r.reason==='timeout'?'var(--yellow)':'var(--red)')+';border:1px solid '+(r.reason==='timeout'?'var(--yellow)':'var(--red)')+'33"><span class="status-dot" style="background:'+(r.reason==='timeout'?'var(--yellow)':'var(--red)')+'"></span>'+r.reason+'</span>' : '<span style="color:var(--text3)">—</span>'}</td><td><button class="btn btn-sm btn-outline-warning me-1 py-0 px-2" onclick="retryTask('${r.id}')"><i class="bi bi-arrow-clockwise"></i></button><button class="btn btn-sm btn-outline-danger py-0 px-2" onclick="killTask('${r.id}')"><i class="bi bi-x-lg"></i></button></td></tr>`;
        }).join('')
      : '<tr><td colspan="8" class="empty-state">'+EMPTY_ICONS.noTasks+'Không có tác vụ treo</td></tr>';
    document.getElementById('staleCountBadge').textContent = t.stale_count;
    document.getElementById('selectAll').checked = false;
    document.getElementById('killSelectedBtn').classList.add('d-none');

    // Pie chart
    var pieEl = document.getElementById('pieChart');
    var legendEl = document.getElementById('pieLegend');
    if (pieEl && legendEl) {
      var pieColors = {ready:'#4ade80',blocked:'#6b7280',running:'#fbbf24',stale:'#f87171',done:'#38bdf8',archived:'#5858aa'};
      var pieLabels = {ready:'Sẵn sàng',blocked:'Chặn',running:'Đang chạy',stale:'Treo',done:'Hoàn tất',archived:'Lưu trữ'};
      var pieOrder = ['ready','blocked','running','stale','done','archived'];
      var totals = {};
      board.forEach(function(x){ totals[x.status] = (totals[x.status]||0) + x.cnt; });
      var items = pieOrder.filter(function(s){ return totals[s]; });
      var total = items.reduce(function(a,s){ return a+totals[s]; }, 0) || 1;
      var conic = items.map(function(s,i){
        var pct = totals[s]/total*100;
        var start = items.slice(0,i).reduce(function(a,ss){ return a+totals[ss]/total*100; }, 0);
        return pieColors[s]+' '+start+'% '+(start+pct)+'%';
      }).join(', ');
      pieEl.style.background = 'conic-gradient('+conic+')';
      legendEl.innerHTML = items.map(function(s){ return '<div class="d-flex align-items-center gap-2 mb-1"><span style="width:10px;height:10px;border-radius:3px;background:'+pieColors[s]+';flex-shrink:0"></span><span>'+pieLabels[s]+'</span><span class="ms-auto" style="color:var(--text3)">'+totals[s]+'</span></div>'; }).join('');
    }

    // Recent activity from task-outputs API
    try {
      var recentResp = await fetch('/api/task-outputs');
      var recentData = await recentResp.json();
      var recentEl = document.getElementById('recentTable');
      recentEl.innerHTML = (recentData||[]).slice(0,10).map(function(t,i){
        var ts = t.completed_at || t.started_at;
        var shortTitle = (t.title||t.id).substring(0,45)+(t.title&&t.title.length>45?'...':'');
        return '<tr><td><span class="row-idx">'+(i+1)+'</span></td><td><a href="#" onclick="openTaskDetail(\''+t.id+'\');return false" class="task-link" style="font-size:.72rem">'+h(shortTitle)+'</a></td><td>'+badge(t.status)+'</td><td style="color:var(--text3);font-size:.68rem;white-space:nowrap" title="'+fmtTime(ts)+'">'+fmtRelative(ts)+'</td></tr>';
      }).join('') || '<tr><td colspan="4" class="empty-state">'+EMPTY_ICONS.noData+'Chưa có dữ liệu</td></tr>';
    } catch(_) { /* ignore recent fetch errors */ }

    renderKanban(board);
    renderCron(crons);
    document.getElementById('cronBadge').textContent = crons.length;
    document.getElementById('cronCountBadge').textContent = crons.length;
    document.getElementById('lastUpdate').textContent = new Date().toLocaleTimeString('vi-VN');
    loadOutputs();
  } catch(e) { console.error(e); toast('Lỗi tải dữ liệu', 'danger'); }
}

function renderKanban(data) {
  var statusOrder = ['ready','blocked','running','stale','done','archived'];
  var statusLabels = {ready:'Sẵn sàng',blocked:'Chặn',running:'Đang chạy',stale:'Treo',done:'Hoàn tất',archived:'Lưu trữ'};
  var statusColors = {ready:'#4ade80',blocked:'#6b7280',running:'#fbbf24',stale:'#f87171',done:'#38bdf8',archived:'#5858aa'};

  var search = (document.getElementById('searchInput').value||'').toLowerCase();
  var cols = {};
  data.forEach(function(r){
    if (!cols[r.status]) cols[r.status] = {};
    var a = r.assignee || 'unassigned';
    cols[r.status][a] = (cols[r.status][a]||0) + r.cnt;
  });

  var html = '';
  statusOrder.forEach(function(s){
    var agents = cols[s] || {};
    var allEntries = Object.entries(agents);
    var total = allEntries.reduce(function(a,b){ return a+b[1]; }, 0);
    var filtered = allEntries.filter(function(e){ return !search || e[0].toLowerCase().includes(search); });
    html += '<div class="kanban-col"><div class="kanban-col-head" style="border-bottom-color:'+statusColors[s]+'60;color:'+statusColors[s]+'"><span>'+statusLabels[s]+'</span><span class="badge-dot" style="background:'+statusColors[s]+'1a;color:'+statusColors[s]+';border:1px solid '+statusColors[s]+'33"><span class="status-dot" style="background:'+statusColors[s]+'"></span>'+total+'</span></div><div class="kanban-col-body">';
    if (filtered.length) {
      filtered.forEach(function(e){
        var name = e[0] === 'unassigned' ? '?' : e[0];
        html += '<div class="kanban-item" style="border-left:3px solid '+statusColors[s]+'60" onclick="openTasksModal(\''+e[0]+'\')">'+avatar(name,'sm')+'<span class="kanban-item-text">'+e[0]+'</span><span class="kanban-item-count" style="color:'+statusColors[s]+'">'+e[1]+'</span></div>';
      });
    } else {
      html += '<div class="empty-state" style="padding:.8rem 0;font-size:.68rem;color:var(--text3)">Trống</div>';
    }
    html += '</div></div>';
  });
  document.getElementById('kanbanBoard').innerHTML = html || '<div class="empty-state" style="padding:2.5rem">'+EMPTY_ICONS.noData+'Chưa có dữ liệu</div>';
}

function renderCron(crons) {
  document.getElementById('cronTable').innerHTML = crons.length
    ? crons.map(c => {
        const st = c.last_status || 'unknown';
        const err = c.last_error || c.last_delivery_error || '';
        return `<tr><td><strong>${h(c.name)}</strong></td><td><code style="color:var(--text3);font-size:.72rem">${h(c.schedule_display)}</code></td><td style="color:var(--text2);font-size:.72rem" title="${fmtTime(c.next_run_at)}">${fmtRelative(c.next_run_at)}</td><td style="color:var(--text2);font-size:.72rem" title="${fmtTime(c.last_run_at)}">${fmtRelative(c.last_run_at)}</td><td>${badge(st)}</td><td style="max-width:250px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:.72rem" title="${err.replace(/"/g,'&quot;')}">${err ? err.substring(0,60)+(err.length>60?'...':'') : '<span style="color:var(--text3)">—</span>'}</td></tr>`;
      }).join('')
    : '<tr><td colspan="6" class="empty-state">'+EMPTY_ICONS.noCron+'Không có dữ liệu cron</td></tr>';
}

async function loadOutputs() {
  try {
    const r = await fetch('/api/task-outputs');
    const d = await r.json();
    document.getElementById('outputCountBadge').textContent = d.length;
    document.getElementById('outputRefreshLabel').textContent = new Date().toLocaleTimeString('vi-VN');
    document.getElementById('outputTable').innerHTML = d.length
      ? d.map((t, i) => {
          const ts = t.completed_at || t.started_at;
          return `<tr><td><span class="row-idx">${i+1}</span></td><td><a href="#" onclick="openTaskDetail('${t.id}');return false" class="task-link">${h(t.title||t.id)}</a></td><td>${assigneeCell(t.assignee)}</td><td>${badge(t.status)}</td><td style="color:var(--text3);font-size:.7rem;white-space:nowrap" title="${fmtTime(ts)}">${fmtRelative(ts)}</td></tr>`;
        }).join('')
      : '<tr><td colspan="5" class="empty-state">'+EMPTY_ICONS.noOutput+'Chưa có output</td></tr>';
  } catch(e) { document.getElementById('outputTable').innerHTML = '<tr><td colspan="6" class="empty-state">'+EMPTY_ICONS.error+'Lỗi: '+e.message+'</td></tr>'; }
}

function updateSelected() {
  const n = document.querySelectorAll('.stale-check:checked').length;
  const btn = document.getElementById('killSelectedBtn');
  if (n) { btn.classList.remove('d-none'); document.getElementById('selectedCount').textContent = n; }
  else { btn.classList.add('d-none'); }
}
function toggleSelectAll() {
  document.querySelectorAll('.stale-check').forEach(cb => cb.checked = document.getElementById('selectAll').checked);
  updateSelected();
}
function getSelectedIds() { return Array.from(document.querySelectorAll('.stale-check:checked')).map(cb=>cb.value); }

async function bulkKill(ids, label) {
  if (!ids.length) { toast('Không có task để kill', 'warning'); return; }
  if (!confirm(`Kill ${ids.length} task (${label})?`)) return;
  try {
    const r = await fetch('/api/tasks/bulk-kill', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({ids})});
    const d = await r.json();
    toast(d.message || `Đã xử lý ${d.count} task`, d.ok ? 'warning' : 'danger');
    loadDashboard();
  } catch(e) { toast('Lỗi: '+e, 'danger'); }
}
function killSelected() { bulkKill(getSelectedIds(), 'đã chọn'); }
function killAllDead() { bulkKill(Array.from(document.querySelectorAll('.stale-check')).map(cb=>cb.value), 'all'); }

async function openTasksModal(assignee) {
  try {
    const r = await fetch(`/api/tasks?assignee=${encodeURIComponent(assignee)}`);
    const tasks = await r.json();
    document.getElementById('tasksModalTitle').innerHTML = `${avatar(assignee,'md')} <span class="ms-1">${assignee}</span> <span class="badge bg-secondary ms-2">${tasks.length}</span>`;
    document.getElementById('tasksModalBody').innerHTML = tasks.length
      ? tasks.map(t => `<tr><td><code class="task-id" onclick="bootstrap.Modal.getInstance(document.getElementById('tasksModal')).hide();openTaskDetail('${t.id}')">${t.id.substring(0,12)}</code></td><td>${h(t.title)}</td><td>${badge(t.status)}</td><td>${h(t.worker_pid)}</td><td style="max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:.72rem" title="${(t.last_failure_error||'').replace(/"/g,'&quot;')}">${t.last_failure_error ? t.last_failure_error.substring(0,50)+(t.last_failure_error.length>50?'...':'') : '<span style="color:var(--text3)">—</span>'}</td></tr>`).join('')
      : '<tr><td colspan="5" class="empty-state">'+EMPTY_ICONS.noTasks+'Không có task</td></tr>';
    new bootstrap.Modal(document.getElementById('tasksModal')).show();
  } catch(e) { toast('Lỗi: '+e, 'danger'); }
}

async function retryTask(id) {
  if (!confirm(`Retry task ${id}?`)) return;
  try { const r = await fetch(`/api/task/${id}/retry`, {method:'POST'}); const d = await r.json(); toast(d.message, 'success'); }
  catch(e) { toast('Lỗi: '+e, 'danger'); }
}
async function killTask(id) {
  if (!confirm(`Kill task ${id}?`)) return;
  try { const r = await fetch(`/api/task/${id}/kill`, {method:'POST'}); const d = await r.json(); toast(d.message, 'warning'); loadDashboard(); }
  catch(e) { toast('Lỗi: '+e, 'danger'); }
}

async function openTaskDetail(id) {
  try {
    const r = await fetch(`/api/task/${id}`);
    const d = await r.json();
    if (d.error) { toast(d.error, 'danger'); return; }
    const t = d.task;
    const evs = (d.events||[]).slice(0,10);
    const runs = (d.runs||[]).slice(0,10);
    const rawOutput = d.output;

    // Title
    document.getElementById('modalTitle').innerHTML = `${h(t.title||id)} <span style="font-weight:400;font-size:.72rem;color:var(--text2);display:inline-flex;align-items:center;gap:4px;margin-left:6px">${avatar(t.assignee,'sm')}${h(t.assignee)}</span>`;

    // Metadata chips
    const metaHtml = `<div class="meta-chips">
      ${badge(t.status)}
      <span class="meta-chip-sep"></span>
      <span class="meta-chip"><i class="bi bi-123"></i> <strong>${t.id.substring(0,10)}</strong></span>
      <span class="meta-chip-sep"></span>
      <span class="meta-chip"><i class="bi bi-cpu"></i> PID: <strong>${h(t.worker_pid, '—')}</strong></span>
      <span class="meta-chip-sep"></span>
      <span class="meta-chip"><i class="bi bi-x-circle" style="color:${t.consecutive_failures>0?'var(--red)':'var(--text3)'}"></i> Lỗi: <strong>${h(t.consecutive_failures,'0')}</strong></span>
    </div>`;

    // Error collapse
    const errorHtml = t.last_failure_error ? `<div class="error-collapse open" id="errorCollapse">
      <div class="error-collapse-header" onclick="toggleErrorCollapse()"><i class="bi bi-exclamation-triangle-fill"></i>LỖI GẦN NHẤT${t.consecutive_failures>0?' (x'+t.consecutive_failures+')':''}</div>
      <div class="error-collapse-body" id="errorCollapseBody">${t.last_failure_error.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')}</div>
    </div>` : '';

    // Output tab content
    const outputHtml = rawOutput
      ? `<div class="output-toolbar">
        <span class="toolbar-btn active" id="outViewRendered" onclick="toggleOutputView('rendered');return false"><i class="bi bi-eye"></i> Hiển thị</span>
        <span class="toolbar-btn" id="outViewRaw" onclick="toggleOutputView('raw');return false"><i class="bi bi-braces"></i> Raw</span>
        <span class="toolbar-sep"></span>
        <span class="toolbar-btn" id="copyBtn" onclick="copyOutput()" title="Ctrl+C"><i class="bi bi-clipboard"></i> Copy</span>
        <span class="toolbar-btn" id="expandBtn" onclick="toggleOutputExpand()" title="Toàn màn hình"><i class="bi bi-arrows-fullscreen" id="expandIcon"></i></span>
      </div>
      <div class="output-block" id="outputBlockContent">${renderMd(rawOutput)}</div>
      <pre class="output-raw" id="outputRawContent" style="display:none">${h(rawOutput)}</pre>`
      : `<div class="empty-state" style="padding:3rem">${EMPTY_ICONS.noOutput}Không có output được ghi lại</div>`;

    // Events tab content (timeline)
    const evHtml = evs.length
      ? '<div class="timeline">'+evs.map(e => `<div class="timeline-item timeline-${h(e.kind,'unknown')}"><span class="timeline-time" title="${fmtTime(e.created_at)}">${fmtRelative(e.created_at)}</span><div class="timeline-content"><span class="badge-dot" style="background:${(S_COLOR[e.kind]||'#6b7280')}1a;color:${S_COLOR[e.kind]||'#6b7280'};border:1px solid ${(S_COLOR[e.kind]||'#6b7280')}33"><span class="status-dot" style="background:${S_COLOR[e.kind]||'#6b7280'}"></span>${h(e.kind)}</span></div></div>`).join('')+'</div>'
      : '<div class="empty-state" style="padding:2rem">'+EMPTY_ICONS.noEvents+'Không có sự kiện</div>';

    // Runs tab content
    const runsHtml = runs.length
      ? '<div class="timeline">'+runs.map(r => {
          const st = r.status || 'unknown';
          return `<div class="timeline-item ${st==='done'?'timeline-complete':st==='running'?'timeline-enqueue':''}"><span class="timeline-time" title="${fmtTime(r.started_at)}">${fmtRelative(r.started_at)}</span><div class="timeline-content">${badge(st)}${r.error ? '<span style="color:var(--red);font-size:.7rem;max-width:300px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;margin-left:4px">'+r.error.substring(0,60)+'</span>' : ''}</div></div>`;
        }).join('')+'</div>'
      : '<div class="empty-state" style="padding:2rem">'+EMPTY_ICONS.noData+'Không có lần chạy</div>';

    // Build body
    document.getElementById('modalBody').innerHTML = `
      ${errorHtml}
      ${metaHtml}
      <div class="modal-tabs">
        <button class="modal-tab active" onclick="switchModalTab('output');return false"><i class="bi bi-file-text"></i> Output</button>
        <button class="modal-tab" onclick="switchModalTab('events');return false"><i class="bi bi-clock-history"></i> Sự kiện <span class="tab-count">${evs.length}</span></button>
        <button class="modal-tab" onclick="switchModalTab('runs');return false"><i class="bi bi-play-circle"></i> Lần chạy <span class="tab-count">${runs.length}</span></button>
      </div>
      <div class="modal-tab-pane active" id="pane-output">${outputHtml}</div>
      <div class="modal-tab-pane" id="pane-events">${evHtml}</div>
      <div class="modal-tab-pane" id="pane-runs">${runsHtml}</div>`;

    window._taskOutputData = { rawOutput, taskId: id };
    new bootstrap.Modal(document.getElementById('taskModal')).show();
  } catch(e) { toast('Lỗi: '+e, 'danger'); }
}

function switchModalTab(name) {
  document.querySelectorAll('.modal-tab').forEach((b,i) => b.classList.toggle('active', b.textContent.trim().toLowerCase().includes(name)));
  document.querySelectorAll('.modal-tab-pane').forEach(p => {
    p.classList.toggle('active', p.id === 'pane-'+name || (name==='output'&&p.id==='pane-output'));
  });
  if (name === 'output') { toggleErrorCollapse(true); }
}
function toggleErrorCollapse(show) {
  var ec = document.getElementById('errorCollapse');
  if (!ec) return;
  if (show !== undefined) { if (show) ec.classList.add('open'); else ec.classList.remove('open'); }
  else ec.classList.toggle('open');
}

function toggleOutputView(mode) {
  var rendered = document.getElementById('outputBlockContent');
  var raw = document.getElementById('outputRawContent');
  var btnR = document.getElementById('outViewRendered');
  var btnRaw = document.getElementById('outViewRaw');
  if (!rendered || !raw) return;
  if (mode === 'rendered') {
    rendered.style.display = ''; raw.style.display = 'none';
    btnR.classList.add('active'); btnRaw.classList.remove('active');
  } else {
    rendered.style.display = 'none'; raw.style.display = '';
    btnR.classList.remove('active'); btnRaw.classList.add('active');
  }
}

function toggleOutputExpand() {
  var dlg = document.querySelector('#taskModal .modal-dialog');
  var icon = document.getElementById('expandIcon');
  if (!dlg || !icon) return;
  var isFull = dlg.classList.contains('modal-fullscreen');
  if (isFull) {
    dlg.classList.remove('modal-fullscreen');
    icon.className = 'bi bi-arrows-fullscreen';
  } else {
    dlg.classList.add('modal-fullscreen');
    icon.className = 'bi bi-arrows-angle-contract';
  }
}

async function copyOutput() {
  var data = window._taskOutputData;
  var text = data ? (data.rawOutput || '') : '';
  if (!text) { toast('Không có nội dung để copy', 'warning'); return; }
  try {
    await navigator.clipboard.writeText(text);
    var btn = document.getElementById('copyBtn');
    if (btn) { btn.classList.add('copied'); setTimeout(function(){ btn.classList.remove('copied'); }, 1500); }
    toast('Đã copy vào clipboard', 'success');
  } catch(e) { toast('Lỗi: '+e, 'danger'); }
}

document.getElementById('searchInput').addEventListener('input', () => {
  if (document.querySelector('#tab-kanban.active')) loadDashboard();
  if (document.querySelector('#tab-outputs.active')) loadOutputs();
});

document.addEventListener('keydown', function(e) {
  if ((e.ctrlKey || e.metaKey) && e.key === 'k') {
    e.preventDefault();
    document.getElementById('searchInput').focus();
    document.getElementById('searchInput').select();
  }
  if ((e.ctrlKey || e.metaKey) && e.key === 'c') {
    var sel = window.getSelection().toString();
    if (!sel && document.querySelector('#taskModal.show') && window._taskOutputData && window._taskOutputData.rawOutput) {
      e.preventDefault();
      copyOutput();
    }
  }
  if (e.key === 'Escape') {
    var taskModal = document.querySelector('#taskModal.show');
    if (taskModal) {
      bootstrap.Modal.getInstance(document.getElementById('taskModal')).hide();
      return;
    }
    var searchEl = document.getElementById('searchInput');
    if (document.activeElement === searchEl) {
      searchEl.value = '';
      searchEl.blur();
      if (document.querySelector('#tab-kanban.active')) loadDashboard();
      if (document.querySelector('#tab-outputs.active')) loadOutputs();
    }
  }
});

document.querySelectorAll('[data-bs-toggle="tab"]').forEach(tab => {
  tab.addEventListener('shown.bs.tab', function(e) {
    if (e.target.id === 'tab-cron') {
      document.getElementById('cronRefreshDot').classList.add('loading');
      document.getElementById('cronRefreshLabel').textContent = 'Đang tải...';
      if (!cronTimer) cronTimer = setInterval(async () => { await loadDashboard(); document.getElementById('cronRefreshDot').classList.remove('loading'); document.getElementById('cronRefreshLabel').textContent = '30s auto'; }, 30000);
    } else if (e.target.id === 'tab-outputs') {
      loadOutputs();
      if (cronTimer) { clearInterval(cronTimer); cronTimer = null; }
    } else { if (cronTimer) { clearInterval(cronTimer); cronTimer = null; } }
  });
});

function initTooltips() {
  document.querySelectorAll('[data-bs-toggle="tooltip"]').forEach(el => {
    try { bootstrap.Tooltip.getOrCreateInstance(el).dispose(); bootstrap.Tooltip.getOrCreateInstance(el); } catch(e) {}
  });
}

function toggleTheme() {
  const html = document.documentElement;
  const isDark = html.getAttribute('data-bs-theme') !== 'light';
  const newTheme = isDark ? 'light' : 'dark';
  html.setAttribute('data-bs-theme', newTheme);
  localStorage.setItem('theme', newTheme);
  const icon = document.querySelector('#themeToggle i');
  if (icon) icon.className = newTheme === 'dark' ? 'bi bi-moon-stars' : 'bi bi-sun';
}

(function initTheme() {
  const saved = localStorage.getItem('theme');
  const prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
  const theme = saved || (prefersDark ? 'dark' : 'light');
  document.documentElement.setAttribute('data-bs-theme', theme);
  const icon = document.querySelector('#themeToggle i');
  if (icon) icon.className = theme === 'dark' ? 'bi bi-moon-stars' : 'bi bi-sun';
})();

setInterval(() => { initTooltips(); }, 2000);
initTooltips();
loadDashboard();
</script>
</body>
</html>"""

if __name__ == '__main__':
    print(f"[cron] fallback path: {CRON_JSON}")
    print(f"[board] filter: {BOARD}")
    print(f"Monitoring Dashboard @ http://localhost:8093")
    app.run(host='0.0.0.0', port=8093, debug=False)
