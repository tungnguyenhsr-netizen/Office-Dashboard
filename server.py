# -*- coding: utf-8 -*-
import io, json, os, signal, sqlite3, subprocess, sys, time, uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from flask import Flask, jsonify, render_template_string, request, session, redirect

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET', os.urandom(24).hex())

FLASK_USER = os.environ.get('FLASK_USER', 'admin')
FLASK_PASS = os.environ.get('FLASK_PASS', 'admin')

@app.before_request
def check_auth():
    if request.path in ('/login', '/api/login'):
        return
    if 'user' not in session:
        if request.path.startswith('/api/'):
            return jsonify({'error': 'Unauthorized'}), 401
        return render_template_string(LOGIN_PAGE)

@app.route('/login', methods=['POST'])
def do_login():
    data = request.json if request.is_json else request.form
    username = data.get('username', '')
    password = data.get('password', '')
    if username == FLASK_USER and password == FLASK_PASS:
        session['user'] = username
        return jsonify({'ok': True})
    return jsonify({'ok': False, 'message': 'Sai tài khoản hoặc mật khẩu'}), 401

@app.route('/logout')
def do_logout():
    session.pop('user', None)
    return redirect('/')

DB = os.path.expandvars(r'%LOCALAPPDATA%\hermes\kanban.db')
CRON_JSON = os.path.expandvars(r'%LOCALAPPDATA%\hermes\cron\jobs.json')
BOARD = "%"
TZ = timezone(timedelta(hours=7))

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
                        _FILE_INDEX.append({'name': fn, 'path': rel, 'modified': st.st_mtime, 'size': st.st_size})
                    except:
                        pass
    _FILE_INDEX_TIME = now
    return _FILE_INDEX

def db_conn(readonly=True):
    conn = sqlite3.connect(DB, timeout=5)
    conn.row_factory = sqlite3.Row
    if readonly:
        conn.execute("PRAGMA query_only = ON")
    return conn

DB_EXISTS = os.path.exists(DB)

def fetch_tasks_summary():
    if not DB_EXISTS:
        return {'total': 0, 'done_count': 0, 'active_count': 0, 'stale_count': 0, 'running_workers': 0, 'board_summary': [], 'stale_running': []}
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
VAULT_CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'vault-config.json')

def _read_vault_config():
    if os.path.exists(VAULT_CONFIG_FILE):
        try:
            with open(VAULT_CONFIG_FILE, 'r', encoding='utf-8') as f:
                return json.load(f).get('vault_dir', '')
        except:
            pass
    return ''

VAULT_ROOT = os.environ.get('HERMES_VAULT_DIR', _read_vault_config())

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

@app.route('/api/tasks/all')
def api_tasks_all():
    status = request.args.get('status', '')
    assignee = request.args.get('assignee', '')
    sort = request.args.get('sort', 'created_at')
    order = request.args.get('order', 'desc')
    limit = min(int(request.args.get('limit', 50)), 200)
    offset = int(request.args.get('offset', 0))
    conn = db_conn()
    c = conn.cursor()
    allowed_sorts = {'created_at', 'started_at', 'completed_at', 'title', 'status', 'assignee', 'consecutive_failures'}
    if sort not in allowed_sorts:
        sort = 'created_at'
    if order not in ('asc', 'desc'):
        order = 'desc'
    where = []
    params = []
    if status:
        statuses = status.split(',')
        where.append('status IN (' + ','.join('?' for _ in statuses) + ')')
        params.extend(statuses)
    if assignee:
        where.append('assignee = ?')
        params.append(assignee)
    where_clause = ('WHERE ' + ' AND '.join(where)) if where else ''
    total = c.execute(f"SELECT COUNT(1) FROM tasks {where_clause}", params).fetchone()[0]
    rows = c.execute(f"SELECT * FROM tasks {where_clause} ORDER BY {sort} {order} LIMIT ? OFFSET ?", params + [limit, offset]).fetchall()
    conn.close()
    return jsonify({'tasks': [dict(r) for r in rows], 'total': total, 'limit': limit, 'offset': offset})

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
        if sys.platform == 'win32':
            r = subprocess.run(['taskkill', '/F', '/PID', str(pid)], capture_output=True, text=True, timeout=10)
            if r.returncode == 0:
                return True, 'đã kill'
            return False, r.stderr.strip() or r.stdout.strip() or 'taskkill thất bại'
        else:
            os.kill(pid, signal.SIGTERM)
            return True, 'đã kill'
    except subprocess.TimeoutExpired:
        return False, 'timeout'
    except ProcessLookupError:
        return False, 'process đã chết trước đó'
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
    conn = db_conn(readonly=False)
    c = conn.cursor()
    try:
        existing = c.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not existing:
            conn.close()
            return jsonify({'ok': False, 'message': 'Task không tồn tại'}), 404
        c.execute("UPDATE tasks SET status='ready', consecutive_failures=0, last_failure_error=NULL WHERE id=?",
                  (task_id,))
        c.execute("INSERT INTO task_events (task_id, kind, created_at) VALUES (?, 'retry', ?)",
                  (task_id, int(time.time())))
        conn.commit()
        conn.close()
        return jsonify({'ok': True, 'message': f'Retry {task_id} thành công'})
    except Exception as e:
        conn.close()
        return jsonify({'ok': False, 'message': str(e)}), 500

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

@app.route('/api/tasks/bulk-delete', methods=['POST'])
def api_bulk_delete():
    ids = request.json.get('ids', []) if request.is_json else []
    if not ids:
        return jsonify({'ok': False, 'message': 'Không có task nào'}), 400
    conn = db_conn(readonly=False)
    c = conn.cursor()
    try:
        placeholders = ','.join('?' for _ in ids)
        c.execute(f"DELETE FROM task_events WHERE task_id IN ({placeholders})", ids)
        c.execute(f"DELETE FROM task_runs WHERE task_id IN ({placeholders})", ids)
        c.execute(f"DELETE FROM tasks WHERE id IN ({placeholders})", ids)
        conn.commit()
        return jsonify({'ok': True, 'count': len(ids), 'message': f'Đã xoá {len(ids)} tasks'})
    except Exception as e:
        return jsonify({'ok': False, 'message': str(e)}), 500
    finally:
        conn.close()

@app.route('/api/tasks', methods=['POST'])
def api_create_task():
    data = request.json or {}
    title = data.get('title', '').strip()
    assignee = data.get('assignee', '').strip()
    description = data.get('description', '').strip()
    if not title:
        return jsonify({'ok': False, 'message': 'Thiếu tiêu đề task'}), 400
    task_id = str(uuid.uuid4())[:36]
    now = int(time.time())
    conn = db_conn(readonly=False)
    c = conn.cursor()
    try:
        c.execute("INSERT INTO tasks (id, title, assignee, status, created_at, workspace_kind) VALUES (?, ?, ?, 'ready', ?, 'scratch')",
                  (task_id, title, assignee or None, now))
        c.execute("INSERT INTO task_events (task_id, kind, created_at) VALUES (?, 'created', ?)",
                  (task_id, now))
        conn.commit()
        conn.close()
        return jsonify({'ok': True, 'task_id': task_id, 'message': 'Đã tạo task'})
    except Exception as e:
        conn.close()
        return jsonify({'ok': False, 'message': str(e)}), 500

@app.route('/api/task/<task_id>', methods=['PATCH'])
def api_update_task(task_id):
    data = request.json or {}
    conn = db_conn(readonly=False)
    c = conn.cursor()
    try:
        existing = c.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not existing:
            conn.close()
            return jsonify({'ok': False, 'message': 'Task không tồn tại'}), 404
        updates = []
        params = []
        if 'status' in data and data['status'] != existing['status']:
            updates.append("status = ?")
            params.append(data['status'])
            c.execute("INSERT INTO task_events (task_id, kind, created_at) VALUES (?, ?, ?)",
                      (task_id, data['status'], int(time.time())))
        if 'assignee' in data and data['assignee'] != existing['assignee']:
            updates.append("assignee = ?")
            params.append(data['assignee'])
        if 'body' in data and data['body'] != existing['body']:
            updates.append("body = ?")
            params.append(data['body'])
        if updates:
            params.append(task_id)
            c.execute(f"UPDATE tasks SET {', '.join(updates)} WHERE id = ?", params)
            conn.commit()
        conn.close()
        return jsonify({'ok': True, 'message': 'Đã cập nhật task'})
    except Exception as e:
        conn.close()
        return jsonify({'ok': False, 'message': str(e)}), 500

@app.route('/api/task/<task_id>', methods=['DELETE'])
def api_delete_task(task_id):
    data = request.json or {}
    if data.get('confirm') != 'CONFIRM':
        return jsonify({'ok': False, 'message': 'Gõ CONFIRM để xác nhận xoá'}), 400
    conn = db_conn(readonly=False)
    c = conn.cursor()
    try:
        c.execute("DELETE FROM task_events WHERE task_id = ?", (task_id,))
        c.execute("DELETE FROM task_runs WHERE task_id = ?", (task_id,))
        c.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        conn.commit()
        return jsonify({'ok': True, 'message': f'Đã xoá task {task_id[:10]} thành công'})
    except Exception as e:
        return jsonify({'ok': False, 'message': str(e)}), 500
    finally:
        conn.close()

@app.route('/api/task/<task_id>/output', methods=['PATCH'])
def api_update_task_output(task_id):
    data = request.json or {}
    output = data.get('output', '')
    conn = db_conn(readonly=False)
    c = conn.cursor()
    try:
        c.execute("UPDATE tasks SET result = ? WHERE id = ?",
                  (output, task_id))
        conn.commit()
        conn.close()
        return jsonify({'ok': True, 'message': 'Đã cập nhật output'})
    except Exception as e:
        conn.close()
        return jsonify({'ok': False, 'message': str(e)}), 500

@app.route('/api/workers')
def api_workers():
    if not DB_EXISTS: return jsonify([])
    conn = db_conn()
    c = conn.cursor()
    rows = c.execute("""
        SELECT r.profile, r.worker_pid, r.status as run_status,
               r.started_at, r.last_heartbeat_at,
               t.id as task_id, t.title, t.assignee
        FROM task_runs r
        JOIN tasks t ON t.id = r.task_id
        WHERE r.profile IS NOT NULL AND r.profile != ''
        ORDER BY r.profile, r.started_at DESC
    """).fetchall()
    conn.close()
    workers = {}
    for r in rows:
        p = r['profile']
        if p not in workers:
            workers[p] = {
                'profile': p,
                'pid': r['worker_pid'],
                'current_task': None,
                'task_count': 0,
                'last_heartbeat': r['last_heartbeat_at'],
                'started_at': r['started_at'],
                'status': 'offline'
            }
        workers[p]['task_count'] += 1
        if r['run_status'] == 'running' and not workers[p].get('current_task'):
            workers[p]['current_task'] = {'id': r['task_id'], 'title': r['title']}
            workers[p]['status'] = 'running'
            workers[p]['pid'] = r['worker_pid']
    return jsonify(list(workers.values()))

@app.route('/api/files')
def api_files():
    results = []
    efforts_dir = os.path.join(VAULT_ROOT, 'Efforts')
    if not os.path.exists(efforts_dir):
        return jsonify([])
    for root, dirs, files in os.walk(efforts_dir):
        for fn in files:
            if fn.endswith('.md'):
                path = os.path.join(root, fn)
                rel = os.path.relpath(path, VAULT_ROOT)
                try:
                    st = os.stat(path)
                    preview = ''
                    try:
                        with open(path, 'r', encoding='utf-8', errors='ignore') as f:
                            preview = f.read()
                    except:
                        pass
                    results.append({
                        'name': fn,
                        'path': rel,
                        'modified': st.st_mtime,
                        'size': st.st_size,
                        'preview': preview,
                    })
                except:
                    pass
    results.sort(key=lambda x: x['modified'], reverse=True)
    return jsonify(results)

@app.route('/api/config', methods=['GET'])
def api_get_config():
    return jsonify({'vault_dir': VAULT_ROOT})

