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

from app.services.vault import read_vault_config, write_vault_config
from app.services.system import kill_by_pid, get_system_health
from app.services.database import (
    db_conn, DB_EXISTS,
    fetch_tasks_summary, fetch_task_detail, fetch_task_output, fetch_crons,
    _get_file_index,
    CRON_JSON, HERMES_HOME,
)

BOARD = "%"
VAULT_CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'vault-config.json')

VAULT_ROOT = os.environ.get('HERMES_VAULT_DIR', read_vault_config())
import app.services.database as _db
_db.VAULT_ROOT = VAULT_ROOT

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
    ok, msg = kill_by_pid(pid)
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
        ok, msg = kill_by_pid(pid)
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
    global VAULT_ROOT
    data = request.json or {}
    vault_dir = data.get('vault_dir', '').strip()
    try:
        write_vault_config(vault_dir)
        VAULT_ROOT = vault_dir
        _db.VAULT_ROOT = vault_dir
        _db._FILE_INDEX.clear()
        _db._FILE_INDEX_TIME = 0
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
    return jsonify(get_system_health())

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

LOGIN_PAGE = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'app', 'templates', 'login.html'), 'r', encoding='utf-8').read()
HTML_TEMPLATE = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'app', 'templates', 'dashboard.html'), 'r', encoding='utf-8').read()


if __name__ == '__main__':
    port = int(os.environ.get('DASHBOARD_PORT', 8093))
    print(f"[cron] fallback path: {CRON_JSON}")
    print(f"[board] filter: {BOARD}")
    print(f"Monitoring Dashboard @ http://localhost:{port}")
    app.run(host='0.0.0.0', port=port, debug=False)