@app.route('/api/config/vault', methods=['POST'])
def api_set_vault():
    global VAULT_ROOT, _FILE_INDEX, _FILE_INDEX_TIME
    data = request.json or {}
    vault_dir = data.get('vault_dir', '').strip()
    try:
        with open(VAULT_CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump({'vault_dir': vault_dir}, f, indent=2, ensure_ascii=False)
        VAULT_ROOT = vault_dir
        _FILE_INDEX = []
        _FILE_INDEX_TIME = 0
        return jsonify({'ok': True, 'vault_dir': vault_dir, 'message': 'Đã lưu cấu hình vault'})
    except Exception as e:
        return jsonify({'ok': False, 'message': str(e)}), 500

@app.route('/api/conversations')
def api_conversations():
    results = []
    profiles_dir = os.path.join(HERMES_HOME, 'profiles')
    if not os.path.exists(profiles_dir):
        return jsonify([])
    for name in os.listdir(profiles_dir):
        state_path = os.path.join(profiles_dir, name, 'state.db')
        if not os.path.exists(state_path):
            continue
        try:
            sconn = sqlite3.connect(state_path)
            sconn.row_factory = sqlite3.Row
            sc = sconn.cursor()
            sessions = sc.execute("""
                SELECT id, title, model, started_at, ended_at, message_count,
                       tool_call_count, input_tokens, output_tokens, estimated_cost_usd
                FROM sessions ORDER BY started_at DESC LIMIT 50
            """).fetchall()
            sconn.close()
            for s in sessions:
                d = dict(s)
                d['profile'] = name
                results.append(d)
        except Exception:
            pass
    results.sort(key=lambda x: x.get('started_at', 0) or 0, reverse=True)
    return jsonify(results[:100])

@app.route('/api/conversation/<profile>/<session_id>')
def api_conversation_detail(profile, session_id):
    state_path = os.path.join(HERMES_HOME, 'profiles', profile, 'state.db')
    if not os.path.exists(state_path):
        return jsonify({'ok': False, 'message': 'Profile không tồn tại'}), 404
    try:
        sconn = sqlite3.connect(state_path)
        sconn.row_factory = sqlite3.Row
        sc = sconn.cursor()
        session = sc.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        if not session:
            sconn.close()
            return jsonify({'ok': False, 'message': 'Session không tồn tại'}), 404
        messages = sc.execute("""
            SELECT id, role, content, timestamp, tool_calls, tool_name, reasoning
            FROM messages WHERE session_id=? ORDER BY timestamp ASC
        """, (session_id,)).fetchall()
        sconn.close()
        return jsonify({
            'ok': True, 'session': dict(session), 'profile': profile,
            'messages': [dict(m) for m in messages]
        })
    except Exception as e:
        return jsonify({'ok': False, 'message': str(e)}), 500

@app.route('/api/system-health')
def api_system_health():
    try:
        import psutil
        cpu = {'percent': psutil.cpu_percent(interval=0.5), 'count': psutil.cpu_count()}
        mem = psutil.virtual_memory()
        disk = psutil.disk_usage('/')
        uptime_sec = int(time.time() - psutil.boot_time())
    except Exception:
        cpu = {'percent': 0, 'count': 0}
        mem = {'total': 0, 'used': 0, 'percent': 0}
        disk = {'total': 0, 'used': 0, 'percent': 0}
        uptime_sec = 0
    # Check hermes processes
    hermes = {'dispatcher_running': False, 'dispatcher_pid': None, 'orchestrator_running': False}
    try:
        for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
            try:
                cmd = ' '.join(proc.info.get('cmdline') or [])
                if 'dispatcher' in cmd.lower():
                    hermes['dispatcher_running'] = True
                    hermes['dispatcher_pid'] = proc.info['pid']
                if 'orchestrator' in cmd.lower():
                    hermes['orchestrator_running'] = True
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
    except Exception:
        pass
    return jsonify({
        'cpu': cpu, 'memory': {'total': mem.total, 'used': mem.used, 'percent': mem.percent},
        'disk': {'total': disk.total, 'used': disk.used, 'percent': disk.percent},
        'uptime': uptime_sec, 'hermes': hermes
    })

@app.route('/api/cron/<name>/toggle', methods=['POST'])
def api_cron_toggle(name):
    try:
        with open(CRON_JSON, 'r', encoding='utf-8') as f:
            config = json.load(f)
        jobs = config.get('jobs', [])
        found = None
        for j in jobs:
            if j.get('name') == name:
                found = j
                break
        if not found:
            return jsonify({'ok': False, 'message': 'Không tìm thấy cron job'}), 404
        new_enabled = not found.get('enabled', True)
        found['enabled'] = new_enabled
        with open(CRON_JSON, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
        return jsonify({'ok': True, 'enabled': new_enabled, 'message': f"{'Bật' if new_enabled else 'Tắt'} {name} thành công"})
    except Exception as e:
        return jsonify({'ok': False, 'message': str(e)}), 500

@app.route('/api/export/tasks')
def api_export_tasks():
    fmt = request.args.get('format', 'json').lower()
    conn = db_conn()
    c = conn.cursor()
    rows = c.execute("SELECT * FROM tasks ORDER BY created_at DESC").fetchall()
    conn.close()
    data = [dict(r) for r in rows]
    if fmt == 'csv':
        import io as _io
        if not data:
            return jsonify([])
        keys = list(data[0].keys())
        buf = _io.StringIO()
        buf.write(','.join(f'"{k}"' for k in keys) + '\n')
        quote = chr(34)
        for row in data:
            vals = []
            for k in keys:
                v = str(row.get(k, '')).replace(quote, quote+quote)
                vals.append(f'"{v}"')
            buf.write(','.join(vals) + '\n')
        csv_text = buf.getvalue()
        buf.close()
        return csv_text, 200, {'Content-Type': 'text/csv; charset=utf-8', 'Content-Disposition': 'attachment; filename=tasks.csv'}
    return jsonify(data)

@app.route('/api/analytics')
def api_analytics():
    if not DB_EXISTS:
        return jsonify({'total': 0, 'done': 0, 'stale': 0, 'running': 0, 'error': 0,
            'completion_rate': 0, 'recent_completed': 0, 'avg_failures': 0, 'health': 0, 'db_missing': True})
    conn = db_conn()
    c = conn.cursor()
    total = c.execute("SELECT COUNT(1) FROM tasks").fetchone()[0]
    done = c.execute("SELECT COUNT(1) FROM tasks WHERE status='done'").fetchone()[0]
    stale = c.execute("SELECT COUNT(1) FROM tasks WHERE status='stale'").fetchone()[0]
    running = c.execute("SELECT COUNT(1) FROM tasks WHERE status='running'").fetchone()[0]
    error = c.execute("SELECT COUNT(1) FROM tasks WHERE status='error'").fetchone()[0]
    recent_completed = c.execute("SELECT COUNT(1) FROM tasks WHERE status='done' AND completed_at > ?", (int(time.time()) - 86400,)).fetchone()[0]
    avg_failures = c.execute("SELECT AVG(consecutive_failures) FROM tasks WHERE consecutive_failures > 0").fetchone()[0] or 0
    conn.close()
    completion_rate = round(done / total * 100, 1) if total else 0
    health = max(0, min(100, round(
        (completion_rate * 0.4) + (max(0, 100 - stale * 5) * 0.3) + (recent_completed * 5 * 0.3)
    )))
    return jsonify({
        'total': total, 'done': done, 'stale': stale, 'running': running, 'error': error,
        'completion_rate': completion_rate, 'recent_completed': recent_completed,
        'avg_failures': round(avg_failures, 1), 'health': health
    })

@app.route('/api/search')
def api_search():
    q = request.args.get('q', '').strip().lower()
    if not q:
        return jsonify({'tasks': [], 'files': [], 'workers': []})
    conn = db_conn()
    c = conn.cursor()
    tasks = c.execute("""SELECT id, title, assignee, status FROM tasks
        WHERE LOWER(title) LIKE ? OR LOWER(id) LIKE ? OR LOWER(COALESCE(assignee,'')) LIKE ?
        LIMIT 20""", (f'%{q}%', f'%{q}%', f'%{q}%')).fetchall()
    workers = c.execute("""SELECT DISTINCT r.profile, r.status as run_status, t.title
        FROM task_runs r LEFT JOIN tasks t ON t.id = r.task_id
        WHERE LOWER(r.profile) LIKE ? AND r.profile != ''
        LIMIT 10""", (f'%{q}%',)).fetchall()
    conn.close()
    files = []
    ql = q.lower()
    for f in _get_file_index():
        if ql in f['name'].lower():
            files.append(f)
            if len(files) >= 10: break
    return jsonify({
        'tasks': [dict(r) for r in tasks],
        'files': files,
        'workers': [dict(r) for r in workers],
    })

@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)

LOGIN_PAGE = r"""<!DOCTYPE html>
<html lang="vi" data-bs-theme="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Login — Monitor</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
<link href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.min.css" rel="stylesheet">
<style>
:root {
  --bg: #08080f; --surface: #111127; --border: #252550; --border2: #35356a;
  --text: #d0d0ec; --text2: #7878aa; --accent: #818cf8; --accent2: #6366f1;
  --red: #f87171; --radius: 10px; --radius-lg: 14px; --shadow-lg: 0 8px 28px rgba(0,0,0,.45);
  --font-sans: 'Inter', 'Segoe UI', system-ui, sans-serif;
}
[data-bs-theme="light"] {
  --bg: #f5f5fa; --surface: #ffffff; --border: #d4d4e0; --border2: #b8b8cc;
  --text: #1a1a2e; --text2: #6b6b8a; --accent: #6366f1; --accent2: #4f46e5;
}
body { background: var(--bg); color: var(--text); font-family: var(--font-sans);
  display: flex; align-items: center; justify-content: center; min-height: 100vh; margin: 0; }
.login-card {
  background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius-lg);
  padding: 2.5rem 2rem; width: 380px; box-shadow: var(--shadow-lg); text-align: center;
}
.login-card h3 { font-weight: 700; font-size: 1.2rem; letter-spacing: -.3px; margin-bottom: .25rem;
  background: linear-gradient(135deg, var(--text), var(--accent)); -webkit-background-clip: text;
  -webkit-text-fill-color: transparent; background-clip: text; }
.login-card .sub { font-size: .75rem; color: var(--text2); margin-bottom: 1.5rem; }
.form-group { margin-bottom: 1rem; text-align: left; }
.form-group label { font-size: .7rem; color: var(--text2); display: block; margin-bottom: 3px; }
.form-control {
  background: rgba(255,255,255,.04); border: 1px solid var(--border); border-radius: var(--radius);
  padding: .55rem .75rem; color: var(--text); font-size: .85rem; width: 100%; box-sizing: border-box;
  transition: .2s; outline: none;
}
.form-control:focus { border-color: var(--accent); box-shadow: 0 0 0 3px rgba(129,140,248,.12); }
.form-control::placeholder { color: var(--text2); }
.btn-login {
  width: 100%; padding: .6rem; border: none; border-radius: var(--radius);
  background: var(--accent); color: #fff; font-size: .85rem; font-weight: 600;
  cursor: pointer; transition: .2s; margin-top: .5rem;
}
.btn-login:hover { background: var(--accent2); transform: translateY(-1px); }
.btn-login:disabled { opacity: .5; cursor: not-allowed; }
.error-msg { color: var(--red); font-size: .72rem; margin-top: .75rem; display: none; }
</style>
</head>
<body>
<div class="login-card">
  <h3>Monitor</h3>
  <div class="sub">Hermes Dashboard</div>
  <div class="form-group">
    <label>Tên đăng nhập</label>
    <input type="text" class="form-control" id="username" placeholder="admin" autocomplete="username" onkeydown="if(event.key==='Enter')doLogin()">
  </div>
  <div class="form-group">
    <label>Mật khẩu</label>
    <input type="password" class="form-control" id="password" placeholder="········" autocomplete="current-password" onkeydown="if(event.key==='Enter')doLogin()">
  </div>
  <button class="btn-login" id="loginBtn" onclick="doLogin()">Đăng nhập</button>
  <div class="error-msg" id="loginError"></div>
</div>
<script>
var themeEl = document.documentElement;
var saved = localStorage.getItem('theme');
var prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
themeEl.setAttribute('data-bs-theme', saved || (prefersDark ? 'dark' : 'light'));

async function doLogin() {
  var u = document.getElementById('username').value.trim();
  var p = document.getElementById('password').value;
  if (!u || !p) return;
  var btn = document.getElementById('loginBtn');
  var err = document.getElementById('loginError');
  btn.disabled = true; btn.textContent = 'Đang đăng nhập...'; err.style.display = 'none';
  try {
    var r = await fetch('/login', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({username:u, password:p})});
    var d = await r.json();
    if (d.ok) { location.reload(); }
    else { err.textContent = d.message || 'Lỗi'; err.style.display = 'block'; }
  } catch(e) { err.textContent = 'Lỗi kết nối'; err.style.display = 'block'; }
  btn.disabled = false; btn.textContent = 'Đăng nhập';
}
</script>
</body>
</html>"""

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

  /* Toast */
  --toast-bg: rgba(17, 17, 39, 0.92);
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

  /* Toast */
  --toast-bg: rgba(255, 255, 255, 0.92);
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
  background: var(--toast-bg);
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

/* === Workers Grid === */
.worker-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
  gap: .75rem;
  margin-bottom: .25rem;
}

.worker-card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  padding: 1rem 1.1rem;
  transition: var(--transition-base);
  animation: fadeUp .35s ease backwards;
}

.worker-card:hover {
  border-color: var(--border2);
  box-shadow: var(--shadow-sm);
}

.worker-card .wc-head {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: .6rem;
}

.worker-card .wc-name {
  font-weight: 600;
  font-size: .85rem;
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.worker-card .wc-row {
  display: flex;
  justify-content: space-between;
  font-size: .7rem;
  color: var(--text2);
  padding: 2px 0;
}

.worker-card .wc-row strong {
  color: var(--text);
  font-weight: 500;
}

.worker-dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  flex-shrink: 0;
}

.worker-dot.running { background: var(--green); box-shadow: 0 0 8px var(--green); }
.worker-dot.offline { background: var(--text3); }
.worker-dot.idle { background: var(--yellow); box-shadow: 0 0 8px var(--yellow); }

/* === File Viewer === */
.file-path {
  font-size: .68rem;
  color: var(--text3);
  max-width: 320px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.file-size {
  font-size: .7rem;
  color: var(--text2);
}

.file-preview-modal .modal-dialog { max-width: 1200px; }

.file-preview-body {
  font-family: var(--font-mono);
  font-size: .76rem;
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 70vh;
  overflow-y: auto;
  background: var(--bg2);
  padding: 1rem;
  border-radius: var(--radius-sm);
  border: 1px solid var(--border);
  line-height: 1.65;
}

/* === Inline Edit Styles === */
.editable-field {
  cursor: pointer;
  border-bottom: 1px dashed var(--border2);
  transition: var(--transition-fast);
}

.editable-field:hover {
  border-color: var(--accent);
  color: var(--accent);
}

.edit-inline {
  background: var(--bg2);
  border: 1px solid var(--accent);
  border-radius: var(--radius-sm);
  padding: 2px 6px;
  font-size: .78rem;
  color: var(--text);
  font-family: var(--font-sans);
  outline: none;
  width: auto;
  min-width: 100px;
}

.edit-inline:focus {
  box-shadow: 0 0 0 2px var(--accent-glow);
}

.edit-inline-select {
  background: var(--bg2);
  border: 1px solid var(--accent);
  border-radius: var(--radius-sm);
  padding: 2px 6px;
  font-size: .78rem;
  color: var(--text);
  outline: none;
}

.edit-save-btn {
  background: var(--accent);
  color: #fff;
  border: none;
  border-radius: 4px;
  padding: 1px 8px;
  font-size: .68rem;
  cursor: pointer;
  margin-left: 4px;
  transition: var(--transition-fast);
}

.edit-save-btn:hover {
  opacity: .85;
}

.edit-cancel-btn {
  background: transparent;
  color: var(--text2);
  border: 1px solid var(--border2);
  border-radius: 4px;
  padding: 1px 8px;
  font-size: .68rem;
  cursor: pointer;
  margin-left: 2px;
  transition: var(--transition-fast);
}

.edit-cancel-btn:hover {
  color: var(--text);
  border-color: var(--text3);
}

/* === Form Controls for modals === */
.form-control:focus {
  box-shadow: 0 0 0 2px var(--accent-glow);
  border-color: var(--accent);
}

.form-control::placeholder {
  color: var(--text3);
  font-size: .78rem;
}

/* === Modal Footer === */
.modal-footer {
  border-top: 1px solid var(--border-light);
  padding: .75rem 1.25rem;
}

/* === Create Task Animation === */
#createTaskBtn:disabled {
  opacity: .5;
  pointer-events: none;
}

/* === Skeleton Loading === */
.skeleton {
  background: linear-gradient(90deg, var(--surface2) 25%, var(--surface3) 50%, var(--surface2) 75%);
  background-size: 200% 100%;
  animation: shimmer 1.4s infinite;
  border-radius: var(--radius-sm);
}
@keyframes shimmer { 0%{background-position:200% 0} 100%{background-position:-200% 0} }
.skeleton-h { height: .75rem; margin: 3px 0; }
.skeleton-row { margin: .5rem 0; }
.skeleton-card { height: 80px; border-radius: var(--radius-lg); margin: 0; }
.skeleton-badge { display: inline-block; width: 60px; height: 18px; border-radius: 50px; }

/* === Analytics Cards === */
.analytics-row {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: .75rem;
  margin-bottom: 1.25rem;
}
.analytics-card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  padding: .9rem 1rem;
  display: flex;
  align-items: center;
  gap: .75rem;
  transition: var(--transition-base);
}
.analytics-card:hover { border-color: var(--border2); box-shadow: var(--shadow-sm); }
.analytics-mini-val { font-size: 1.4rem; font-weight: 800; letter-spacing: -.5px; line-height: 1; }
.analytics-mini-label { font-size: .65rem; color: var(--text3); text-transform: uppercase; letter-spacing: .4px; margin-top: 2px; }
.health-ring {
  width: 42px; height: 42px; border-radius: 50%;
  display: flex; align-items: center; justify-content: center;
  font-weight: 700; font-size: .72rem; flex-shrink: 0;
}

/* === Search Dropdown === */
.search-dropdown {
  position: absolute;
  top: 100%;
  left: 0;
  right: 0;
  background: var(--surface);
  border: 1px solid var(--border2);
  border-radius: var(--radius-sm);
  margin-top: 4px;
  box-shadow: var(--shadow-lg);
  max-height: 400px;
  overflow-y: auto;
  z-index: var(--z-dropdown);
  display: none;
}
.search-dropdown.show { display: block; }
.search-result-group { padding: .35rem .6rem; font-size: .62rem; color: var(--text3); text-transform: uppercase; letter-spacing: .6px; font-weight: 600; background: var(--bg2); border-bottom: 1px solid var(--border-light); }
.search-result-item {
  padding: .45rem .75rem;
  cursor: pointer;
  font-size: .76rem;
  border-bottom: 1px solid var(--border-light);
  transition: var(--transition-fast);
  display: flex;
  align-items: center;
  gap: 6px;
}
.search-result-item:hover { background: var(--accent-subtle); }
.search-result-item:last-child { border-bottom: none; }
.search-no-result { padding: .75rem; text-align: center; color: var(--text3); font-size: .72rem; }

/* === Filter Bar === */
.filter-bar { display: flex; gap: .4rem; flex-wrap: wrap; align-items: center; margin-bottom: .75rem; }
.filter-chip {
  font-size: .65rem; padding: .2em .55em; border-radius: 50px;
  border: 1px solid var(--border2); background: var(--surface2); color: var(--text2);
  cursor: pointer; transition: var(--transition-fast); display: flex; align-items: center; gap: 4px;
  user-select: none;
}
.filter-chip:hover { border-color: var(--accent); color: var(--accent); background: var(--accent-subtle); }
.filter-chip.active { background: var(--accent); color: #fff; border-color: var(--accent); }
.filter-chip .fc-remove { font-size: .6rem; margin-left: 2px; opacity: .6; }
.filter-chip.active .fc-remove { opacity: 1; }

/* === Task Action Buttons === */
.task-actions-bar {
  display: flex; gap: .4rem; flex-wrap: wrap; margin-bottom: .75rem;
  padding: .5rem .6rem; background: var(--bg2); border-radius: var(--radius-sm);
  border: 1px solid var(--border-light); align-items: center;
}
.task-actions-bar .ta-label { font-size: .62rem; color: var(--text3); text-transform: uppercase; letter-spacing: .5px; margin-right: .3rem; }

/* === Cron Toggle === */
.cron-toggle {
  display: flex; align-items: center; gap: 6px;
}
.cron-status-led {
  width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0;
}
.cron-status-led.enabled { background: var(--green); box-shadow: 0 0 6px var(--green); }
.cron-status-led.disabled { background: var(--text3); }

/* === Export Buttons === */
.export-section { display: flex; gap: .35rem; margin-left: auto; }

/* === Health bar === */
.health-bar-wrap { height: 6px; background: var(--surface3); border-radius: 3px; margin-top: 4px; overflow: hidden; }
.health-bar-fill { height: 100%; border-radius: 3px; transition: width .6s ease; }

/* === Task action btn === */
.ta-btn {
  font-size: .65rem; padding: .18rem .5rem; border-radius: 50px; cursor: pointer;
  transition: var(--transition-fast); border: 1px solid transparent;
  display: flex; align-items: center; gap: 3px;
}
.ta-btn-claim { background: var(--blue-subtle); color: var(--blue); border-color: var(--blue); }
.ta-btn-claim:hover { background: var(--blue); color: #fff; }
.ta-btn-enqueue { background: var(--accent-subtle); color: var(--accent); border-color: var(--accent2); }
.ta-btn-enqueue:hover { background: var(--accent); color: #fff; }
.ta-btn-complete { background: var(--green-subtle); color: var(--green); border-color: var(--green); }
.ta-btn-complete:hover { background: var(--green); color: #fff; }

/* === Mobile Polish === */
@media (max-width: 992px) {
  .analytics-row { grid-template-columns: repeat(3, 1fr); }
  .kanban-col { flex: 0 0 220px; }
}
@media (max-width: 768px) {
  .analytics-row { grid-template-columns: repeat(2, 1fr); gap: .5rem; }
  .analytics-mini-val { font-size: 1.1rem; }
  .stat-wrap { grid-template-columns: repeat(2, 1fr); }
  .search-box { width: 140px; }
  .search-box:focus { width: 180px; }
  .worker-grid { grid-template-columns: 1fr; }
  .filter-bar { gap: .25rem; }
  .app-main { padding: .75rem 1rem; }
  .app-header { padding: .6rem 1rem; gap: .4rem; }
  .app-header h5 { font-size: .85rem; }
  .nav-tabs .nav-link { font-size: .7rem; padding: .5rem .7rem; }
  .table { font-size: .72rem; }
  .modal-xl .modal-dialog { max-width: 98vw; }
}
@media (max-width: 576px) {
  .stat-wrap { grid-template-columns: 1fr; gap: .5rem; }
  .analytics-row { grid-template-columns: 1fr; gap: .4rem; }
  .search-box { width: 100px; }
  .search-box:focus { width: 140px; }
  .app-header { flex-wrap: wrap; gap: .4rem; padding: .5rem .8rem; }
  .toast-container { left: .5rem; right: .5rem; max-width: none; }
  .worker-grid { grid-template-columns: 1fr; }
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
        <input type="text" class="search-box" id="searchInput" placeholder="Tìm toàn bộ..." autocomplete="off">
        <span class="kbd" id="searchKbd">Ctrl K</span>
        <div class="search-dropdown" id="searchDropdown"></div>
      </div>
      <button class="icon-btn" onclick="openCreateTaskModal()" title="Tạo task mới" data-bs-toggle="tooltip" data-bs-placement="bottom"><i class="bi bi-plus-lg"></i></button>
      <button class="icon-btn" onclick="loadDashboard()" title="Refresh" data-bs-toggle="tooltip" data-bs-placement="bottom"><i class="bi bi-arrow-clockwise"></i></button>
      <span id="healthIndicator" style="display:none;font-size:.65rem;color:var(--text2);display:inline-flex;gap:8px;align-items:center">
        <span onclick="showSystemHealth()" style="cursor:pointer" title="CPU"><i class="bi bi-cpu"></i> <span id="cpuVal">—</span>%</span>
        <span onclick="showSystemHealth()" style="cursor:pointer" title="RAM"><i class="bi bi-memory"></i> <span id="ramVal">—</span>%</span>
        <span onclick="showSystemHealth()" style="cursor:pointer" title="Disk"><i class="bi bi-hdd"></i> <span id="diskVal">—</span>%</span>
      </span>
      <div style="display:flex;align-items:center;gap:4px;font-size:.62rem;color:var(--text3)">
        <i class="bi bi-arrow-repeat" style="font-size:.7rem"></i>
        <select id="autoRefreshSelect" onchange="setAutoRefresh(this.value)" style="background:transparent;border:none;color:var(--text3);font-size:.62rem;outline:none;cursor:pointer">
          <option value="0">Tắt</option>
          <option value="5">5s</option>
          <option value="10" selected>10s</option>
          <option value="30">30s</option>
          <option value="60">60s</option>
        </select>
      </div>
      <span class="export-section">
        <button class="btn btn-sm btn-outline-secondary" onclick="exportTasks('csv')" title="Xuất CSV" style="font-size:.65rem"><i class="bi bi-download me-1"></i>CSV</button>
        <button class="btn btn-sm btn-outline-secondary" onclick="exportTasks('json')" title="Xuất JSON" style="font-size:.65rem"><i class="bi bi-download me-1"></i>JSON</button>
      </span>
      <button class="icon-btn" id="themeToggle" onclick="toggleTheme()" title="Chế độ sáng/tối" data-bs-toggle="tooltip" data-bs-placement="bottom"><i class="bi bi-moon-stars"></i></button>
      <button class="icon-btn" id="langToggle" onclick="switchLang(currentLang==='vi'?'en':'vi')" title="Language" data-bs-toggle="tooltip" data-bs-placement="bottom" style="width:auto;padding:0 8px;font-size:.68rem;font-weight:600"><i class="bi bi-translate"></i> VI</button>
      <button class="icon-btn" onclick="location.href='/logout'" title="Đăng xuất" data-bs-toggle="tooltip" data-bs-placement="bottom"><i class="bi bi-box-arrow-right"></i></button>
    </div>
  </div>

  <div class="app-main">
    <ul class="nav nav-tabs" id="mainTabs">
      <li class="nav-item"><button class="nav-link active" id="tab-system" data-bs-toggle="tab" data-bs-target="#pane-system"><i class="bi bi-cpu"></i>Hệ thống</button></li>
      <li class="nav-item"><button class="nav-link" id="tab-kanban" data-bs-toggle="tab" data-bs-target="#pane-kanban"><i class="bi bi-columns-gap"></i>Kanban</button></li>
      <li class="nav-item"><button class="nav-link" id="tab-cron" data-bs-toggle="tab" data-bs-target="#pane-cron"><i class="bi bi-alarm"></i>Cron <span class="badge bg-secondary ms-1" id="cronBadge">0</span></button></li>
      <li class="nav-item"><button class="nav-link" id="tab-outputs" data-bs-toggle="tab" data-bs-target="#pane-outputs"><i class="bi bi-file-text"></i>Outputs</button></li>
      <li class="nav-item"><button class="nav-link" id="tab-workers" data-bs-toggle="tab" data-bs-target="#pane-workers"><i class="bi bi-people"></i>Workers</button></li>
      <li class="nav-item"><button class="nav-link" id="tab-files" data-bs-toggle="tab" data-bs-target="#pane-files"><i class="bi bi-folder"></i>Files</button></li>
      <li class="nav-item"><button class="nav-link" id="tab-conversations" data-bs-toggle="tab" data-bs-target="#pane-conversations"><i class="bi bi-chat-dots"></i>Hội thoại</button></li>
    </ul>

    <div class="tab-content mt-3">
      <!-- System -->
      <div class="tab-pane fade show active" id="pane-system">
        <div class="stat-wrap" id="statCards"></div>
        <span id="analyticsToggle" onclick="toggleAnalytics()" style="cursor:pointer;font-size:.65rem;color:var(--text3);user-select:none;display:none;margin-bottom:2px">
          <i class="bi bi-chevron-up"></i> Thu gọn analytics
        </span>
        <div class="analytics-row" id="analyticsRow"></div>
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
          <ul class="nav nav-tabs border-0 gap-0" id="taskSubTabs" style="font-size:.72rem">
            <li class="nav-item"><button class="nav-link active" id="subtab-stale" onclick="switchSubTab('stale')"><i class="bi bi-exclamation-triangle"></i>Treo <span class="badge bg-danger ms-1" id="staleCountBadge">0</span></button></li>
            <li class="nav-item"><button class="nav-link" id="subtab-all" onclick="switchSubTab('all')"><i class="bi bi-list-task"></i>Tất cả <span class="badge bg-secondary ms-1" id="allTaskCountBadge">0</span></button></li>
            <li class="nav-item"><button class="nav-link" id="subtab-done" onclick="switchSubTab('done')"><i class="bi bi-check-circle"></i>Xong <span class="badge bg-secondary ms-1" id="doneTaskCountBadge">0</span></button></li>
          </ul>
          <div class="d-flex gap-2">
            <button class="btn btn-sm btn-outline-warning d-none" id="killSelectedBtn" onclick="killSelected()"><i class="bi bi-x-lg me-1"></i>Kill đã chọn (<span id="selectedCount">0</span>)</button>
            <button class="btn btn-sm btn-outline-danger d-none" id="deleteSelectedBtn" onclick="deleteSelected()"><i class="bi bi-trash3 me-1"></i>Xoá đã chọn (<span id="deleteSelectedCount">0</span>)</button>
            <button class="btn btn-sm btn-outline-danger" id="killAllBtn" onclick="killAllDead()"><i class="bi bi-trash3 me-1"></i>Kill all</button>
          </div>
        </div>
        <div>
          <div class="sub-pane" id="pane-sub-stale" style="display:block">
            <div class="table-wrap"><table class="table"><thead><tr><th style="width:28px"><input type="checkbox" id="selectAll" onchange="toggleSelectAll()"></th><th>ID</th><th>Tiêu đề</th><th>Người phụ trách</th><th style="width:50px">PID</th><th style="width:55px">Age</th><th style="width:80px">Lý do</th><th style="width:110px"></th></tr></thead><tbody id="staleTable"></tbody></table></div>
          </div>
          <div class="sub-pane" id="pane-sub-all" style="display:none">
            <div class="d-flex gap-2 mb-2 align-items-center">
              <select id="allTaskFilter" onchange="loadAllTasks()" style="font-size:.72rem;background:var(--surface2);border:1px solid var(--border);color:var(--text);border-radius:var(--radius-sm);padding:.2rem .5rem">
                <option value="">Tất cả trạng thái</option>
                <option value="ready">Sẵn sàng</option>
                <option value="running">Đang chạy</option>
                <option value="blocked">Chặn</option>
                <option value="stale">Treo</option>
                <option value="done">Xong</option>
                <option value="error">Lỗi</option>
                <option value="killed">Đã kill</option>
              </select>
              <span style="font-size:.65rem;color:var(--text3)" id="allTaskInfo"></span>
            </div>
            <div class="table-wrap" style="max-height:500px;overflow-y:auto"><table class="table"><thead style="position:sticky;top:0;z-index:2;background:var(--surface2)"><tr><th style="width:28px"><input type="checkbox" onchange="toggleAllCheckboxes(this,'allTaskCheck')"></th><th onclick="sortAllTasks('created_at')" style="cursor:pointer">Tạo lúc <i class="bi bi-arrow-down-up" style="font-size:.6rem"></i></th><th onclick="sortAllTasks('title')" style="cursor:pointer">Tiêu đề</th><th onclick="sortAllTasks('assignee')" style="cursor:pointer">Người phụ trách</th><th onclick="sortAllTasks('status')" style="cursor:pointer">Trạng thái</th><th onclick="sortAllTasks('started_at')" style="cursor:pointer">Bắt đầu</th></tr></thead><tbody id="allTaskTable"></tbody></table></div>
          </div>
          <div class="sub-pane" id="pane-sub-done" style="display:none">
            <div class="table-wrap"><table class="table"><thead><tr><th style="width:28px"><input type="checkbox" onchange="toggleAllCheckboxes(this,'doneTaskCheck')"></th><th>Tiêu đề</th><th>Người phụ trách</th><th>Xong lúc</th></tr></thead><tbody id="doneTaskTable"></tbody></table></div>
          </div>
        </div>
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
        <div class="table-wrap"><table class="table"><thead><tr><th>Tên</th><th>Lịch</th><th>Lần chạy tới</th><th>Lần cuối</th><th>Trạng thái</th><th>Lỗi</th><th style="width:60px">On/Off</th></tr></thead><tbody id="cronTable"></tbody></table></div>
      </div>

      <!-- Outputs -->
      <div class="tab-pane fade" id="pane-outputs">
        <div class="d-flex justify-content-between align-items-center mb-2">
          <div class="sec-title m-0"><i class="bi bi-file-text"></i>Kết quả tác vụ <span class="badge bg-secondary ms-1" id="outputCountBadge">0</span></div>
          <span><span class="refresh-dot" id="outputRefreshDot"></span><small class="text-secondary" style="font-size:.7rem" id="outputRefreshLabel"></small></span>
        </div>
        <div class="table-wrap"><table class="table"><thead><tr><th style="width:36px">STT</th><th>Tiêu đề</th><th>Người phụ trách</th><th style="width:70px">Trạng thái</th><th style="width:120px">Thời gian</th></tr></thead><tbody id="outputTable"></tbody></table></div>
      </div>

      <!-- Workers -->
      <div class="tab-pane fade" id="pane-workers">
        <div class="d-flex justify-content-between align-items-center mb-2">
          <div class="sec-title m-0"><i class="bi bi-people"></i>Worker Agents</div>
          <span><span class="refresh-dot" id="workerRefreshDot"></span><small class="text-secondary" style="font-size:.7rem" id="workerRefreshLabel"></small></span>
        </div>
        <div id="workerGrid" class="worker-grid"></div>
        <div class="table-wrap mt-3"><table class="table"><thead><tr><th>Profile</th><th>PID</th><th>Status</th><th>Task hiện tại</th><th>Tổng task</th><th>Hoạt động gần nhất</th></tr></thead><tbody id="workerTable"></tbody></table></div>
      </div>

      <!-- Files -->
      <div class="tab-pane fade" id="pane-files">
        <div class="d-flex justify-content-between align-items-center mb-2">
          <div class="sec-title m-0"><i class="bi bi-folder"></i>File Viewer</div>
          <span><span class="refresh-dot" id="fileRefreshDot"></span><small class="text-secondary" style="font-size:.7rem" id="fileRefreshLabel"></small> <button class="btn btn-sm btn-outline-secondary py-0 px-2" onclick="openSettings()" style="font-size:.65rem"><i class="bi bi-gear"></i></button></span>
        </div>
        <div id="vaultConfigPrompt" style="display:none;margin-bottom:.75rem;padding:.6rem .8rem;background:var(--yellow-subtle);border:1px solid var(--yellow);border-radius:var(--radius-sm);font-size:.72rem;color:var(--yellow)">
          <i class="bi bi-info-circle"></i> Vault chưa được cấu hình. <a href="#" onclick="openSettings();return false" style="color:var(--accent);text-decoration:underline">Cấu hình ngay</a> để xem file .md.
        </div>
        <div class="table-wrap"><table class="table"><thead><tr><th style="width:36px">STT</th><th>Tên file</th><th>Đường dẫn</th><th style="width:80px">Kích thước</th><th style="width:120px">Sửa đổi</th><th style="width:80px"></th></tr></thead><tbody id="fileTable"></tbody></table></div>
      </div>

      <!-- Conversations -->
      <div class="tab-pane fade" id="pane-conversations">
        <div class="d-flex justify-content-between align-items-center mb-2">
          <div class="sec-title m-0"><i class="bi bi-chat-dots"></i>Hội thoại Agent</div>
          <span><span class="refresh-dot" id="convRefreshDot"></span><small class="text-secondary" style="font-size:.7rem" id="convRefreshLabel"></small></span>
        </div>
        <div class="table-wrap"><table class="table"><thead><tr><th>Profile</th><th>Tiêu đề</th><th>Model</th><th style="width:60px">Messages</th><th style="width:50px">Tokens</th><th style="width:70px">Cost</th><th style="width:100px">Thời gian</th><th style="width:40px"></th></tr></thead><tbody id="conversationTable"></tbody></table></div>
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

<div class="modal fade" id="createTaskModal" tabindex="-1"><div class="modal-dialog modal-dialog-centered"><div class="modal-content">
  <div class="modal-header"><h5 class="modal-title"><i class="bi bi-plus-circle me-1"></i>Tạo task mới</h5><button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal"></button></div>
  <div class="modal-body">
    <div class="mb-3">
      <label class="form-label" style="font-size:.75rem;color:var(--text2)">Tiêu đề <span style="color:var(--red)">*</span></label>
      <input type="text" class="form-control" id="createTaskTitle" placeholder="Nhập tiêu đề task..." style="background:var(--bg2);border-color:var(--border);color:var(--text);font-size:.82rem">
    </div>
    <div class="mb-3">
      <label class="form-label" style="font-size:.75rem;color:var(--text2)">Người phụ trách</label>
      <input type="text" class="form-control" id="createTaskAssignee" placeholder="Tên người phụ trách..." list="workerDatalist" style="background:var(--bg2);border-color:var(--border);color:var(--text);font-size:.82rem">
      <datalist id="workerDatalist"></datalist>
    </div>
    <div class="mb-3">
      <label class="form-label" style="font-size:.75rem;color:var(--text2)">Mô tả</label>
      <textarea class="form-control" id="createTaskDesc" rows="3" placeholder="Mô tả chi tiết..." style="background:var(--bg2);border-color:var(--border);color:var(--text);font-size:.82rem;resize:vertical"></textarea>
    </div>
  </div>
  <div class="modal-footer" style="border-color:var(--border-light);padding:.75rem 1.25rem">
    <button class="btn btn-sm btn-outline-secondary" data-bs-dismiss="modal">Huỷ</button>
    <button class="btn btn-sm btn-primary" id="createTaskBtn" onclick="submitCreateTask()"><i class="bi bi-check-lg me-1"></i>Tạo task</button>
  </div>
</div></div></div>

<div class="modal fade" id="deleteTaskModal" tabindex="-1"><div class="modal-dialog modal-dialog-centered"><div class="modal-content">
  <div class="modal-header" style="border-bottom-color:var(--red)"><h5 class="modal-title"><i class="bi bi-exclamation-triangle-fill" style="color:var(--red)"></i> Xoá task</h5><button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal"></button></div>
  <div class="modal-body">
    <p style="color:var(--red);font-size:.82rem">Task sẽ bị xoá vĩnh viễn! Hành động này <strong>không thể hoàn tác</strong>.</p>
    <p style="font-size:.72rem;color:var(--text2)">Gõ <strong>CONFIRM</strong> để xác nhận:</p>
    <input type="text" class="form-control" id="deleteConfirmInput" placeholder="Gõ CONFIRM..." style="background:var(--bg2);border-color:var(--border);color:var(--text);font-size:.9rem;text-align:center;letter-spacing:3px;text-transform:uppercase" oninput="document.getElementById('deleteTaskBtn').disabled=this.value!=='CONFIRM'">
  </div>
  <div class="modal-footer" style="border-color:var(--border-light)">
    <button class="btn btn-sm btn-outline-secondary" data-bs-dismiss="modal">Huỷ</button>
    <button class="btn btn-sm btn-outline-danger" id="deleteTaskBtn" disabled onclick="confirmDeleteTask()"><i class="bi bi-trash3 me-1"></i> Xoá vĩnh viễn</button>
  </div>
</div></div></div>

<div class="modal fade" id="systemHealthModal" tabindex="-1"><div class="modal-dialog modal-dialog-centered modal-sm"><div class="modal-content">
  <div class="modal-header"><h5 class="modal-title"><i class="bi bi-activity me-1"></i>System Health</h5><button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal"></button></div>
  <div class="modal-body" id="systemHealthBody"></div>
</div></div></div>

<div class="modal fade" id="conversationModal" tabindex="-1"><div class="modal-dialog modal-xl modal-dialog-centered modal-dialog-scrollable"><div class="modal-content">
  <div class="modal-header"><h5 class="modal-title" id="convModalTitle"></h5><button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal"></button></div>
  <div class="modal-body" id="convModalBody"></div>
</div></div></div>

<div class="modal fade" id="settingsModal" tabindex="-1"><div class="modal-dialog modal-dialog-centered"><div class="modal-content">
  <div class="modal-header"><h5 class="modal-title"><i class="bi bi-gear me-1"></i>Cấu hình</h5><button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal"></button></div>
  <div class="modal-body">
    <div class="mb-3">
      <label class="form-label" style="font-size:.75rem;color:var(--text2)">Đường dẫn Obsidian Vault</label>
      <input type="text" class="form-control" id="settingsVaultDir" placeholder="C:\Users\...\Documents\Vault" style="background:var(--bg2);border-color:var(--border);color:var(--text);font-size:.82rem">
      <small style="font-size:.65rem;color:var(--text3)">Để trống nếu không dùng vault. Cấu trúc: {dir}/Efforts/*.md</small>
    </div>
  </div>
  <div class="modal-footer" style="border-color:var(--border-light);padding:.75rem 1.25rem">
    <button class="btn btn-sm btn-outline-secondary" data-bs-dismiss="modal">Huỷ</button>
    <button class="btn btn-sm btn-primary" id="saveSettingsBtn" onclick="saveVaultSettings()"><i class="bi bi-check-lg me-1"></i>Lưu</button>
  </div>
</div></div></div>

<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script>
<script>
const API = '/api/dashboard';
let cronTimer = null;

// === i18n Language ===
const LANG = {
  vi: {
    tab_system:'Hệ thống', tab_kanban:'Kanban', tab_cron:'Cron', tab_outputs:'Outputs',
    tab_workers:'Workers', tab_files:'Files', tab_conversations:'Hội thoại',
    subtab_stale:'Treo', subtab_all:'Tất cả', subtab_done:'Xong',
    stat_total:'Tổng tasks', stat_done:'Đã hoàn thành', stat_stale:'Treo / Đang chạy', stat_cron:'Cron lỗi',
    sec_title_recent:'Hoạt động gần đây', sec_title_dist:'Phân bố', sec_title_stale:'Tác vụ treo',
    sec_title_kanban:'Kanban theo trạng thái', sec_title_cron:'Lịch trình Cron',
    sec_title_outputs:'Kết quả tác vụ', sec_title_workers:'Worker Agents',
    sec_title_files:'File Viewer', sec_title_conv:'Hội thoại Agent',
    health_score:'Health Score', health_complete:'Hoàn thành', health_stale:'Treo', health_running:'Đang chạy',
    filter_kill_sel:'Kill đã chọn', filter_del_sel:'Xoá đã chọn', filter_kill_all:'Kill all',
    all_status:'Tất cả trạng thái', all_info:'tasks',
    btn_create:'Tạo task mới', btn_export_csv:'CSV', btn_export_json:'JSON',
    btn_claim:'Claim', btn_enqueue:'Enqueue', btn_complete:'Complete', btn_retry:'Retry', btn_delete:'Delete',
    btn_save:'Lưu', btn_cancel:'Huỷ', btn_ok:'OK',
    modal_output:'Output', modal_events:'Sự kiện', modal_runs:'Lần chạy', modal_notes:'Notes',
    modal_delete_title:'Xoá task', modal_delete_warn:'Task sẽ bị xoá vĩnh viễn! Hành động này không thể hoàn tác.',
    modal_delete_prompt:'Gõ CONFIRM để xác nhận:', modal_delete_confirm:'Gõ CONFIRM...',
    modal_delete_btn:'Xoá vĩnh viễn', modal_delete_deleting:'Đang xoá...',
    modal_create_title:'Tạo task mới', modal_create_title_label:'Tiêu đề', modal_create_assignee:'Người phụ trách',
    modal_create_desc:'Mô tả', modal_create_btn:'Tạo task',
    modal_health_title:'System Health',
    modal_conv_title:'Hội thoại',
    toolbar_rendered:'Hiển thị', toolbar_raw:'Raw', toolbar_copy:'Copy', toolbar_edit:'Sửa', toolbar_fullscreen:'Toàn màn hình',
    action_bar_label:'Hành động:',
    notes_help:'Thêm ghi chú cho task này (lưu vào body field)', notes_save:'Lưu notes',
    worker_status_run:'Đang chạy', worker_status_off:'Ngoại tuyến', worker_pid:'PID', worker_task:'Task hiện tại',
    worker_total:'Tổng task', worker_active:'Hoạt động gần nhất',
    conv_profile:'Profile', conv_title:'Tiêu đề', conv_model:'Model', conv_msgs:'Messages', conv_tokens:'Tokens',
    conv_cost:'Cost', conv_time:'Thời gian',
    file_header_stt:'STT', file_header_name:'Tên file', file_header_path:'Đường dẫn',
    file_header_size:'Kích thước', file_header_mod:'Sửa đổi',
    search_placeholder:'Tìm toàn bộ...', search_no_result:'Không tìm thấy kết quả',
    search_group_tasks:'Tasks', search_group_files:'Files', search_group_workers:'Workers',
    auto_off:'Tắt', auto_seconds:'s',
    analytics_collapse:'Thu gọn analytics', analytics_expand:'Mở analytics',
    theme_toggle:'Chế độ sáng/tối',
    stale_header_id:'ID', stale_header_title:'Tiêu đề', stale_header_assignee:'Người phụ trách',
    stale_header_pid:'PID', stale_header_age:'Age', stale_header_reason:'Lý do',
    all_header_created:'Tạo lúc', all_header_title:'Tiêu đề', all_header_assignee:'Người phụ trách',
    all_header_status:'Trạng thái', all_header_started:'Bắt đầu',
    done_header_title:'Tiêu đề', done_header_assignee:'Người phụ trách', done_header_done:'Xong lúc',
    cron_header_name:'Tên', cron_header_schedule:'Lịch', cron_header_next:'Lần chạy tới',
    cron_header_last:'Lần cuối', cron_header_status:'Trạng thái', cron_header_error:'Lỗi', cron_header_toggle:'On/Off',
    output_header_stt:'STT', output_header_title:'Tiêu đề', output_header_assignee:'Người phụ trách',
    output_header_status:'Trạng thái', output_header_time:'Thời gian',
    stale_create_header_stt:'STT', stale_create_header_title:'Tiêu đề',
    health_cpu:'CPU', health_ram:'RAM', health_disk:'Disk', health_uptime:'Uptime',
    health_disp:'Dispatcher', health_disp_run:'Chạy', health_disp_off:'Tắt', health_cores:'cores',
    empty_no_tasks:'Không có tác vụ', empty_no_data:'Chưa có dữ liệu',
    empty_no_cron:'Không có dữ liệu cron', empty_no_output:'Chưa có output',
    empty_no_events:'Không có sự kiện', empty_no_conv:'Không có hội thoại nào',
    empty_no_files:'Không có file .md nào', empty_no_msg:'Không có tin nhắn',
    empty_error:'Lỗi',
    confirm_retry:'Retry task', confirm_kill:'Kill task', confirm_claim:'Claim task',
    confirm_enqueue:'Enqueue task', confirm_complete:'Complete task', confirm_delete:'Xoá',
    confirm_bulk_kill:'Kill', confirm_bulk_del:'Xoá',
    toast_copied:'Đã copy vào clipboard', toast_no_content:'Không có nội dung để copy',
    toast_loading:'Đang tải...', toast_saved:'Đã lưu',
    tip_create_task:'Tạo task mới', tip_refresh:'Refresh', tip_delete:'Xoá task',
    task_workspace:'Workspace', task_status:'Status', task_assignee:'Assignee',
    filter_label_all:'Tất cả', filter_label_title:'Tiêu đề',
    all_table_empty:'Không có task nào', done_table_empty:'Chưa có task xong',
    status_archived:'Lưu trữ', kanban_empty:'Trống',
    cron_on:'ON', cron_off:'OFF', cron_auto_label:'30s auto', cron_loading:'Đang tải...',
    conv_no_title:'(no title)', conv_session:'Hội thoại', conv_agent:'Agent', conv_user:'User', conv_reasoning:'Reasoning',
    conv_tool_call:'tool_call', conv_no_msgs:'Không có tin nhắn',
    out_loading:'Đang tải...',
    system_health_title:'System Health', system_health_cores:'cores', system_health_disp:'Dispatcher',
    system_health_disp_run:'Chạy', system_health_disp_off:'Tắt',
    form_title_required:'Vui lòng nhập tiêu đề',
    empty_no_workers:'Không có worker nào', empty_no_tasks_filter:'Không có tác vụ',
    empty_no_task:'Không có task', empty_no_runs:'Không có lần chạy',
    confirm_kill_tasks:'Kill', confirm_kill_tasks_label:'Kill', confirm_del_selected:'Xoá đã chọn',
    toast_copied_clipboard:'Đã copy vào clipboard', toast_no_content_copy:'Không có nội dung để copy',
    toast_no_task_kill:'Không có task để kill', toast_select_del:'Chọn task để xoá',
    toast_enter_title:'Vui lòng nhập tiêu đề', toast_error_load:'Lỗi tải dữ liệu',
    modal_tab_output:'Output', modal_tab_events:'Sự kiện', modal_tab_runs:'Lần chạy', modal_tab_notes:'Notes',
    health_hermes_process:'Hermes Process', health_hermes_disp:'Dispatcher',
    health_hermes_disp_run:'Chạy', health_hermes_disp_off:'Tắt',
    worker_status_text:'Trạng thái', worker_status_run:'Đang chạy', worker_status_off:'Ngoại tuyến',
    kanban_load_error:'Lỗi:', kanban_no_data:'Chưa có dữ liệu',
    time_second:'giây trước', time_minute:'phút trước', time_hour:'giờ trước', time_day:'ngày trước', time_week:'tuần trước',
  },
  en: {
    tab_system:'System', tab_kanban:'Kanban', tab_cron:'Cron', tab_outputs:'Outputs',
    tab_workers:'Workers', tab_files:'Files', tab_conversations:'Conversations',
    subtab_stale:'Stale', subtab_all:'All', subtab_done:'Done',
    stat_total:'Total Tasks', stat_done:'Completed', stat_stale:'Stale / Running', stat_cron:'Cron Errors',
    sec_title_recent:'Recent Activity', sec_title_dist:'Distribution', sec_title_stale:'Stale Tasks',
    sec_title_kanban:'Kanban by Status', sec_title_cron:'Cron Schedule',
    sec_title_outputs:'Task Outputs', sec_title_workers:'Worker Agents',
    sec_title_files:'File Viewer', sec_title_conv:'Agent Conversations',
    health_score:'Health Score', health_complete:'Completed', health_stale:'Stale', health_running:'Running',
    filter_kill_sel:'Kill Selected', filter_del_sel:'Delete Selected', filter_kill_all:'Kill All',
    all_status:'All Status', all_info:'tasks',
    btn_create:'New Task', btn_export_csv:'CSV', btn_export_json:'JSON',
    btn_claim:'Claim', btn_enqueue:'Enqueue', btn_complete:'Complete', btn_retry:'Retry', btn_delete:'Delete',
    btn_save:'Save', btn_cancel:'Cancel', btn_ok:'OK',
    modal_output:'Output', modal_events:'Events', modal_runs:'Runs', modal_notes:'Notes',
    modal_delete_title:'Delete Task', modal_delete_warn:'This task will be permanently deleted! This action cannot be undone.',
    modal_delete_prompt:'Type CONFIRM to proceed:', modal_delete_confirm:'Type CONFIRM...',
    modal_delete_btn:'Delete Permanently', modal_delete_deleting:'Deleting...',
    modal_create_title:'Create New Task', modal_create_title_label:'Title', modal_create_assignee:'Assignee',
    modal_create_desc:'Description', modal_create_btn:'Create Task',
    modal_health_title:'System Health',
    modal_conv_title:'Conversation',
    toolbar_rendered:'Rendered', toolbar_raw:'Raw', toolbar_copy:'Copy', toolbar_edit:'Edit', toolbar_fullscreen:'Fullscreen',
    action_bar_label:'Actions:',
    notes_help:'Add notes to this task (saved to body field)', notes_save:'Save Notes',
    worker_status_run:'Running', worker_status_off:'Offline', worker_pid:'PID', worker_task:'Current Task',
    worker_total:'Total Tasks', worker_active:'Last Active',
    conv_profile:'Profile', conv_title:'Title', conv_model:'Model', conv_msgs:'Messages', conv_tokens:'Tokens',
    conv_cost:'Cost', conv_time:'Time',
    file_header_stt:'#', file_header_name:'Filename', file_header_path:'Path',
    file_header_size:'Size', file_header_mod:'Modified',
    search_placeholder:'Search everything...', search_no_result:'No results found',
    search_group_tasks:'Tasks', search_group_files:'Files', search_group_workers:'Workers',
    auto_off:'Off', auto_seconds:'s',
    analytics_collapse:'Collapse analytics', analytics_expand:'Expand analytics',
    theme_toggle:'Dark/Light Mode',
    stale_header_id:'ID', stale_header_title:'Title', stale_header_assignee:'Assignee',
    stale_header_pid:'PID', stale_header_age:'Age', stale_header_reason:'Reason',
    all_header_created:'Created', all_header_title:'Title', all_header_assignee:'Assignee',
    all_header_status:'Status', all_header_started:'Started',
    done_header_title:'Title', done_header_assignee:'Assignee', done_header_done:'Completed',
    cron_header_name:'Name', cron_header_schedule:'Schedule', cron_header_next:'Next Run',
    cron_header_last:'Last Run', cron_header_status:'Status', cron_header_error:'Error', cron_header_toggle:'On/Off',
    output_header_stt:'#', output_header_title:'Title', output_header_assignee:'Assignee',
    output_header_status:'Status', output_header_time:'Time',
    stale_create_header_stt:'#', stale_create_header_title:'Title',
    health_cpu:'CPU', health_ram:'RAM', health_disk:'Disk', health_uptime:'Uptime',
    health_disp:'Dispatcher', health_disp_run:'Running', health_disp_off:'Stopped', health_cores:'cores',
    empty_no_tasks:'No stale tasks', empty_no_data:'No data',
    empty_no_cron:'No cron data', empty_no_output:'No outputs',
    empty_no_events:'No events', empty_no_conv:'No conversations',
    empty_no_files:'No .md files', empty_no_msg:'No messages',
    empty_error:'Error',
    confirm_retry:'Retry task', confirm_kill:'Kill task', confirm_claim:'Claim task',
    confirm_enqueue:'Enqueue task', confirm_complete:'Complete task', confirm_delete:'Delete',
    confirm_bulk_kill:'Kill', confirm_bulk_del:'Delete',
    toast_copied:'Copied to clipboard', toast_no_content:'No content to copy',
    toast_loading:'Loading...', toast_saved:'Saved',
    tip_create_task:'Create new task', tip_refresh:'Refresh', tip_delete:'Delete task',
    task_workspace:'Workspace', task_status:'Status', task_assignee:'Assignee',
    filter_label_all:'All', filter_label_title:'Title',
    all_table_empty:'No tasks', done_table_empty:'No completed tasks',
    status_archived:'Archived', kanban_empty:'Empty',
    cron_on:'ON', cron_off:'OFF', cron_auto_label:'30s auto', cron_loading:'Loading...',
    conv_no_title:'(no title)', conv_session:'Conversation', conv_agent:'Agent', conv_user:'User', conv_reasoning:'Reasoning',
    conv_tool_call:'tool call', conv_no_msgs:'No messages',
    out_loading:'Loading...',
    system_health_title:'System Health', system_health_cores:'cores', system_health_disp:'Dispatcher',
    system_health_disp_run:'Running', system_health_disp_off:'Stopped',
    form_title_required:'Please enter a title',
    empty_no_workers:'No workers',     empty_no_tasks_filter:'No tasks',
    empty_no_task:'No tasks', empty_no_runs:'No runs',
    confirm_kill_tasks:'Kill', confirm_kill_tasks_label:'Kill', confirm_del_selected:'Delete Selected',
    toast_copied_clipboard:'Copied to clipboard', toast_no_content_copy:'No content to copy',
    toast_no_task_kill:'No tasks to kill', toast_select_del:'Select tasks to delete',
    toast_enter_title:'Please enter a title', toast_error_load:'Error loading data',
    modal_tab_output:'Output', modal_tab_events:'Events', modal_tab_runs:'Runs', modal_tab_notes:'Notes',
    health_hermes_process:'Hermes Process', health_hermes_disp:'Dispatcher',
    health_hermes_disp_run:'Running', health_hermes_disp_off:'Stopped',
    worker_status_text:'Status', worker_status_run:'Running', worker_status_off:'Offline',
    kanban_load_error:'Error:', kanban_no_data:'No data',
    time_second:'seconds ago', time_minute:'minutes ago', time_hour:'hours ago', time_day:'days ago', time_week:'weeks ago',
  }
};
let currentLang = localStorage.getItem('lang') || 'vi';
function _i(key, fb) {
  return (LANG[currentLang] || {})[key] || fb || key;
}
const S_LABEL = {
  ready: _i('status_ready','Sẵn sàng'), running: _i('status_running','Đang chạy'), stale: _i('status_stale','Treo'),
  blocked: _i('status_blocked','Chặn'), done: _i('status_done','Xong'), error: _i('status_error','Lỗi'),
  ok: _i('status_ok','OK'), completed: _i('status_completed','Hoàn tất'), gave_up: _i('status_gaveup','Bỏ'),
  spawn_failed: _i('status_spawn','Lỗi KT'), killed: _i('status_killed','Đã kill')
};
// Also add status labels to LANG dict
(function buildStatusLabels() {
  const viLabels = {status_ready:'Sẵn sàng', status_running:'Đang chạy', status_stale:'Treo', status_blocked:'Chặn', status_done:'Xong', status_error:'Lỗi', status_ok:'OK', status_completed:'Hoàn tất', status_gaveup:'Bỏ', status_spawn_failed:'Lỗi KT', status_killed:'Đã kill'};
  const enLabels = {status_ready:'Ready', status_running:'Running', status_stale:'Stale', status_blocked:'Blocked', status_done:'Done', status_error:'Error', status_ok:'OK', status_completed:'Completed', status_gaveup:'Gave Up', status_spawn_failed:'Spawn Fail', status_killed:'Killed'};
  Object.assign(LANG.vi, viLabels);
  Object.assign(LANG.en, enLabels);
})();
updateS_LABEL(); // Re-init S_LABEL with correct lang after status keys added to LANG
function updateS_LABEL() {
  const keys = ['ready','running','stale','blocked','done','error','ok','completed','gave_up','spawn_failed','killed'];
  const viVals = ['Sẵn sàng','Đang chạy','Treo','Chặn','Xong','Lỗi','OK','Hoàn tất','Bỏ','Lỗi KT','Đã kill'];
  const enVals = ['Ready','Running','Stale','Blocked','Done','Error','OK','Completed','Gave Up','Spawn Fail','Killed'];
  const vals = currentLang === 'en' ? enVals : viVals;
  keys.forEach((k,i) => S_LABEL[k] = vals[i]);
}
function switchLang(lang) {
  localStorage.setItem('lang', lang);
  location.reload();
}
function updateLangToggleUI() {
  const el = document.getElementById('langToggle');
  if (!el) return;
  el.innerHTML = currentLang === 'vi' ? '<i class="bi bi-translate"></i> VI' : '<i class="bi bi-translate"></i> EN';
}
function applyLangToStatic() {
  // Update tab names - only replace text nodes, preserve icons and badges
  const tabIds = {system:'tab_system',kanban:'tab_kanban',cron:'tab_cron',outputs:'tab_outputs',workers:'tab_workers',files:'tab_files',conversations:'tab_conversations'};
  Object.entries(tabIds).forEach(([k,v]) => {
    const el = document.getElementById('tab-'+k);
    if (!el) return;
    for (let node of el.childNodes) {
      if (node.nodeType === 3 && node.textContent.trim()) {
        node.textContent = ' ' + _i(v, LANG.vi[v]) + ' ';
        break;
      }
    }
  });
  // Update sub-tab names
  const subIds = {stale:'subtab_stale',all:'subtab_all',done:'subtab_done'};
  Object.entries(subIds).forEach(([k,v]) => {
    const el = document.getElementById('subtab-'+k);
    if (!el) return;
    for (let node of el.childNodes) {
      if (node.nodeType === 3 && node.textContent.trim()) {
        node.textContent = ' ' + _i(v, LANG.vi[v]) + ' ';
        break;
      }
    }
  });
  // Update analytics toggle
  const at = document.getElementById('analyticsToggle');
  if (at && window._analyticsVisible !== undefined) {
    const vis = window._analyticsVisible !== false;
    at.innerHTML = `<i class="bi ${vis?'bi-chevron-up':'bi-chevron-down'}"></i> ${vis ? _i('analytics_collapse','Thu gọn analytics') : _i('analytics_expand','Mở analytics')}`;
  }
  // Update all TH elements with known text via data map
  const textMap = [
    // Section headers / card headers
    ['Hoạt động gần đây', 'sec_title_recent'], ['Phân bố', 'sec_title_dist'],
    ['Kanban theo trạng thái', 'sec_title_kanban'], ['Lịch trình Cron', 'sec_title_cron'],
    ['Kết quả tác vụ', 'sec_title_outputs'], ['Worker Agents', 'sec_title_workers'],
    ['File Viewer', 'sec_title_files'], ['Hội thoại Agent', 'sec_title_conv'],
    // Stale table headers
    ['ID', 'stale_header_id'], ['Tiêu đề', 'stale_header_title'],
    ['Người phụ trách', 'stale_header_assignee'], ['PID', 'stale_header_pid'],
    ['Age', 'stale_header_age'], ['Lý do', 'stale_header_reason'],
    // All-task table headers
    ['Tạo lúc', 'all_header_created'], ['Trạng thái', 'all_header_status'],
    ['Bắt đầu', 'all_header_started'],
    // Done table
    ['Xong lúc', 'done_header_done'],
    // Cron table
    ['Tên', 'cron_header_name'], ['Lịch', 'cron_header_schedule'],
    ['Lần chạy tới', 'cron_header_next'], ['Lần cuối', 'cron_header_last'],
    ['Lỗi', 'cron_header_error'], ['On/Off', 'cron_header_toggle'],
    // Output table
    ['Thời gian', 'output_header_time'],
    // Worker table
    ['Profile', 'conv_profile'], ['Status', 'worker_status_text'],
    ['Tổng task', 'worker_total'], ['Hoạt động gần nhất', 'worker_active'],
    // File table
    ['STT', 'file_header_stt'], ['Tên file', 'file_header_name'],
    ['Đường dẫn', 'file_header_path'], ['Kích thước', 'file_header_size'],
    ['Sửa đổi', 'file_header_mod'],
    // Conversation table
    ['Model', 'conv_model'], ['Messages', 'conv_msgs'],
    ['Tokens', 'conv_tokens'], ['Cost', 'conv_cost'],
    ['Thời gian', 'conv_time'],
    // Filter dropdown
    ['Tất cả trạng thái', 'all_status'],
    // Task modal table
    ['Task', 'sec_title_stale'],
  ];
  document.querySelectorAll('th, .sec-title, .card-header, option, .filter-chip').forEach(el => {
    let txt = el.textContent.trim();
    // Strip trailing numbers from badge spans
    txt = txt.replace(/\d+$/, '').trim();
    textMap.forEach(([vi, key]) => {
      if (txt === vi) {
        if (el.tagName === 'OPTION' || el.classList.contains('filter-chip')) {
          el.textContent = _i(key, vi);
        } else {
          el.childNodes.forEach(n => {
            if (n.nodeType === 3 && n.textContent.trim() === vi) n.textContent = _i(key, vi);
          });
        }
      }
    });
  });
  // Update lang toggle
  updateLangToggleUI();
  // Update kill/delete button text
  ['killSelectedBtn', 'deleteSelectedBtn'].forEach(id => {
    const btn = document.getElementById(id);
    if (btn) {
      const key = id === 'killSelectedBtn' ? 'filter_kill_sel' : 'filter_del_sel';
      const countSpan = btn.querySelector('span');
      const count = countSpan ? countSpan.textContent : '0';
      const icon = btn.querySelector('i');
      btn.innerHTML = (icon ? icon.outerHTML : '') + _i(key, id==='killSelectedBtn'?'Kill':'Xoá')+' đã chọn';
      if (countSpan) btn.appendChild(countSpan);
    }
  });
  // Update Kill All button
  const kab = document.getElementById('killAllBtn');
  if (kab) { kab.innerHTML = '<i class="bi bi-trash3 me-1"></i>' + _i('filter_kill_all','Kill all'); }
  // Update search placeholder
  const si = document.getElementById('searchInput');
  if (si) si.placeholder = _i('search_placeholder','Tìm toàn bộ...');
}

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
  try { const ms = typeof ts === 'number' && ts < 1e12 ? ts * 1000 : ts; const d = new Date(ms); return isNaN(d.getTime()) ? ts : d.toLocaleString(currentLang==='en'?'en-US':'vi-VN',{hour:'2-digit',minute:'2-digit',day:'2-digit',month:'2-digit'}); }
  catch(e) { return ts; }
}

function fmtRelative(ts) {
  if (!ts) return '—';
  try {
    const ms = typeof ts === 'number' && ts < 1e12 ? ts * 1000 : ts;
    const diff = Date.now() - ms;
    if (isNaN(diff)) return fmtTime(ts);
    const sec = Math.floor(diff/1000);
    if (sec < 60) return sec + ' ' + _i('time_second','giây trước');
    const min = Math.floor(sec/60);
    if (min < 60) return min + ' ' + _i('time_minute','phút trước');
    const hr = Math.floor(min/60);
    if (hr < 24) return hr + ' ' + _i('time_hour','giờ trước');
    const day = Math.floor(hr/24);
    if (day < 7) return day + ' ' + _i('time_day','ngày trước');
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
  // Preserve code blocks before HTML escaping
  const codes = [];
  s = s.replace(/```([\s\S]*?)```/g, (_,c) => { const i=codes.length; codes.push({text:c,block:true}); return `\x00CB${i}\x00`; });
  s = s.replace(/`([^`]+)`/g, (_,c) => { const i=codes.length; codes.push({text:c,block:false}); return `\x00CB${i}\x00`; });
  // HTML escape
  s = s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  // Restore code blocks with proper tags
  s = s.replace(/\x00CB(\d+)\x00/g, (_,i) => {
    const entry = codes[parseInt(i)];
    const esc = entry.text.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    if (!entry.block) return `<code>${esc}</code>`;
    return `<pre><code>${esc}</code></pre>`;
  });
  // Links
  s = s.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  // Inline formatting (order: bold before italic)
  s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  s = s.replace(/\*([^*]+)\*/g, '<em>$1</em>');
  s = s.replace(/__([^_]+)__/g, '<u>$1</u>');
  s = s.replace(/~~([^~]+)~~/g, '<s>$1</s>');
  // Headings (h1-h6)
  s = s.replace(/^(#{1,6})\s+(.*)$/gm, (_, h, c) => `<h${Math.min(h.length,6)}>${c}</h${Math.min(h.length,6)}>`);
  // Horizontal rules
  s = s.replace(/^-{3,}$/gm, '<hr>');
  // Lists: wrap consecutive list items in single <ul>
  s = s.replace(/(?:^|\n)[ \t]*[-*]\s+.*(?:\n[ \t]*[-*]\s+.*)*/g, m => '<ul>'+(m.replace(/^\n/,'').split(/\n/).map(l => '<li>'+l.replace(/^[ \t]*[-*]\s+/,'')+'</li>').join(''))+'</ul>');
  s = s.replace(/(?:^|\n)[ \t]*\d+\.\s+.*(?:\n[ \t]*\d+\.\s+.*)*/g, m => '<ol>'+(m.replace(/^\n/,'').split(/\n/).map(l => '<li>'+l.replace(/^[ \t]*\d+\.\s+/,'')+'</li>').join(''))+'</ol>');
  // Tables
  ;(function(){
    const lines = s.split('\n'), out = [];
    let i = 0;
    while (i < lines.length) {
      if (/^\|.*\|$/.test(lines[i].trim())) {
        const tbl = [];
        let j = i;
        while (j < lines.length && /^\|.*\|$/.test(lines[j].trim())) { tbl.push(lines[j]); j++; }
        if (tbl.length >= 2) {
          const hasSep = tbl[1] && /^\|[\s:-]+\|$/.test(tbl[1].trim());
          const hRows = hasSep ? tbl.slice(0,1) : [];
          const dRows = hasSep ? tbl.slice(2) : tbl;
          let html = '<div class="table-wrap" style="margin:4px 0;font-size:.72rem"><table>';
          if (hRows.length) {
            html += '<thead><tr>'+hRows[0].split('|').filter(c=>c.trim()).map(c=>'<th>'+c.trim()+'</th>').join('')+'</tr></thead>';
          }
          if (dRows.length) {
            html += '<tbody>';
            for (let k=0;k<dRows.length;k++) {
              html += '<tr>'+dRows[k].split('|').filter(c=>c.trim()).map(c=>'<td>'+c.trim()+'</td>').join('')+'</tr>';
            }
            html += '</tbody>';
          }
          out.push(html+'</table></div>');
        } else { out.push(tbl.join('\n')); }
        i = j;
      } else { out.push(lines[i]); i++; }
    }
    s = out.join('\n');
  })();
  // Paragraphs: double newline = paragraph break, single newline = <br>
  s = s.replace(/\n{2,}/g, '</p><p>');
  s = s.replace(/\n/g, '<br>');
  // Remove empty paragraphs
  s = s.replace(/<p>\s*<\/p>/g, '');
  s = '<p>' + s + '</p>';
  // Clean up wrappers around block elements
  s = s.replace(/<p><(pre|code|h[1-6]|ul|ol|li|hr|div|table|thead|tbody|tr|th|td)/g, '<$1');
  s = s.replace(/<\/(pre|code|h[1-6]|ul|ol|li|div|table|thead|tbody|tr|th|td)><\/p>/g, '</$1>');
  // Fix: </p> before block elements
  s = s.replace(/<\/p>\n?<(pre|h[1-6]|ul|ol|div|table)/g, '<$1');
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
      {label:_i('stat_total','Tổng tasks'), value:t.total, sub:pct+'% '+_i('health_complete','hoàn thành'), icon:'bi-list-task', color:'#818cf8'},
      {label:_i('stat_done','Đã hoàn thành'), value:doneTotal, sub:activeTasks+' '+_i('health_running','đang hoạt động'), icon:'bi-check-circle', color:'#4ade80'},
      {label:_i('stat_stale','Treo / Đang chạy'), value:t.stale_count+' / '+t.running_workers, icon:'bi-exclamation-triangle', color:t.stale_count>0?'#f87171':'#fbbf24'},
      {label:_i('stat_cron','Cron lỗi'), value:data.cron_errors, sub:'/'+crons.length+' jobs', icon:'bi-x-circle', color:data.cron_errors>0?'#f87171':'#7878aa'},
    ];
    document.getElementById('statCards').innerHTML = cards.map(c => `
      <div class="stat-card" style="cursor:pointer" onclick="toggleAnalytics()">
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

    const filtered = window._taskFilter === 'all' ? s : s.filter(r => r.status === window._taskFilter);
    document.getElementById('staleTable').innerHTML = filtered.length
      ? filtered.map(r => {
          const isStale = r.status === 'stale';
          return `<tr class="${isStale?'stale-row':''}"><td><input type="checkbox" class="stale-check" value="${r.id}" onchange="updateSelected()"></td><td><code class="task-id" onclick="openTaskDetail('${r.id}')">${h(r.id,'').substring(0,12)}</code></td><td>${h(r.title,'(no title)')}</td><td>${assigneeCell(r.assignee)}</td><td>${h(r.worker_pid)}</td><td>${h(r.age_human)}</td><td>${r.reason ? '<span class="badge-dot" style="background:'+(r.reason==='timeout'?'var(--yellow)':'var(--red)')+'1a;color:'+(r.reason==='timeout'?'var(--yellow)':'var(--red)')+';border:1px solid '+(r.reason==='timeout'?'var(--yellow)':'var(--red)')+'33"><span class="status-dot" style="background:'+(r.reason==='timeout'?'var(--yellow)':'var(--red)')+'"></span>'+r.reason+'</span>' : '<span style="color:var(--text3)">—</span>'}</td><td><button class="btn btn-sm btn-outline-warning me-1 py-0 px-2" onclick="retryTask('${r.id}')"><i class="bi bi-arrow-clockwise"></i></button><button class="btn btn-sm btn-outline-danger py-0 px-2" onclick="killTask('${r.id}')"><i class="bi bi-x-lg"></i></button></td></tr>`;
        }).join('')
      : '<tr><td colspan="8" class="empty-state">'+EMPTY_ICONS.noTasks+_i('empty_no_tasks','Không có tác vụ')+(window._taskFilter!=='all'?' ('+window._taskFilter+')':'')+'</td></tr>';
    document.getElementById('staleCountBadge').textContent = t.stale_count;
    document.getElementById('selectAll').checked = false;
    document.getElementById('killSelectedBtn').classList.add('d-none');

    // Pie chart
    var pieEl = document.getElementById('pieChart');
    var legendEl = document.getElementById('pieLegend');
    if (pieEl && legendEl) {
      var pieColors = {ready:'#4ade80',blocked:'#6b7280',running:'#fbbf24',stale:'#f87171',done:'#38bdf8',archived:'#5858aa'};
      var pieLabels = {ready:_i('status_ready','Sẵn sàng'),blocked:_i('status_blocked','Chặn'),running:_i('status_running','Đang chạy'),stale:_i('status_stale','Treo'),done:_i('status_done','Xong'),archived:_i('status_archived','Lưu trữ')};
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
      }).join('') || '<tr><td colspan="4" class="empty-state">'+EMPTY_ICONS.noData+_i('empty_no_data','Chưa có dữ liệu')+'</td></tr>';
  } catch(_) { /* ignore recent fetch errors */ }

    renderKanban(board);
    renderCron(crons);
    document.getElementById('cronBadge').textContent = crons.length;
    document.getElementById('cronCountBadge').textContent = crons.length;
    document.getElementById('lastUpdate').textContent = new Date().toLocaleTimeString(currentLang==='en'?'en-US':'vi-VN');
    loadOutputs();
  } catch(e) { console.error(e); toast('Lỗi tải dữ liệu', 'danger'); }
}

function renderKanban(data) {
  var statusOrder = ['ready','blocked','running','stale','done','archived'];
  var statusLabels = {ready:_i('status_ready','Sẵn sàng'),blocked:_i('status_blocked','Chặn'),running:_i('status_running','Đang chạy'),stale:_i('status_stale','Treo'),done:_i('status_done','Xong'),archived:_i('status_archived','Lưu trữ')};
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
      html += '<div class="empty-state" style="padding:.8rem 0;font-size:.68rem;color:var(--text3)">'+_i('kanban_empty','Trống')+'</div>';
    }
    html += '</div></div>';
  });
  document.getElementById('kanbanBoard').innerHTML = html || '<div class="empty-state" style="padding:2.5rem">'+EMPTY_ICONS.noData+_i('empty_no_data','Chưa có dữ liệu')+'</div>';
}

function renderCron(crons) {
  document.getElementById('cronTable').innerHTML = crons.length
      ? crons.map(c => {
        const st = c.last_status || 'unknown';
        const err = c.last_error || c.last_delivery_error || '';
        const enabled = c.enabled !== false;
        const onOff = enabled ? _i('cron_on','ON') : _i('cron_off','OFF');
        return `<tr><td><strong>${h(c.name)}</strong></td><td><code style="color:var(--text3);font-size:.72rem">${h(c.schedule_display)}</code></td><td style="color:var(--text2);font-size:.72rem" title="${fmtTime(c.next_run_at)}">${fmtRelative(c.next_run_at)}</td><td style="color:var(--text2);font-size:.72rem" title="${fmtTime(c.last_run_at)}">${fmtRelative(c.last_run_at)}</td><td>${badge(st)}</td><td style="max-width:250px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:.72rem" title="${err.replace(/"/g,'"')}">${err ? err.substring(0,60)+(err.length>60?'...':'') : '<span style="color:var(--text3)">—</span>'}</td><td class="text-center"><span class="cron-toggle" style="cursor:pointer" onclick="toggleCron('${c.name}')"><span class="cron-status-led ${enabled?'enabled':'disabled'}"></span><span style="font-size:.62rem;color:${enabled?'var(--green)':'var(--text3)'}">${onOff}</span></span></td></tr>`;
      }).join('')
    : '<tr><td colspan="7" class="empty-state">'+EMPTY_ICONS.noCron+_i('empty_no_cron','Không có dữ liệu cron')+'</td></tr>';
}

async function loadOutputs() {
  try {
    const r = await fetch('/api/task-outputs');
    const d = await r.json();
    document.getElementById('outputCountBadge').textContent = d.length;
    document.getElementById('outputRefreshLabel').textContent = new Date().toLocaleTimeString(currentLang==='en'?'en-US':'vi-VN');
    document.getElementById('outputTable').innerHTML = d.length
      ? d.map((t, i) => {
          const ts = t.completed_at || t.started_at;
          return `<tr><td><span class="row-idx">${i+1}</span></td><td><a href="#" onclick="openTaskDetail('${t.id}');return false" class="task-link">${h(t.title||t.id)}</a></td><td>${assigneeCell(t.assignee)}</td><td>${badge(t.status)}</td><td style="color:var(--text3);font-size:.7rem;white-space:nowrap" title="${fmtTime(ts)}">${fmtRelative(ts)}</td></tr>`;
        }).join('')
      : '<tr><td colspan="5" class="empty-state">'+EMPTY_ICONS.noOutput+_i('empty_no_output','Chưa có output')+'</td></tr>';
  } catch(e) { document.getElementById('outputTable').innerHTML = '<tr><td colspan="6" class="empty-state">'+EMPTY_ICONS.error+_i('empty_error','Lỗi')+': '+e.message+'</td></tr>'; }
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
  if (!ids.length) { toast(_i('toast_no_task_kill','Không có task để kill'), 'warning'); return; }
  if (!confirm(_i('confirm_kill_tasks','Kill')+` ${ids.length} task`)) return;
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
      : '<tr><td colspan="5" class="empty-state">'+EMPTY_ICONS.noTasks+_i('empty_no_task','Không có task')+'</td></tr>';
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
      <span id="modalStatusCell">${badge(t.status)} <span class="edit-inline-btn" onclick="editTaskStatus('${t.status}')" title="Sửa trạng thái" style="cursor:pointer;color:var(--text3);font-size:.65rem;margin-left:2px"><i class="bi bi-pencil"></i></span></span>
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
        <span class="toolbar-btn active" id="outViewRendered" onclick="toggleOutputView('rendered');return false"><i class="bi bi-eye"></i> ${_i('toolbar_rendered','Hiển thị')}</span>
        <span class="toolbar-btn" id="outViewRaw" onclick="toggleOutputView('raw');return false"><i class="bi bi-braces"></i> ${_i('toolbar_raw','Raw')}</span>
        <span class="toolbar-sep"></span>
        <span class="toolbar-btn" id="copyBtn" onclick="copyOutput()" title="Ctrl+C"><i class="bi bi-clipboard"></i> ${_i('toolbar_copy','Copy')}</span>
        <span class="toolbar-btn" onclick="editTaskOutput(window._taskOutputData?.rawOutput)" title="${_i('toolbar_edit','Sửa output')}" style="margin-left:2px"><i class="bi bi-pencil"></i> ${_i('toolbar_edit','Sửa')}</span>
        <span class="toolbar-sep"></span>
        <span class="toolbar-btn" id="expandBtn" onclick="toggleOutputExpand()" title="${_i('toolbar_fullscreen','Toàn màn hình')}"><i class="bi bi-arrows-fullscreen" id="expandIcon"></i></span>
      </div>
      <div class="output-block" id="outputBlockContent">${renderMd(rawOutput)}</div>
      <pre class="output-raw" id="outputRawContent" style="display:none">${h(rawOutput)}</pre>`
      : `<div class="empty-state" style="padding:3rem">${EMPTY_ICONS.noOutput}Không có output được ghi lại</div>`;

    // Events tab content (timeline)
    const evHtml = evs.length
      ? '<div class="timeline">'+evs.map(e => `<div class="timeline-item timeline-${h(e.kind,'unknown')}"><span class="timeline-time" title="${fmtTime(e.created_at)}">${fmtRelative(e.created_at)}</span><div class="timeline-content"><span class="badge-dot" style="background:${(S_COLOR[e.kind]||'#6b7280')}1a;color:${S_COLOR[e.kind]||'#6b7280'};border:1px solid ${(S_COLOR[e.kind]||'#6b7280')}33"><span class="status-dot" style="background:${S_COLOR[e.kind]||'#6b7280'}"></span>${h(e.kind)}</span></div></div>`).join('')+'</div>'
      : '<div class="empty-state" style="padding:2rem">'+EMPTY_ICONS.noEvents+_i('empty_no_events','Không có sự kiện')+'</div>';

    // Runs tab content
    const runsHtml = runs.length
      ? '<div class="timeline">'+runs.map(r => {
          const st = r.status || 'unknown';
          return `<div class="timeline-item ${st==='done'?'timeline-complete':st==='running'?'timeline-enqueue':''}"><span class="timeline-time" title="${fmtTime(r.started_at)}">${fmtRelative(r.started_at)}</span><div class="timeline-content">${badge(st)}${r.error ? '<span style="color:var(--red);font-size:.7rem;max-width:300px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;margin-left:4px">'+r.error.substring(0,60)+'</span>' : ''}</div></div>`;
        }).join('')+'</div>'
      : '<div class="empty-state" style="padding:2rem">'+EMPTY_ICONS.noData+_i('empty_no_runs','Không có lần chạy')+'</div>';

    // Build body
    document.getElementById('modalBody').innerHTML = `
      ${errorHtml}
      ${metaHtml}
      <div class="task-actions-bar">
        <span class="ta-label">${_i('action_bar_label','Hành động:')}</span>
        <span class="ta-btn ta-btn-claim" onclick="claimTask('${id}')"><i class="bi bi-hand-index-thumb"></i> ${_i('btn_claim','Claim')}</span>
        <span class="ta-btn ta-btn-enqueue" onclick="enqueueTask('${id}')"><i class="bi bi-play-fill"></i> ${_i('btn_enqueue','Enqueue')}</span>
        <span class="ta-btn ta-btn-complete" onclick="completeTask('${id}')"><i class="bi bi-check-lg"></i> ${_i('btn_complete','Complete')}</span>
        <span class="ta-btn ta-btn-claim" onclick="retryTask('${id}')"><i class="bi bi-arrow-clockwise"></i> ${_i('btn_retry','Retry')}</span>
      </div>
      <div class="modal-tabs">
        <button class="modal-tab active" onclick="switchModalTab('output');return false"><i class="bi bi-file-text"></i> Output</button>
        <button class="modal-tab" onclick="switchModalTab('events');return false"><i class="bi bi-clock-history"></i> Sự kiện <span class="tab-count">${evs.length}</span></button>
        <button class="modal-tab" onclick="switchModalTab('runs');return false"><i class="bi bi-play-circle"></i> Lần chạy <span class="tab-count">${runs.length}</span></button>
      </div>
      <div class="modal-tab-pane active" id="pane-output">${outputHtml}</div>
      <div class="modal-tab-pane" id="pane-events">${evHtml}</div>
      <div class="modal-tab-pane" id="pane-runs">${runsHtml}</div>`;
    window._taskOutputData = { rawOutput, taskId: id };
    var modalEl = document.getElementById('taskModal');
    var modal = bootstrap.Modal.getInstance(modalEl) || new bootstrap.Modal(modalEl);
    modal.show();
  } catch(e) { toast('Lỗi: '+e, 'danger'); }
}

function switchModalTab(name) {
  const tabNames = {output:_i('modal_tab_output','Output'), events:_i('modal_tab_events','Sự kiện'), runs:_i('modal_tab_runs','Lần chạy'), notes:_i('modal_tab_notes','Notes')};
  document.querySelectorAll('.modal-tab').forEach(b => {
    const txt = b.textContent.trim().toLowerCase();
    const match = (name==='output'&&txt.includes('output')) || (name==='events'&&(txt.includes('sự kiện')||txt.includes('events'))) || (name==='runs'&&(txt.includes('lần chạy')||txt.includes('runs'))) || (name==='notes'&&txt.includes('notes'));
    b.classList.toggle('active', match);
  });
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
  if (!text) { toast(_i('toast_no_content_copy','Không có nội dung để copy'), 'warning'); return; }
  try {
    await navigator.clipboard.writeText(text);
    var btn = document.getElementById('copyBtn');
    if (btn) { btn.classList.add('copied'); setTimeout(function(){ btn.classList.remove('copied'); }, 1500); }
    toast(_i('toast_copied_clipboard','Đã copy vào clipboard'), 'success');
  } catch(e) { toast('Lỗi: '+e, 'danger'); }
}

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
      document.getElementById('cronRefreshLabel').textContent = _i('cron_loading','Đang tải...');
      loadDashboard().then(() => {
        document.getElementById('cronRefreshDot').classList.remove('loading');
        document.getElementById('cronRefreshLabel').textContent = _i('cron_auto_label','30s auto');
      });
      if (!cronTimer) cronTimer = setInterval(async () => { await loadDashboard(); document.getElementById('cronRefreshDot').classList.remove('loading'); document.getElementById('cronRefreshLabel').textContent = _i('cron_auto_label','30s auto'); }, 30000);
    } else if (e.target.id === 'tab-outputs') {
      loadOutputs();
      if (cronTimer) { clearInterval(cronTimer); cronTimer = null; }
    } else if (e.target.id === 'tab-workers') {
      if (cronTimer) { clearInterval(cronTimer); cronTimer = null; }
      loadWorkers();
    } else if (e.target.id === 'tab-files') {
      if (cronTimer) { clearInterval(cronTimer); cronTimer = null; }
      loadFiles();
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

// === Create Task ===
function openCreateTaskModal() {
  document.getElementById('createTaskTitle').value = '';
  document.getElementById('createTaskAssignee').value = '';
  document.getElementById('createTaskDesc').value = '';
  document.getElementById('createTaskBtn').disabled = false;
  document.getElementById('createTaskBtn').innerHTML = '<i class="bi bi-check-lg me-1"></i>Tạo task';
  new bootstrap.Modal(document.getElementById('createTaskModal')).show();
}

async function submitCreateTask() {
  const title = document.getElementById('createTaskTitle').value.trim();
  if (!title) { toast(_i('toast_enter_title','Vui lòng nhập tiêu đề'), 'warning'); return; }
  const btn = document.getElementById('createTaskBtn');
  btn.disabled = true; btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Đang tạo...';
  try {
    const r = await fetch('/api/tasks', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        title,
        assignee: document.getElementById('createTaskAssignee').value.trim(),
        description: document.getElementById('createTaskDesc').value.trim(),
      })
    });
    const d = await r.json();
    if (d.ok) {
      toast(d.message, 'success');
      bootstrap.Modal.getInstance(document.getElementById('createTaskModal')).hide();
      loadDashboard();
    } else {
      toast(d.message || 'Lỗi tạo task', 'danger');
    }
  } catch(e) { toast('Lỗi: '+e, 'danger'); }
  btn.disabled = false; btn.innerHTML = '<i class="bi bi-check-lg me-1"></i>Tạo task';
}

// === Inline Edit Task Status (in modal) ===
function editTaskStatus(current) {
  const cell = document.getElementById('modalStatusCell');
  if (!cell) return;
  const opts = ['ready','running','blocked','stale','done','error','killed'];
  cell.innerHTML = `<select class="edit-inline-select" id="inlineStatusSelect">${opts.map(s => `<option value="${s}"${s===current?' selected':''}>${S_LABEL[s]||s}</option>`).join('')}</select> <button class="edit-save-btn" onclick="saveTaskStatus()"><i class="bi bi-check"></i></button> <button class="edit-cancel-btn" onclick="cancelTaskEdit('modalStatusCell')">Huỷ</button>`;
}

async function saveTaskStatus() {
  const sel = document.getElementById('inlineStatusSelect');
  if (!sel) return;
  const id = window._taskOutputData?.taskId;
  if (!id) return;
  try {
    const r = await fetch(`/api/task/${id}`, {
      method: 'PATCH',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({status: sel.value})
    });
    const d = await r.json();
    if (d.ok) { toast(d.message, 'success'); openTaskDetail(id); }
    else { toast(d.message, 'danger'); }
  } catch(e) { toast('Lỗi: '+e, 'danger'); }
}

// === Inline Edit Task Output (in modal) ===
function editTaskOutput(current) {
  const pane = document.getElementById('pane-output');
  if (!pane) return;
  const text = current || '';
  pane.innerHTML = `<textarea class="form-control" id="inlineOutputText" rows="10" style="font-family:var(--font-mono);font-size:.78rem;background:var(--bg2);border-color:var(--border);color:var(--text);resize:vertical">${h(text).replace(/&amp;/g,'&').replace(/&lt;/g,'<').replace(/&gt;/g,'>')}</textarea><div style="margin-top:6px;display:flex;gap:6px"><button class="edit-save-btn" onclick="saveTaskOutput()"><i class="bi bi-check"></i> ${_i('btn_save','Lưu')}</button><button class="edit-cancel-btn" onclick="cancelEditOutput()">Huỷ</button></div>`;
}

async function saveTaskOutput() {
  const ta = document.getElementById('inlineOutputText');
  if (!ta) return;
  const id = window._taskOutputData?.taskId;
  if (!id) return;
  try {
    const r = await fetch(`/api/task/${id}/output`, {
      method: 'PATCH',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({output: ta.value})
    });
    const d = await r.json();
    if (d.ok) { toast(d.message, 'success'); openTaskDetail(id); }
    else { toast(d.message, 'danger'); }
  } catch(e) { toast('Lỗi: '+e, 'danger'); }
}

function cancelEditOutput() {
  const id = window._taskOutputData?.taskId;
  if (id) openTaskDetail(id);
}

function cancelTaskEdit(cellId) {
  const id = window._taskOutputData?.taskId;
  if (id) openTaskDetail(id);
}

// === Workers ===
async function loadWorkers() {
  try {
    const r = await fetch('/api/workers');
    const data = await r.json();
    renderWorkers(data);
    document.getElementById('workerRefreshLabel').textContent = new Date().toLocaleTimeString(currentLang==='en'?'en-US':'vi-VN');
  } catch(e) { document.getElementById('workerTable').innerHTML = '<tr><td colspan="6" class="empty-state">'+EMPTY_ICONS.error+'Lỗi: '+e.message+'</td></tr>'; }
}

function renderWorkers(data) {
  const grid = document.getElementById('workerGrid');
  const table = document.getElementById('workerTable');

  if (!data || !data.length) {
    grid.innerHTML = '<div class="empty-state" style="padding:2rem">'+EMPTY_ICONS.noData+_i('empty_no_workers','Không có worker nào')+'</div>';
    table.innerHTML = '<tr><td colspan="6" class="empty-state">Không có dữ liệu</td></tr>';
    return;
  }

  grid.innerHTML = data.map((w, i) => `
    <div class="worker-card" style="animation-delay:${i*0.05}s">
      <div class="wc-head">
        <span class="worker-dot ${w.status}"></span>
        <span class="wc-name">${avatar(w.profile,'md')} ${h(w.profile)}</span>
      </div>
      <div class="wc-row"><span>PID</span><strong>${h(w.pid,'—')}</strong></div>
      <div class="wc-row"><span>${_i('worker_status_run','Trạng thái')}</span><strong style="color:${w.status==='running'?'var(--green)':'var(--text3)'}">${w.status==='running'?_i('worker_status_run','Đang chạy'):_i('worker_status_off','Ngoại tuyến')}</strong></div>
      <div class="wc-row"><span>${_i('worker_task','Task hiện tại')}</span><strong>${w.current_task ? w.current_task.title.substring(0,28)+(w.current_task.title.length>28?'...':'') : '—'}</strong></div>
      <div class="wc-row"><span>${_i('worker_total','Tổng task')}</span><strong>${w.task_count}</strong></div>
      <div class="wc-row"><span>${_i('worker_active','Hoạt động')}</span><strong style="font-size:.65rem">${fmtRelative(w.last_heartbeat||w.started_at)}</strong></div>
    </div>
  `).join('');

  table.innerHTML = data.map(w => `
    <tr>
      <td>${avatar(w.profile,'sm')} ${h(w.profile)}</td>
      <td><code style="font-size:.72rem">${h(w.pid,'—')}</code></td>
      <td>${w.status==='running' ? badge('running') : '<span class="badge-dot" style="background:var(--text3)1a;color:var(--text3);border:1px solid var(--text3)33"><span class="status-dot" style="background:var(--text3)"></span>'+_i('worker_status_off','Ngoại tuyến')+'</span>'}</td>
      <td style="font-size:.75rem">${w.current_task ? `<a href="#" onclick="openTaskDetail('${w.current_task.id}');return false" class="task-link">${h(w.current_task.title).substring(0,40)}</a>` : '<span style="color:var(--text3)">—</span>'}</td>
      <td>${w.task_count}</td>
      <td style="font-size:.7rem;color:var(--text2)" title="${fmtTime(w.last_heartbeat||w.started_at)}">${fmtRelative(w.last_heartbeat||w.started_at)}</td>
    </tr>
  `).join('');
}

// === Files ===
async function loadFiles() {
  try {
    const r = await fetch('/api/files');
    const data = await r.json();
    renderFiles(data);
    document.getElementById('fileRefreshLabel').textContent = new Date().toLocaleTimeString(currentLang==='en'?'en-US':'vi-VN');
  } catch(e) { document.getElementById('fileTable').innerHTML = '<tr><td colspan="6" class="empty-state">'+EMPTY_ICONS.error+'Lỗi: '+e.message+'</td></tr>'; }
}

function renderFiles(data) {
  const el = document.getElementById('fileTable');
  const prompt = document.getElementById('vaultConfigPrompt');
  // Check vault config
  fetch('/api/config').then(r => r.json()).then(c => {
    if (c.vault_dir) {
      if (prompt) prompt.style.display = 'none';
    } else {
      if (prompt) prompt.style.display = 'block';
    }
  }).catch(() => {});
  if (!data || !data.length) {
    el.innerHTML = '<tr><td colspan="6" class="empty-state">'+EMPTY_ICONS.noData+_i('empty_no_files','Không có file .md nào')+'</td></tr>';
    return;
  }
  el.innerHTML = data.map((f, i) => {
    const sizeStr = f.size < 1024 ? f.size+'B' : f.size < 1048576 ? (f.size/1024).toFixed(1)+'KB' : (f.size/1048576).toFixed(1)+'MB';
    return `<tr>
      <td><span class="row-idx">${i+1}</span></td>
      <td><strong style="font-size:.78rem">${h(f.name)}</strong></td>
      <td><span class="file-path" title="${h(f.path)}">${h(f.path).substring(0,50)+(f.path.length>50?'...':'')}</span></td>
      <td><span class="file-size">${sizeStr}</span></td>
      <td style="font-size:.7rem;color:var(--text2)" title="${fmtTime(f.modified*1000)}">${fmtRelative(f.modified*1000)}</td>
      <td><button class="btn btn-sm btn-outline-secondary py-0 px-2" onclick="openFilePreview(${i})"><i class="bi bi-eye"></i></button></td>
    </tr>`;
  }).join('');
  window._fileData = data;
}

// === Settings ===
function openSettings() {
  fetch('/api/config').then(r => r.json()).then(c => {
    document.getElementById('settingsVaultDir').value = c.vault_dir || '';
    new bootstrap.Modal(document.getElementById('settingsModal')).show();
  }).catch(() => {
    new bootstrap.Modal(document.getElementById('settingsModal')).show();
  });
}

async function saveVaultSettings() {
  const dir = document.getElementById('settingsVaultDir').value.trim();
  const btn = document.getElementById('saveSettingsBtn');
  btn.disabled = true; btn.innerHTML = '<i class="bi bi-check-lg me-1"></i>Đang lưu...';
  try {
    const r = await fetch('/api/config/vault', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({vault_dir: dir})});
    const d = await r.json();
    toast(d.message, d.ok ? 'success' : 'danger');
    if (d.ok) {
      bootstrap.Modal.getInstance(document.getElementById('settingsModal')).hide();
      loadFiles();
    }
  } catch(e) { toast('Lỗi: '+e, 'danger'); }
  btn.disabled = false; btn.innerHTML = '<i class="bi bi-check-lg me-1"></i>Lưu';
}

function openFilePreview(idx) {
  const data = window._fileData;
  if (!data || !data[idx]) return;
  const f = data[idx];
  const raw = f.preview || '';
  const sizeStr = f.size < 1024 ? f.size+'B' : f.size < 1048576 ? (f.size/1024).toFixed(1)+'KB' : (f.size/1048576).toFixed(1)+'MB';
  const html = `<div class="modal fade file-preview-modal" id="filePreviewModal" tabindex="-1"><div class="modal-dialog modal-xl modal-dialog-centered modal-dialog-scrollable"><div class="modal-content">
    <div class="modal-header"><h5 class="modal-title"><i class="bi bi-file-earmark-text me-1"></i>${h(f.name)}</h5><button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal"></button></div>
    <div class="modal-body">
      <div class="meta-chips" style="margin-bottom:.75rem">
        <span class="meta-chip"><i class="bi bi-folder"></i> <strong>${h(f.path)}</strong></span>
        <span class="meta-chip-sep"></span>
        <span class="meta-chip"><i class="bi bi-file-earmark"></i> <strong>${sizeStr}</strong></span>
      </div>
      <div class="output-toolbar">
        <span class="toolbar-btn active" id="fileViewRendered" onclick="toggleFileView('rendered');return false"><i class="bi bi-eye"></i> ${_i('toolbar_rendered','Hiển thị')}</span>
        <span class="toolbar-btn" id="fileViewRaw" onclick="toggleFileView('raw');return false"><i class="bi bi-braces"></i> ${_i('toolbar_raw','Raw')}</span>
        <span class="toolbar-sep"></span>
        <span class="toolbar-btn" onclick="copyFileContent()" title="Ctrl+C"><i class="bi bi-clipboard"></i> ${_i('toolbar_copy','Copy')}</span>
      </div>
      <div class="output-block" id="fileBlockContent">${renderMd(raw)}</div>
      <pre class="output-raw" id="fileRawContent" style="display:none">${h(raw)}</pre>
    </div>
  </div></div></div>`;
  const existing = document.getElementById('filePreviewModal');
  if (existing) existing.remove();
  document.body.insertAdjacentHTML('beforeend', html);
  window._fileRawContent = raw;
  var modalEl = document.getElementById('filePreviewModal');
  var modal = bootstrap.Modal.getInstance(modalEl) || new bootstrap.Modal(modalEl);
  modal.show();
  modalEl.addEventListener('hidden.bs.modal', function(){ this.remove(); });
}

function toggleFileView(mode) {
  var rendered = document.getElementById('fileBlockContent');
  var raw = document.getElementById('fileRawContent');
  var btnR = document.getElementById('fileViewRendered');
  var btnRaw = document.getElementById('fileViewRaw');
  if (!rendered || !raw) return;
  if (mode === 'rendered') {
    rendered.style.display = ''; raw.style.display = 'none';
    btnR.classList.add('active'); btnRaw.classList.remove('active');
  } else {
    rendered.style.display = 'none'; raw.style.display = '';
    btnR.classList.remove('active'); btnRaw.classList.add('active');
  }
}

async function copyFileContent() {
  var text = window._fileRawContent || '';
  if (!text) { toast(_i('toast_no_content_copy','Không có nội dung để copy'), 'warning'); return; }
  try {
    await navigator.clipboard.writeText(text);
    toast(_i('toast_copied_clipboard','Đã copy vào clipboard'), 'success');
  } catch(e) { toast('Lỗi: '+e, 'danger'); }
}

// === Task Actions ===
async function claimTask(id) {
  if (!confirm(`Claim task ${id.substring(0,10)}?`)) return;
  try {
    const r = await fetch(`/api/task/${id}/claim`, {method:'POST'});
    const d = await r.json();
    if (d.ok) { toast(d.message, 'success'); loadDashboard(); openTaskDetail(id); }
    else { toast(d.message, 'warning'); }
  } catch(e) { toast('Lỗi: '+e, 'danger'); }
}
async function enqueueTask(id) {
  if (!confirm(`Enqueue task ${id.substring(0,10)}?`)) return;
  try {
    const r = await fetch(`/api/task/${id}/enqueue`, {method:'POST'});
    const d = await r.json();
    if (d.ok) { toast(d.message, 'success'); loadDashboard(); openTaskDetail(id); }
    else { toast(d.message, 'warning'); }
  } catch(e) { toast('Lỗi: '+e, 'danger'); }
}
async function completeTask(id) {
  if (!confirm(`Complete task ${id.substring(0,10)}?`)) return;
  try {
    const r = await fetch(`/api/task/${id}/complete`, {method:'POST'});
    const d = await r.json();
    if (d.ok) { toast(d.message, 'success'); loadDashboard(); openTaskDetail(id); }
    else { toast(d.message, 'warning'); }
  } catch(e) { toast('Lỗi: '+e, 'danger'); }
}

// === Cron Toggle ===
async function toggleCron(name) {
  try {
    const r = await fetch(`/api/cron/${encodeURIComponent(name)}/toggle`, {method:'POST'});
    const d = await r.json();
    toast(d.message, 'success');
    loadDashboard();
  } catch(e) { toast('Lỗi: '+e, 'danger'); }
}

// === Export ===
function exportTasks(format) {
  window.open(`/api/export/tasks?format=${format}`, '_blank');
}

// === Analytics ===
async function loadAnalytics() {
  try {
    const r = await fetch('/api/analytics');
    const d = await r.json();
    renderAnalytics(d);
  } catch(e) { /* silent */ }
}
function renderAnalytics(d) {
  const el = document.getElementById('analyticsRow');
  if (!el) return;
  const healthColor = d.health>=70?'var(--green)':d.health>=40?'var(--yellow)':'var(--red)';
  el.innerHTML = `
    <div class="analytics-card">
      <div class="health-ring" style="background:conic-gradient(${healthColor} ${d.health*3.6}deg, var(--surface3) 0)">${d.health}</div>
      <div>
        <div class="analytics-mini-label">${_i('health_score','Health Score')}</div>
        <div class="health-bar-wrap"><div class="health-bar-fill" style="width:${d.health}%;background:${healthColor}"></div></div>
      </div>
    </div>
    <div class="analytics-card">
      <div class="health-ring" style="background:conic-gradient(var(--green) ${d.completion_rate*3.6}deg, var(--surface3) 0)">${d.completion_rate}%</div>
      <div>
        <div class="analytics-mini-label">${_i('health_complete','Hoàn thành')}</div>
        <div class="analytics-mini-val" style="color:var(--green)">${d.done}<small style="font-size:.65rem;font-weight:400">/${d.total}</small></div>
      </div>
    </div>
    <div class="analytics-card">
      <div>
        <div class="analytics-mini-val" style="color:var(--red)">${d.stale}</div>
        <div class="analytics-mini-label">${_i('health_stale','Treo')}</div>
        <div class="analytics-mini-val" style="color:var(--yellow);margin-top:4px">${d.running}</div>
        <div class="analytics-mini-label">${_i('health_running','Đang chạy')}</div>
      </div>
    </div>`;
}

// === Toggle Analytics visibility ===
window._analyticsVisible = localStorage.getItem('analyticsVisible') !== 'false';
function toggleAnalytics() {
  window._analyticsVisible = !window._analyticsVisible;
  localStorage.setItem('analyticsVisible', window._analyticsVisible);
  const row = document.getElementById('analyticsRow');
  const toggle = document.getElementById('analyticsToggle');
  if (row) row.style.display = window._analyticsVisible ? '' : 'none';
  if (toggle) {
    toggle.querySelector('i').className = 'bi ' + (window._analyticsVisible ? 'bi-chevron-up' : 'bi-chevron-down');
    const txt = ' ' + (window._analyticsVisible ? _i('analytics_collapse','Thu gọn analytics') : _i('analytics_expand','Mở analytics'));
    if (toggle.childNodes[1]) toggle.childNodes[1].textContent = txt;
  }
}
(function initAnalytics() {
  if (!window._analyticsVisible) {
    document.getElementById('analyticsRow').style.display = 'none';
    var t = document.getElementById('analyticsToggle');
    if (t) { t.querySelector('i').className = 'bi bi-chevron-down'; t.childNodes[1].textContent = ' ' + _i('analytics_expand','Mở analytics'); }
  }
  document.getElementById('analyticsToggle').style.display = 'inline';
})();

// === Filter ===
window._taskFilter = 'all';
function setTaskFilter(val) {
  window._taskFilter = val;
  document.querySelectorAll('#taskFilterBar .filter-chip').forEach(c => c.classList.remove('active'));
  const activeChip = document.querySelector(`#taskFilterBar .filter-chip[onclick*="${val}"]`);
  if (activeChip) activeChip.classList.add('active');
  else document.getElementById('filterAll').classList.add('active');
  loadDashboard();
}

// === Global Search ===
let searchTimeout = null;
document.getElementById('searchInput').addEventListener('input', function() {
  const q = this.value.trim();
  if (q.length < 2) { document.getElementById('searchDropdown').classList.remove('show'); return; }
  clearTimeout(searchTimeout);
  searchTimeout = setTimeout(() => performSearch(q), 300);
});
document.getElementById('searchInput').addEventListener('blur', function() {
  setTimeout(() => { document.getElementById('searchDropdown').classList.remove('show'); }, 200);
});
document.getElementById('searchInput').addEventListener('focus', function() {
  const q = this.value.trim();
  if (q.length >= 2) performSearch(q);
});

async function performSearch(q) {
  // Also refresh kanban/outputs if active (replaces old searchInput listener)
  if (document.querySelector('#tab-kanban.active')) loadDashboard();
  if (document.querySelector('#tab-outputs.active')) loadOutputs();
  try {
    const r = await fetch(`/api/search?q=${encodeURIComponent(q)}`);
    const d = await r.json();
    const dd = document.getElementById('searchDropdown');
    let html = '';
    if (d.tasks && d.tasks.length) {
      html += '<div class="search-result-group">Tasks ('+d.tasks.length+')</div>';
      html += d.tasks.map(t => `<div class="search-result-item" onclick="openTaskDetail('${t.id}');document.getElementById('searchDropdown').classList.remove('show');document.getElementById('searchInput').value=''">${badge(t.status)} <span>${h(t.title).substring(0,50)}</span></div>`).join('');
    }
    if (d.files && d.files.length) {
      html += '<div class="search-result-group">Files ('+d.files.length+')</div>';
      html += d.files.map(f => `<div class="search-result-item" onclick="document.getElementById('searchDropdown').classList.remove('show');bootstrap.Tab.getInstance(document.getElementById('tab-files'))||(new bootstrap.Tab(document.getElementById('tab-files'))).show()"><i class="bi bi-file-earmark-text" style="color:var(--accent)"></i> <span>${h(f.name)}</span></div>`).join('');
    }
    if (d.workers && d.workers.length) {
      html += '<div class="search-result-group">Workers ('+d.workers.length+')</div>';
      html += d.workers.map(w => `<div class="search-result-item" onclick="document.getElementById('searchDropdown').classList.remove('show');bootstrap.Tab.getInstance(document.getElementById('tab-workers'))||(new bootstrap.Tab(document.getElementById('tab-workers'))).show()"><i class="bi bi-robot" style="color:var(--green)"></i> <span>${h(w.profile)}</span></div>`).join('');
    }
    if (!html) html = '<div class="search-no-result">Không tìm thấy kết quả</div>';
    dd.innerHTML = html;
    dd.classList.add('show');
  } catch(e) {}
}

// === Update loadDashboard to call analytics ===
const _origLoadDashboard = loadDashboard;
loadDashboard = async function() {
  await _origLoadDashboard();
  await loadAnalytics();
};

// === Enhanced Task Table ===
let _allTaskSort = 'created_at';
let _allTaskOrder = 'desc';
let _allTaskOffset = 0;
let _conversationsData = null;

async function loadAllTasks() {
  const status = document.getElementById('allTaskFilter').value;
  try {
    const r = await fetch(`/api/tasks/all?status=${status}&sort=${_allTaskSort}&order=${_allTaskOrder}&limit=50`);
    const d = await r.json();
    const el = document.getElementById('allTaskTable');
    document.getElementById('allTaskInfo').textContent = `${d.total} tasks`;
    document.getElementById('allTaskCountBadge').textContent = d.total;
    el.innerHTML = d.tasks.length
      ? d.tasks.map(t => `<tr>
          <td><input type="checkbox" class="allTaskCheck" value="${t.id}" onclick="event.stopPropagation()" onchange="updateSelected()"></td>
          <td style="font-size:.68rem;color:var(--text3);cursor:pointer" onclick="openTaskDetail('${t.id}')">${fmtTime(t.created_at)}</td>
          <td style="cursor:pointer" onclick="openTaskDetail('${t.id}')">${h(t.title||'(no title)').substring(0,60)}</td>
          <td>${assigneeCell(t.assignee)}</td>
          <td>${badge(t.status)}</td>
          <td style="font-size:.68rem;color:var(--text3)">${fmtRelative(t.started_at)}</td>
        </tr>`).join('')
      : '<tr><td colspan="6" class="empty-state">Không có task nào</td></tr>';
    // Load done count
    document.getElementById('doneTaskTable').innerHTML = d.tasks.filter(t => t.status === 'done').slice(0,20)
      .map(t => `<tr>
          <td><input type="checkbox" class="doneTaskCheck" value="${t.id}" onclick="event.stopPropagation()" onchange="updateSelected()"></td>
          <td style="cursor:pointer" onclick="openTaskDetail('${t.id}')">${h(t.title).substring(0,50)}</td>
          <td>${assigneeCell(t.assignee)}</td>
          <td style="font-size:.68rem;color:var(--text3)">${fmtTime(t.completed_at)}</td>
        </tr>`).join('') || '<tr><td colspan="4" class="empty-state">Chưa có task xong</td></tr>';
    const doneCount = d.total ? d.tasks.filter(t => t.status === 'done').length : 0;
    const totalCount = d.total || 0;
    document.getElementById('doneTaskCountBadge').textContent = doneCount || '0';
  } catch(e) {}
}

function sortAllTasks(col) {
  if (_allTaskSort === col) { _allTaskOrder = _allTaskOrder === 'asc' ? 'desc' : 'asc'; }
  else { _allTaskSort = col; _allTaskOrder = 'desc'; }
  document.getElementById('allTaskTable').innerHTML = '<tr><td colspan="5" class="empty-state">Đang tải...</td></tr>';
  loadAllTasks();
}

// === Conversations ===
async function loadConversations() {
  try {
    const r = await fetch('/api/conversations');
    _conversationsData = await r.json();
    document.getElementById('convRefreshLabel').textContent = new Date().toLocaleTimeString(currentLang==='en'?'en-US':'vi-VN');
    const el = document.getElementById('conversationTable');
    if (!_conversationsData || !_conversationsData.length) {
      el.innerHTML = '<tr><td colspan="8" class="empty-state">Không có hội thoại nào</td></tr>';
      return;
    }
    el.innerHTML = _conversationsData.map((c, i) => {
      const cost = c.estimated_cost_usd ? '$' + c.estimated_cost_usd.toFixed(4) : '—';
      return `<tr>
        <td>${avatar(c.profile,'sm')} ${h(c.profile)}</td>
        <td style="max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${h(c.title||'(no title)')}</td>
        <td style="font-size:.68rem;color:var(--text3)">${h(c.model||'—')}</td>
        <td>${c.message_count||0}</td>
        <td style="font-size:.68rem">${(c.input_tokens||0)+(c.output_tokens||0)}</td>
        <td style="font-size:.68rem">${cost}</td>
        <td style="font-size:.7rem;color:var(--text2)">${fmtRelative(c.started_at)}</td>
        <td><button class="btn btn-sm btn-outline-secondary py-0 px-2" onclick="openConversation('${c.profile}','${c.id}')"><i class="bi bi-eye"></i></button></td>
      </tr>`;
    }).join('');
  } catch(e) { document.getElementById('conversationTable').innerHTML = '<tr><td colspan="8" class="empty-state">Lỗi: '+e.message+'</td></tr>'; }
}

async function openConversation(profile, sessionId) {
  try {
    const r = await fetch(`/api/conversation/${encodeURIComponent(profile)}/${encodeURIComponent(sessionId)}`);
    const d = await r.json();
    if (!d.ok) { toast(d.message, 'danger'); return; }
    document.getElementById('convModalTitle').innerHTML = `<i class="bi bi-chat-dots me-1"></i>${h(d.session.title||_i('conv_session','Hội thoại'))} ${avatar(profile,'sm')} ${h(profile)}`;
    const messages = d.messages || [];
    document.getElementById('convModalBody').innerHTML = messages.length
      ? '<div class="conv-chat">'+messages.map(m => {
          const isUser = m.role === 'user';
          const isTool = m.role === 'tool';
          const content = m.content || '';
          const hasReasoning = m.reasoning && m.reasoning.trim();
          const side = isUser ? 'right' : (isTool ? 'center' : 'left');
          const bg = isUser ? 'var(--accent-subtle)' : (isTool ? 'var(--surface2)' : 'var(--surface)');
          const border = isUser ? 'var(--accent)' : 'var(--border)';
          return `<div class="conv-msg conv-${side}" style="background:${bg};border:1px solid ${border};border-radius:var(--radius-sm);padding:.5rem .65rem;margin-bottom:.5rem;max-width:${isTool?'100%':'80%'};margin-${side}:0">
            <div style="font-size:.62rem;color:var(--text3);margin-bottom:2px">${isUser?_i('conv_user','User'):_i('conv_agent','Agent')} · ${fmtTime(m.timestamp)}</div>
            <div class="output-block" style="max-height:300px;font-size:.76rem;background:transparent;border:none;padding:0;margin:0">${renderMd(content)}</div>
            ${hasReasoning ? `<details style="margin-top:4px;font-size:.68rem"><summary style="color:var(--text3);cursor:pointer">${_i('conv_reasoning','Reasoning')}</summary><pre style="margin:4px 0;padding:.4rem;font-size:.68rem;max-height:150px;overflow-y:auto;background:var(--bg2)">${h(m.reasoning)}</pre></details>` : ''}
            ${m.tool_calls ? `<div style="font-size:.65rem;color:var(--accent);margin-top:2px"><i class="bi bi-wrench"></i> ${h(m.tool_name||_i('conv_tool_call','tool_call'))}</div>` : ''}
          </div>`;
        }).join('')+'</div>'
      : '<div class="empty-state">'+_i('conv_no_msgs','Không có tin nhắn')+'</div>';
    new bootstrap.Modal(document.getElementById('conversationModal')).show();
  } catch(e) { toast('Lỗi: '+e, 'danger'); }
}

// === Delete Task ===
let _deletingTaskId = null;
function deleteTask(id) {
  _deletingTaskId = id;
  document.getElementById('deleteConfirmInput').value = '';
  document.getElementById('deleteTaskBtn').disabled = true;
  new bootstrap.Modal(document.getElementById('deleteTaskModal')).show();
}
async function confirmDeleteTask() {
  const id = _deletingTaskId;
  if (!id) return;
  const btn = document.getElementById('deleteTaskBtn');
  btn.disabled = true; btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Đang xoá...';
  try {
    const r = await fetch(`/api/task/${id}`, { method:'DELETE', headers:{'Content-Type':'application/json'}, body:JSON.stringify({confirm:'CONFIRM'}) });
    const d = await r.json();
    if (d.ok) {
      toast(d.message, 'success');
      bootstrap.Modal.getInstance(document.getElementById('deleteTaskModal')).hide();
      var tm = bootstrap.Modal.getInstance(document.getElementById('taskModal'));
      if (tm) tm.hide();
      loadDashboard();
    } else { toast(d.message, 'danger'); }
  } catch(e) { toast('Lỗi: '+e, 'danger'); }
  btn.disabled = false; btn.innerHTML = '<i class="bi bi-trash3 me-1"></i> Xoá vĩnh viễn';
}
async function deleteSelected() {
  const ids = getSelectedIds();
  if (!ids.length) { toast(_i('toast_select_del','Chọn task để xoá'), 'warning'); return; }
  if (!confirm(`Xoá ${ids.length} task? Hành động này không thể hoàn tác!`)) return;
  try {
    const r = await fetch('/api/tasks/bulk-delete', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({ids})});
    const d = await r.json();
    toast(d.message, d.ok ? 'success' : 'danger');
    loadDashboard();
  } catch(e) { toast('Lỗi: '+e, 'danger'); }
}

// === System Health ===
async function loadSystemHealth() {
  try {
    const r = await fetch('/api/system-health');
    const d = await r.json();
    document.getElementById('cpuVal').textContent = d.cpu.percent;
    document.getElementById('ramVal').textContent = d.memory.percent;
    document.getElementById('diskVal').textContent = d.disk.percent;
    document.getElementById('healthIndicator').style.display = 'inline-flex';
    window._healthData = d;
  } catch(e) { /* silent */ }
}
function showSystemHealth() {
  const d = window._healthData;
  if (!d) return;
  const fmt = (b) => b >= 1073741824 ? (b/1073741824).toFixed(1)+'GB' : (b/1048576).toFixed(0)+'MB';
  document.getElementById('systemHealthBody').innerHTML = `
    <div style="display:grid;gap:.75rem">
      <div><div class="analytics-mini-label">CPU</div><div class="analytics-mini-val">${d.cpu.percent}%</div><div class="health-bar-wrap"><div class="health-bar-fill" style="width:${d.cpu.percent}%;background:${d.cpu.percent>80?'var(--red)':'var(--accent)'}"></div></div><div style="font-size:.65rem;color:var(--text3)">${d.cpu.count} cores</div></div>
      <div><div class="analytics-mini-label">RAM</div><div class="analytics-mini-val">${d.memory.percent}%</div><div class="health-bar-wrap"><div class="health-bar-fill" style="width:${d.memory.percent}%;background:${d.memory.percent>80?'var(--red)':'var(--accent)'}"></div></div><div style="font-size:.65rem;color:var(--text3)">${fmt(d.memory.used)} / ${fmt(d.memory.total)}</div></div>
      <div><div class="analytics-mini-label">Disk</div><div class="analytics-mini-val">${d.disk.percent}%</div><div class="health-bar-wrap"><div class="health-bar-fill" style="width:${d.disk.percent}%;background:${d.disk.percent>80?'var(--red)':'var(--accent)'}"></div></div><div style="font-size:.65rem;color:var(--text3)">${fmt(d.disk.used)} / ${fmt(d.disk.total)}</div></div>
      <div><div class="analytics-mini-label">Uptime</div><div>${Math.floor(d.uptime/86400)}d ${Math.floor(d.uptime%86400/3600)}h ${Math.floor(d.uptime%3600/60)}m</div></div>
      <div><div class="analytics-mini-label">${_i('health_hermes_process','Hermes Process')}</div><div><span class="badge-dot" style="background:${d.hermes.dispatcher_running?'var(--green)1a':'var(--red)1a'};color:${d.hermes.dispatcher_running?'var(--green)':'var(--red)'};border:1px solid ${d.hermes.dispatcher_running?'var(--green)33':'var(--red)33'}"><span class="status-dot" style="background:${d.hermes.dispatcher_running?'var(--green)':'var(--red)'}"></span>${_i('health_hermes_disp','Dispatcher')} ${d.hermes.dispatcher_running?_i('health_hermes_disp_run','Chạy'):_i('health_hermes_disp_off','Tắt')}</span></div></div>
    </div>`;
  new bootstrap.Modal(document.getElementById('systemHealthModal')).show();
}

// === Auto Refresh ===
let _autoRefreshTimer = null;
function setAutoRefresh(seconds) {
  seconds = parseInt(seconds);
  localStorage.setItem('autoRefresh', seconds);
  if (_autoRefreshTimer) { clearInterval(_autoRefreshTimer); _autoRefreshTimer = null; }
  if (seconds > 0) {
    _autoRefreshTimer = setInterval(() => { loadDashboard(); }, seconds * 1000);
  }
}
(function initAutoRefresh() {
  const saved = localStorage.getItem('autoRefresh');
  if (saved !== null) {
    const sel = document.getElementById('autoRefreshSelect');
    sel.value = saved;
    setAutoRefresh(saved);
  } else { setAutoRefresh(10); }
})();

// === Update openCreateTaskModal with assignee autocomplete ===
const _origOpenCreateTaskModal = openCreateTaskModal;
openCreateTaskModal = async function() {
  _origOpenCreateTaskModal();
  try {
    const r = await fetch('/api/workers');
    const workers = await r.json();
    document.getElementById('workerDatalist').innerHTML = workers.map(w => `<option value="${h(w.profile)}">${w.profile} (${w.task_count} tasks)</option>`).join('');
  } catch(e) {}
};

// === Update selectAll toggle for delete button (count all checkbox classes) ===
const _origUpdateSelected = updateSelected;
updateSelected = function() {
  const nAll = document.querySelectorAll('.stale-check:checked, .allTaskCheck:checked, .doneTaskCheck:checked').length;
  const nStale = document.querySelectorAll('.stale-check:checked').length;
  // Original kill button (stale only)
  const killBtn = document.getElementById('killSelectedBtn');
  if (nStale) { killBtn.classList.remove('d-none'); document.getElementById('selectedCount').textContent = nStale; }
  else { killBtn.classList.add('d-none'); }
  // Delete button (all tables)
  const delBtn = document.getElementById('deleteSelectedBtn');
  if (nAll) { delBtn.classList.remove('d-none'); document.getElementById('deleteSelectedCount').textContent = nAll; }
  else { delBtn.classList.add('d-none'); }
};

// === Update tab switch for conversations sub-tabs + load on init ===
const _origTabHandler = document.querySelectorAll('[data-bs-toggle="tab"]');
document.querySelectorAll('[data-bs-toggle="tab"]').forEach(tab => {
  tab.addEventListener('shown.bs.tab', function(e) {
    if (e.target.id === 'tab-conversations') loadConversations();
  });
});

// === Add Notes tab in task detail (extend openTaskDetail) ===
window._taskDetail = null;
const _origOpenTaskDetail3 = openTaskDetail;
openTaskDetail = async function(id) {
  const r = await fetch(`/api/task/${id}`);
  const d = await r.json();
  window._taskDetail = d.task;
  await _origOpenTaskDetail3(id);
  // Add Notes tab after runs tab
  const tabsContainer = document.querySelector('.modal-tabs');
  const panesContainer = document.getElementById('modalBody');
  if (!tabsContainer || panesContainer.querySelector('#pane-notes')) return;
  const noteTab = document.createElement('button');
  noteTab.className = 'modal-tab';
  noteTab.innerHTML = '<i class="bi bi-sticky"></i> Notes';
  noteTab.onclick = function() { switchModalTab('notes'); return false; };
  tabsContainer.appendChild(noteTab);
  const notePane = document.createElement('div');
  notePane.className = 'modal-tab-pane';
  notePane.id = 'pane-notes';
  const task = window._taskDetail;
  notePane.innerHTML = `<div style="margin-bottom:6px;font-size:.72rem;color:var(--text2)"><i class="bi bi-info-circle"></i> Thêm ghi chú cho task này (lưu vào body field)</div>
    <textarea class="form-control" id="notesTextArea" rows="5" style="font-family:var(--font-sans);font-size:.82rem;background:var(--bg2);border-color:var(--border);color:var(--text);resize:vertical">${h((task&&task.body)||'')}</textarea>
    <div style="margin-top:6px"><button class="edit-save-btn" onclick="saveTaskNotes()"><i class="bi bi-check"></i> ${_i('btn_save','Lưu')} ${_i('modal_tab_notes','notes')}</button></div>`;
  panesContainer.insertBefore(notePane, panesContainer.querySelector('#pane-runs').nextSibling);
};

// === Sub-tab switching for System pane ===
function switchSubTab(name) {
  document.querySelectorAll('#pane-system .sub-pane').forEach(p => p.style.display = 'none');
  const target = document.getElementById('pane-sub-' + name);
  if (target) target.style.display = 'block';
  document.querySelectorAll('#taskSubTabs .nav-link').forEach(t => t.classList.remove('active'));
  const tab = document.getElementById('subtab-' + name);
  if (tab) tab.classList.add('active');
  if (name === 'all' || name === 'done') loadAllTasks();
}

// === Bulk checkbox toggle for all/done tables ===
function toggleAllCheckboxes(master, className) {
  document.querySelectorAll('.' + className).forEach(cb => cb.checked = master.checked);
  updateSelected();
}

// === Update getSelectedIds to include all checkboxes ===
const _origGetSelectedIds = getSelectedIds;
getSelectedIds = function() {
  const ids = _origGetSelectedIds();
  document.querySelectorAll('.allTaskCheck:checked, .doneTaskCheck:checked').forEach(cb => ids.push(cb.value));
  return ids;
};

async function saveTaskNotes() {
  const ta = document.getElementById('notesTextArea');
  if (!ta) return;
  const id = window._taskOutputData?.taskId;
  if (!id) return;
  try {
    const r = await fetch(`/api/task/${id}`, { method:'PATCH', headers:{'Content-Type':'application/json'}, body:JSON.stringify({body: ta.value}) });
    const d = await r.json();
    toast(d.message, d.ok ? 'success' : 'danger');
  } catch(e) { toast('Lỗi: '+e, 'danger'); }
}

setInterval(() => { initTooltips(); }, 2000);
initTooltips();
loadDashboard();
loadSystemHealth();
applyLangToStatic();
</script>
</body>
</html>"""

if __name__ == '__main__':
    port = int(os.environ.get('DASHBOARD_PORT', 8093))
    print(f"[cron] fallback path: {CRON_JSON}")
    print(f"[board] filter: {BOARD}")
    print(f"Monitoring Dashboard @ http://localhost:{port}")
    app.run(host='0.0.0.0', port=port, debug=False)
