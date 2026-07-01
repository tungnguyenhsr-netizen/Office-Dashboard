# -*- coding: utf-8 -*-
import argparse
import json, os, re, subprocess, sys, time
from datetime import datetime, timezone, timedelta
from pathlib import Path
import urllib.request
from worker_model_config import (
    should_fallback, log_fallback, flag_for_insight,
    reset_fallback_cycle, model_override_payload,
    fallback_summary, parse_crash_reason, MAX_CONSECUTIVE_FALLBACKS,
    _fallback_state,
)

ROOT = Path(r'C:\Users\YOURNAME\Documents\YourVault\Efforts\Office-Dashboard')
STATE = ROOT / 'state' / 'last-action.json'
LOG = ROOT / 'logs' / ('orchestrator-%s.log' % datetime.now(timezone(timedelta(hours=7))).strftime('%Y-%m-%d'))
MONITOR = 'http://localhost:8093/api/dashboard'
DB = Path(os.path.expandvars(r'%LOCALAPPDATA%\hermes\kanban.db'))

# Ensure dirs
STATE.parent.mkdir(parents=True, exist_ok=True)
LOG.parent.mkdir(parents=True, exist_ok=True)

TZ = timezone(timedelta(hours=7))
NOW = lambda: int(time.time())

# ------------------------ helpers ------------------------

def log(msg: str):
    line = '[%s] %s' % (datetime.now(TZ).strftime('%H:%M:%S'), msg)
    print(line, flush=True)
    try:
        with open(LOG, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except Exception:
        pass


def load_state():
    try:
        if STATE.exists():
            return json.loads(STATE.read_text(encoding='utf-8'))
    except Exception:
        pass
    return {'cooldowns': {}, 'seen_task_ids': [], 'date': datetime.now().astimezone(TZ).date().isoformat()}


def save_state(state):
    try:
        STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
    except Exception:
        pass


def get(url, timeout=20):
    req = urllib.request.Request(url, headers={'Accept': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode('utf-8'))

def http_json(method, path, payload=None):
    url = 'http://localhost:8093' + path
    data = None
    if payload is not None:
        data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(url, data=data, method=method, headers={
        'Accept': 'application/json',
        'Content-Type': 'application/json',
    })
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode('utf-8'))


def db_conn():
    import sqlite3
    con = sqlite3.connect(DB, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def sql_exec(sql, args=(), readonly=True):
    con = db_conn()
    cur = con.cursor()
    if readonly:
        con.execute('PRAGMA query_only = ON')
    cur.execute(sql, args)
    rows = cur.fetchall()
    con.close()
    return rows


def ensure_audit_retry_column():
    try:
        sql_exec("PRAGMA table_info(tasks)", readonly=True)
        cols = [r[1] for r in sql_exec("PRAGMA table_info(tasks)", readonly=True)]
        if 'audit_retry_count' not in cols:
            sql_exec("ALTER TABLE tasks ADD COLUMN audit_retry_count INTEGER DEFAULT 0", args=(), readonly=False)
    except Exception:
        pass


def dispatcher_watchdog(max_age_sec=90):
    now = NOW()
    rows = sql_exec(
        """
        SELECT tr.task_id, tr.last_heartbeat_at, tr.worker_pid
        FROM task_runs tr
        WHERE tr.status = 'running'
        ORDER BY tr.started_at DESC
        LIMIT 5
        """
    )
    if not rows:
        return False
    latest = max((r['last_heartbeat_at'] or 0) for r in rows)
    if latest and (now - latest) > max_age_sec:
        log('[watchdog] dispatcher restarted at %s' % datetime.now(TZ).isoformat())
        return True
    return False


def rows_to_dicts(rows):
    return [dict(r) for r in rows]


# ------------------------ output audit rule ------------------------

def _normalize_artifacts(artifacts):
    out = []
    for a in artifacts or []:
        a = (a or '').strip()
        if not a:
            continue
        if a.startswith('[') and a.endswith(']'):
            try:
                inner = json.loads(a)
                if isinstance(inner, list):
                    inner = _normalize_artifacts(inner)
                    out.extend(inner)
                    continue
            except Exception:
                pass
        elif a.startswith('{') and a.endswith('}'):
            try:
                inner = json.loads(a)
                path = inner.get('path') or inner.get('from') or inner.get('to')
                if path:
                    a = str(path).strip()
            except Exception:
                pass
        out.append(a)
    return out


def _try_resolve_artifact_under_efforts(task_id, title, missing):
    """Fallback: if default DB path is missing, try locate the file anywhere under Efforts."""
    try:
        base = ROOT.parent / 'Efforts'
        needles = set()
        for p in missing:
            rel = Path(p)
            needles.add(rel.name.lower())
            if title:
                needles.add(('%s.md' % title).lower())
        if not needles:
            return None
        for md in base.rglob('*.md'):
            name = md.name.lower()
            if name in needles:
                return str(md)
        for sub in ['Insights', 'General', 'ExampleBrand', 'ExampleBrand', 'Research', 'PLACEHOLDER']:
            d = base / sub
            if not d.exists():
                continue
            for md in d.rglob('*.md'):
                name = md.name.lower()
                if name in needles:
                    return str(md)
    except Exception:
        pass
    return None


def audit_completed_outputs(max_age_sec=6*60*60):
    cutoff = NOW() - max_age_sec
    rows = sql_exec(
        """
        SELECT te.task_id,
               te.payload,
               te.created_at,
               te.kind,
               te.id AS event_id,
               t.status,
               t.assignee,
               t.title
        FROM task_events te
        JOIN tasks t ON t.id = te.task_id
        WHERE te.kind = 'completed'
          AND te.created_at >= ?
        ORDER BY te.created_at DESC
        """,
        (cutoff,),
    )
    issues = []
    no_artifact_count = 0
    for r in rows_to_dicts(rows):
        artifacts = []
        reason = None
        task_type = None
        explicit_no_artifacts = False
        payload_raw = (r.get('payload') or '').strip()
        try:
            payload = json.loads(payload_raw) if payload_raw.startswith('{') else {}
            task_type = (payload.get('task_type') or payload.get('type') or '').strip()
            art = payload.get('artifacts')
            if art is None or art == [] or art == '':
                explicit_no_artifacts = True
                if task_type:
                    reason = 'no_artifacts'
                else:
                    reason = 'no_artifacts_or_task_type'
            elif isinstance(art, list):
                artifacts = _normalize_artifacts(art)
            else:
                explicit_no_artifacts = True
                reason = 'invalid_artifacts_type'
        except Exception:
            reason = 'invalid_payload'
        if explicit_no_artifacts and reason == 'no_artifacts':
            no_artifact_count += 1
        missing = []
        empty = []
        for p in artifacts:
            try:
                path = Path(p).expanduser().resolve()
            except Exception:
                missing.append(str(p))
                continue
            try:
                if not path.exists():
                    missing.append(str(p))
                elif not path.is_file() or path.stat().st_size == 0:
                    empty.append(str(p))
            except Exception:
                missing.append(str(p))
        # Fallback: if DB path is missing, try discover the artifact under Efforts folder
        updated = False
        if missing:
            resolved_fallback = _try_resolve_artifact_under_efforts(r.get('task_id'), r.get('title'), missing)
            if resolved_fallback and Path(resolved_fallback).exists():
                updated = True
                artifacts = [resolved_fallback]
                missing = []
                empty = []
                try:
                    new_payload = json.loads((r.get('payload') or '').strip()) if (r.get('payload') or '').strip().startswith('{') else {}
                    new_payload['artifacts'] = artifacts
                    sql_exec(
                        "UPDATE task_events SET payload=? WHERE id=?",
                        (json.dumps(new_payload, ensure_ascii=False), r.get('event_id')),
                        readonly=False,
                    )
                except Exception:
                    pass
        if reason or missing or empty:
            issue = {
                'task_id': r.get('task_id'),
                'title': r.get('title'),
                'assignee': r.get('assignee'),
                'missing': missing,
                'empty': empty,
                'reason': reason,
                'age_sec': max(0, NOW() - r.get('created_at', 0)),
                'explicit_no_artifacts': explicit_no_artifacts,
                'event_id': r.get('event_id'),
                'task_status': r.get('status'),
            }
            if task_type:
                issue['task_type'] = task_type
            issues.append(issue)
    if no_artifact_count:
        log('[audit] no_artifacts_detected count=%s' % no_artifact_count)
    return issues


def audit_completed_outputs_raw(max_age_sec=6*60*60):
    cutoff = NOW() - max_age_sec
    rows = sql_exec(
        """
        SELECT te.task_id,
               te.payload,
               te.created_at,
               te.kind,
               te.id AS event_id,
               t.status,
               t.assignee,
               t.title
        FROM task_events te
        JOIN tasks t ON t.id = te.task_id
        WHERE te.kind = 'completed'
          AND te.created_at >= ?
        ORDER BY te.created_at DESC
        """,
        (cutoff,),
    )
    no_artifact_count = 0
    for r in rows_to_dicts(rows):
        payload_raw = (r.get('payload') or '').strip()
        try:
            payload = json.loads(payload_raw) if payload_raw.startswith('{') else {}
        except Exception:
            continue
        art = payload.get('artifacts')
        if art is None or art == [] or art == '':
            no_artifact_count += 1
            log('[audit] no_artifacts task=%s reason=no_artifacts' % r.get('task_id'))
    if no_artifact_count:
        log('[audit] no_artifacts_raw count=%s' % no_artifact_count)
    return no_artifact_count


def audit_requeue_failed_outputs():
    ensure_audit_retry_column()
    issues = audit_completed_outputs()
    results = []
    for issue in issues:
        task_id = issue.get('task_id')
        if issue.get('explicit_no_artifacts'):
            log('[audit] skip no_artifacts requeue task=%s reason=%s' % (task_id, issue.get('reason')))
            continue
        missing = issue.get('missing') or []
        empty = issue.get('empty') or []
        if not missing and not empty:
            continue
        # Skip tasks that legitimately have no artifacts and no task type metadata.
        # Invalid payloads or artifact types still run through retry/dead-letter flow.
        if issue.get('explicit_no_artifacts') and issue.get('reason') in ('no_artifacts', 'no_artifacts_or_task_type'):
            log('[audit] skip no_artifacts requeue task=%s reason=%s' % (task_id, issue.get('reason')))
            continue
        # Invalid artifacts or payloads are surfaced as missing so the data can be regenerated.
        if issue.get('explicit_no_artifacts') and issue.get('reason') in ('invalid_artifacts_type', 'invalid_payload') and not missing and not empty:
            missing = ['task_event_id=%s' % issue.get('event_id')]
        try:
            rows = sql_exec("SELECT audit_retry_count FROM tasks WHERE id=?", (task_id,))
            row = rows[0] if rows else {}
            current = (row.get('audit_retry_count') or 0) if isinstance(row, dict) else (row[0] if row else 0)
        except Exception:
            current = 0
        if current >= 2:
            log('[audit] dead_letter task=%s reason=missing_output_or_invalid retries=%s' % (task_id, current))
            results.append({'task_id': task_id, 'status': 'dead_letter'})
            continue
        try:
            sql_exec("UPDATE tasks SET status='ready', audit_retry_count=COALESCE(audit_retry_count,0)+1, claim_lock='', claim_expires=NULL, completed_at=NULL WHERE id=?", (task_id,), readonly=False)
            log('[audit] re-queued %s attempt=%s missing=%s empty=%s' % (task_id, current + 1, missing, empty))
            results.append({'task_id': task_id, 'status': 'ready', 'retry': current + 1})
        except Exception as e:
            log('[audit] requeue error task=%s error=%s' % (task_id, e))
            results.append({'task_id': task_id, 'status': 'error', 'error': str(e)})
    return results


# ------------------------ project output routing ------------------------

import shutil


_PROJECT_ALIASES = {
    'examplebrand': 'ExampleBrand',
    'examplebrand-july-campaign': 'ExampleBrand',
    'examplebrand': 'ExampleBrand',
    'hatsociety': 'ExampleBrand',
    'dyc': 'ExampleBrand',
    'dyc-vietnam': 'ExampleBrand',
    'placeholder': 'PLACEHOLDER',
    'placeholder-studio': 'PLACEHOLDER',
    'minh-tung-studio': 'PLACEHOLDER',
    'research': 'Research',
    'content-factory': 'General',
    'content-strategist': 'General',
    'ai-os': 'General',
    'default': 'General',
}


def _detect_project(task: dict):
    assignee = (task.get('assignee') or '').strip().lower()
    title = (task.get('title') or '').lower()
    body = (task.get('body') or '').lower()
    model = {}
    try:
        model = json.loads(task.get('model_override') or '{}') or {}
    except Exception:
        pass
    # prefer aliases resolved from assignee first
    project = _PROJECT_ALIASES.get(assignee)
    if not project:
        for key, alias in _PROJECT_ALIASES.items():
            if key in title or key in body or key in assignee or key in str(model):
                project = alias
                break
    return project or 'General'


def _project_dir(project: str):
    return ROOT.parent / 'Efforts' / project


def _move_artifacts(artifacts, project: str) -> dict:
    project_dir = _project_dir(project)
    moved = []
    errors = []
    for p in artifacts:
        src = Path(p)
        if not src.exists():
            continue
        try:
            rel = src.name
            dst = project_dir / rel
            project_dir.mkdir(parents=True, exist_ok=True)
            if dst.exists() and dst.stat().st_size > 0 and dst.resolve() != src.resolve():
                ts = datetime.now(TZ).strftime('%Y%m%d-%H%M%S')
                dst = project_dir / f"{src.stem}_{ts}{src.suffix}"
            shutil.move(str(src), str(dst))
            moved.append({'from': str(src), 'to': str(dst)})
        except Exception as e:
            errors.append({'from': str(src), 'error': str(e)})
    return {'moved': moved, 'errors': errors}


def reroute_completed_outputs(max_age_sec=6*60*60):
    cutoff = NOW() - max_age_sec
    rows = sql_exec(
        """
        SELECT te.task_id, te.payload AS event_payload, te.id AS event_id, t.*
        FROM task_events te
        JOIN tasks t ON t.id = te.task_id
        WHERE te.kind = 'completed'
          AND te.created_at >= ?
        ORDER BY te.created_at DESC
        """,
        (cutoff,),
    )
    results = []
    for r in rows_to_dicts(rows):
        artifacts = []
        payload = {}
        try:
            payload = json.loads(r.get('event_payload') or '{}') or {}
            artifacts = [a for a in (payload.get('artifacts') or []) if a]
        except Exception:
            continue
        if not artifacts:
            continue
        project = _detect_project(r)
        project_dir = _project_dir(project)
        under_project = all(
            Path(p).resolve().as_posix().startswith(project_dir.resolve().as_posix()) for p in artifacts
        )
        if under_project:
            continue
        route = _move_artifacts(artifacts, project)
        if route.get('moved'):
            try:
                new_payload = dict(payload)
                new_payload['artifacts'] = [m['to'] for m in route['moved']]
                sql_exec(
                    "UPDATE task_events SET payload=? WHERE id=?",
                    (json.dumps(new_payload, ensure_ascii=False), r.get('event_id')),
                    readonly=False,
                )
            except Exception:
                pass
        log('reroute task=%s project=%s moved=%d errors=%d' % (r.get('task_id'), project, len(route.get('moved', [])), len(route.get('errors', []))))
        results.append({'task_id': r.get('task_id'), 'project': project, 'route': route})
    return results


# ------------------------ atlas insight report ------------------------

_INSIGHT_DIR = ROOT.parent / 'Insights'


def _read_index_cache(max_age_sec=4*60*60):
    idx = ROOT.parent / 'Atlas' / 'vault-index.json'
    try:
        if idx.exists() and (NOW() - idx.stat().st_mtime) < max_age_sec:
            lines = idx.read_text(encoding='utf-8')
            return json.loads(lines)
    except Exception:
        pass
    return None


def _load_me():
    for p in [ROOT.parent / 'Atlas' / 'Me.md']:
        try:
            if p.exists():
                return p.read_text(encoding='utf-8')
        except Exception:
            pass
    return ''


def _load_task_events(max_n=25):
    rows = sql_exec(
        """
        SELECT te.task_id, te.kind, te.created_at, t.assignee, t.title
        FROM task_events te
        JOIN tasks t ON t.id = te.task_id
        ORDER BY te.created_at DESC
        LIMIT ?
        """,
        (max_n,),
    )
    return rows_to_dicts(rows)


def _format_ts(ts):
    return datetime.fromtimestamp(ts, tz=TZ).strftime('%Y-%m-%d %H:%M')


def _safe_wiki(path: str):
    p = Path(path)
    vault_root = ROOT.parent.parent
    try:
        rel = p.relative_to(vault_root)
        text = str(rel)
        if text.lower().endswith('.md'):
            text = text[:-3]
        return '[[%s]]' % text.replace('\\', '/')
    except Exception:
        text = str(p).replace('\\', '/')
        if text.lower().endswith('.md'):
            text = text[:-3]
        return text


def write_insight_report(fb_state=None):
    if fb_state is None:
        fb_state = _fallback_state()
    today = datetime.now(TZ).strftime('%Y-%m-%d')
    report_path = _INSIGHT_DIR / f'{today}.md'
    _INSIGHT_DIR.mkdir(parents=True, exist_ok=True)
    _INSIGHT_DIR.mkdir(parents=True, exist_ok=True)

    index = _read_index_cache()
    me_text = _load_me()
    events = _load_task_events(25)

    # infer active projects
    projects = {'General'}
    if index and isinstance(index, dict):
        for f in index.get('files', []):
            for pr in f.get('projects', []) or []:
                projects.add(pr)
    for e in events:
        a = (e.get('assignee') or '').lower()
        if 'dyc' in a or 'hat' in a or 'hatsociety' in a:
            projects.add('ExampleBrand')
        if 'placeholder' in a:
            projects.add('PLACEHOLDER')
        if 'examplebrand' in a:
            projects.add('ExampleBrand')
        if 'research' in a:
            projects.add('Research')
    projects.discard('')
    project_list = sorted(list(projects))[:12]

    # group recent events by project keywords
    buckets = {p: [] for p in project_list}
    gaps = []
    for e in events:
        kind = e.get('kind')
        title = (e.get('title') or '')
        assignee = (e.get('assignee') or '').lower()
        ts = _format_ts(e.get('created_at', 0) or 0)
        for p in project_list:
            k = p.lower()
            if k in assignee or k in title.lower():
                if kind == 'blocked':
                    buckets[p].append('- %s `blocked`: %s' % (ts, title))
                    gaps.append((p, title))
                elif kind == 'completed':
                    buckets[p].append('- %s `completed`: %s' % (ts, title))
                elif kind == 'claimed':
                    buckets[p].append('- %s `claimed`: %s' % (ts, title))
                elif kind == 'reclaimed':
                    buckets[p].append('- %s `reclaimed`: %s' % (ts, title))
                else:
                    buckets[p].append('- %s `%s`: %s' % (ts, kind, title))
                break

    body_lines = []
    body_lines.append('## Task activity')
    body_lines.append('')
    body_lines.append('## Gaps & blockers')
    if not gaps:
        body_lines.append('_No blocked tasks detected._')
    else:
        seen = set()
        for p, title in gaps[:20]:
            key = (p, title)
            if key in seen:
                continue
            seen.add(key)
            body_lines.append('- [%s] %s' % (p, title))
    body_lines.append('')
    body_lines.append('## Source')
    body_lines.append('- %s' % _safe_wiki('Atlas/Me.md'))
    body_lines.append('- %s' % _safe_wiki('Atlas/vault-index.json'))
    body_lines.append('')
    # —— Model Health ——
    fb_text = fallback_summary()
    if fb_text:
        body_lines.append('## Model Health')
        body_lines.append(fb_text)
        body_lines.append('')

    run_header = '## Run %s' % datetime.now(TZ).strftime('%H:%M')
    run_block = [''] + [run_header] + [''] + body_lines + ['']
    try:
        if report_path.exists() and report_path.stat().st_size > 0:
            existing = report_path.read_text(encoding='utf-8')
            if run_header in existing:
                return {'path': str(report_path), 'projects': project_list, 'items': len(events)}
            content = existing.rstrip('\n') + '\n' + '\n'.join(run_block)
        else:
            header = [
                '---',
                'title: "Insights %s"' % today,
                'type: insight',
                'created: %s' % today,
                'updated: %s' % today,
                'tags: [insight]',
                'projects: %s' % json.dumps(project_list),
                '---',
                '',
                '# Insights %s' % today,
                '',
            ]
            content = '\n'.join(header + run_block)
        report_path.write_text(content, encoding='utf-8')
        try:
            log('insight_report path=%s size=%s' % (report_path, report_path.stat().st_size))
        except Exception:
            pass
        return {'path': str(report_path), 'projects': project_list, 'items': len(events)}
    except Exception as e:
        log('insight write error: %s' % e)
        return {'error': str(e)}


# ------------------------ workspace_kind auto-fix ------------------------
# Core Hermes (kanban_db.py) only accepts: scratch, worktree, dir
# If tasks land with 'board' or 'workspace' (invalid), dispatch silently fails
# with "unknown workspace_kind". This fix runs before every orchestrator cycle.
VALID_CORE_WORKSPACE_KINDS = {'scratch', 'worktree', 'dir'}

def fix_invalid_workspace_kinds():
    con = db_conn()
    cur = con.cursor()
    try:
        # Find tasks with invalid workspace_kind that are stuck (not archived/done)
        rows = cur.execute(
            """SELECT id, title, status, workspace_kind, consecutive_failures
               FROM tasks
               WHERE status NOT IN ('done', 'archived')
                 AND workspace_kind NOT IN ('scratch', 'worktree', 'dir')
               ORDER BY created_at DESC
               LIMIT 50"""
        ).fetchall()
        fixed = 0
        for r in rows:
            tid = r['id']
            old_kind = r['workspace_kind']
            cur.execute(
                """UPDATE tasks
                   SET workspace_kind = 'scratch',
                       consecutive_failures = 0,
                       last_failure_error = NULL
                   WHERE id = ?""",
                (tid,)
            )
            # Log the fix as a task event so it's traceable
            try:
                cur.execute(
                    """INSERT INTO task_events (task_id, run_id, kind, payload, created_at)
                       VALUES (?, NULL, 'auto_fix', ?, ?)""",
                    (tid, json.dumps({
                        'action': 'workspace_kind_fix',
                        'from': old_kind,
                        'to': 'scratch',
                    }), NOW())
                )
            except Exception:
                pass
            fixed += 1
            log('workspace_fix task=%s kind=%s->scratch title=%s' % (tid, old_kind, r['title'][:50]))
        con.commit()
        if fixed:
            log('workspace_fix total=%d' % fixed)
        return fixed
    except Exception as e:
        log('workspace_fix error: %s' % e)
        return 0
    finally:
        con.close()


# ------------------------ detection rules ------------------------

def detect_anomalies(dashboard):
    anomalies = list(dashboard.get('stale_running', []))
    if not anomalies:
        return anomalies
    # Keep only anomalies we care about
    out = []
    for item in anomalies:
        status = item.get('status')
        if status in ('stale', 'running', 'blocked'):
            out.append({
                'kind': 'live_run',
                'task_id': item.get('id'),
                'title': item.get('title'),
                'assignee': item.get('assignee'),
                'status': status,
                'reason': item.get('reason'),
                'alive': item.get('alive'),
                'age_human': item.get('age_human'),
            })
    return out


def detect_recent_crashes(hours=6):
    cutoff = NOW() - hours * 3600
    rows = sql_exec("""
        SELECT tr.task_id, tr.status, tr.error, tr.started_at, tr.ended_at, tr.last_heartbeat_at, t.title, t.assignee
        FROM task_runs tr
        JOIN tasks t ON t.id = tr.task_id
        WHERE tr.started_at >= ? AND tr.status = 'crashed'
        ORDER BY tr.started_at DESC
    """, (cutoff,))
    out = []
    for r in rows_to_dicts(rows):
        out.append({
            'kind': 'recent_crash',
            'task_id': r.get('task_id'),
            'title': r.get('title'),
            'assignee': r.get('assignee'),
            'status': 'crashed',
            'reason': r.get('error'),
            'started_at': r.get('started_at'),
            'ended_at': r.get('ended_at'),
            'last_heartbeat_at': r.get('last_heartbeat_at'),
        })
    return out


# ------------------------ eligibility gate ------------------------

_DOMAIN_KEYWORDS = {
    'PLACEHOLDER': ['placeholder', 'bag', 'bags', 'hàng', 'kho', 'order', 'orders', 'qc', 'factory', 'nhà máy', 'nguyên liệu', 'đóng hàng'],
    'ExampleBrand': ['examplebrand', 'content', 'calendar', 'reels', 'tiktok', 'ads', 'kol', 'brief', 'ugc', 'partner', 'handoff', 'campaign'],
    'ExampleBrand': ['examplebrand', 'headwear', 'hat', 'packaging', 'tk', 'shopee', 'tiktok'],
}

_WEAK_TITLE_RE = re.compile(r'^[A-Za-zÀ-ỹ0-9\s]{0,12}$')  # too short / too generic


def gating(task: dict) -> dict:
    """Return {'pass': bool, 'reasons': [str]}"""
    reasons = []
    title = (task.get('title') or '').strip()
    body = (task.get('body') or '').strip()
    assignee = (task.get('assignee') or '').strip().lower()
    result = (task.get('result') or '').strip()
    meta = {}
    try:
        meta = json.loads(task.get('model_override') or '{}')
    except Exception:
        pass

    # 1) missing input
    missing = []
    if not body:
        missing.append('body')
    if not result:
        missing.append('expected_output/result')
    if not missing:
        pass
    if missing:
        reasons.append('missing:%s' % ','.join(missing))

    # 2) context mismatch
    context_terms = ' '.join([title, body, task.get('workspace_path') or '']).lower()
    allowed_non_domain = bool(re.match(r'^(content-factory|ai-os|content-strategist|dyc-vietnam|examplebrand-campaign|default)$', assignee))
    domain_match = allowed_non_domain
    if not domain_match:
        for domain, kws in _DOMAIN_KEYWORDS.items():
            if any(k in context_terms for k in kws):
                domain_match = True
                break
    if not domain_match:
        reasons.append('context:no-domain-match')

    # 4) workspace kind check — core Hermes only accepts scratch/worktree/dir
    ws = (task.get('workspace_kind') or '').strip().lower()
    if ws not in VALID_CORE_WORKSPACE_KINDS:
        reasons.append('invalid:workspace_kind')

    # 3) attractiveness
    if not title or _WEAK_TITLE_RE.match(title):
        reasons.append('weak:title')
    if 'acceptance criteria' not in body.lower() and 'chấp nhận' not in body.lower():
        reasons.append('weak:acceptance-missing')
    # vault link markers
    if '[[Atlas/' not in body and '[[Calendar/' not in body and '[[Efforts/' not in body:
        reasons.append('weak:vault-link-missing')

    return {'pass': len(reasons) == 0, 'reasons': reasons}


# ------------------------ stale reaper ------------------------

def reap_stale_claims(max_age_sec=180):
    now = NOW()
    rows = sql_exec(
        "SELECT id, claim_lock, claim_expires FROM tasks WHERE status='running' AND claim_lock IS NOT NULL AND claim_lock!='' AND claim_expires < ?",
        (now,),
    )
    reclaimed = 0
    for r in rows:
        try:
            sql_exec(
                "UPDATE tasks SET status='ready', claim_lock='', claim_expires=NULL WHERE id=?",
                (r['id'],),
                readonly=False,
            )
            reclaimed += 1
        except Exception:
            pass
    if reclaimed:
        log('reap_stale_claims reclaimed=%d' % reclaimed)
    return reclaimed


# ------------------------ actions ------------------------

COOLDOWN_SEC = 30 * 60  # 30 minutes
COOLDOWN_AFTER_RETRIES = 60 * 60  # 1 hour cooldown after multiple claims
RETRY_WINDOW = 10 * 60  # 10 minutes window
MAX_RETRIES_IN_WINDOW = 3  # max claims within window before cooldown


def can_act(state, task_id: str) -> bool:
    return task_id not in state.get('cooldowns', {})


def mark_acted(state, task_id: str):
    state.setdefault('cooldowns', {})
    state['cooldowns'][task_id] = NOW()
    # prune cooldowns older than 24h
    cutoff = NOW() - 24*60*60
    state['cooldowns'] = {k: v for k, v in state['cooldowns'].items() if v >= cutoff}


def gatekeeper_comment(task_id: str, reasons):
    existing = sql_exec("SELECT body FROM task_comments WHERE task_id = ? ORDER BY created_at DESC LIMIT 1", (task_id,))
    if existing and 'needs-info' in (existing[0]['body'] if existing else ''):
        return
    body = 'needs-info: ' + '; '.join(reasons)
    ts = NOW()
    sql_exec(
        "INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?,?,?,?)",
        (task_id, 'orchestrator', body, ts),
        readonly=False,
    )
    log('gatekeeper: commented task=%s body=%s' % (task_id, body))


def write_plan(task_id: str, action: str, meta: dict):
    plan = {
        'task_id': task_id,
        'action': action,
        'ts': datetime.now(TZ).isoformat(),
        'meta': meta,
    }
    p = ROOT / 'logs' / ('plan-%s.json' % task_id)
    try:
        data = []
        if p.exists():
            data = json.loads(p.read_text(encoding='utf-8'))
        data.append(plan)
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    except Exception:
        pass


# ------------------------ dispatcher check ------------------------

def dispatcher_check():
    """Ensure the kanban dispatcher daemon is running. Start it if not."""
    import subprocess
    now = NOW()
    try:
        # check dispatcher log for activity in the last 90 seconds
        log_path = Path(os.path.expandvars(r'%LOCALAPPDATA%\hermes\logs\dispatcher.log'))
        if log_path.exists():
            age = now - log_path.stat().st_mtime
            if age < 90:
                return  # dispatcher is active
        # no recent log — cross-check via process list
        result = subprocess.run(
            ['tasklist', '/FI', 'IMAGENAME eq hermes.exe', '/NH', '/FO', 'CSV'],
            capture_output=True, text=True, timeout=10
        )
        # If we see hermes processes but no recent log, log a warning
        # and attempt a restart anyway
    except Exception:
        pass

    # try to start the dispatcher daemon
    try:
        subprocess.Popen(
            ['hermes', 'kanban', 'daemon', '--force', '--interval', '5'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.DETACHED_PROCESS,
        )
        log('dispatcher_check: dispatcher started (daemon)')
    except Exception as e:
        log('dispatcher_check: start failed — %s' % e)


# ------------------------ main ------------------------

def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--audit-only', action='store_true', help='Run only audit checks, skip dispatcher/backfill/insight/re-queue writes')
    parser.add_argument('--audit-max-age-hours', type=float, default=6.0, help='Max age in hours for completed audits')
    args = parser.parse_args()
    state = load_state()
    if state.get('date') != datetime.now(TZ).date().isoformat():
        state = {'cooldowns': {}, 'seen_task_ids': [], 'date': datetime.now(TZ).date().isoformat()}

    if getattr(args, 'audit_only', False):
        max_age_sec = max(1, int(getattr(args, 'audit_max_age_hours', 6.0) * 3600))
        audit_issues = []
        try:
            audit_issues = audit_completed_outputs(max_age_sec=max_age_sec)
        except Exception as e:
            log('[audit] audit_only_error: %s' % e)
        if audit_issues:
            for issue in audit_issues:
                log('output_audit task=%s title=%s missing=%s empty=%s reason=%s explicit_no_artifacts=%s' % (
                    issue.get('task_id'), issue.get('title'), issue.get('missing'), issue.get('empty'),
                    issue.get('reason'), issue.get('explicit_no_artifacts'),
                ))
        raw_no_artifact_count = 0
        try:
            raw_no_artifact_count = audit_completed_outputs_raw(max_age_sec=max_age_sec)
        except Exception as e:
            log('[audit] audit_only_raw_error: %s' % e)
        log('[audit] audit_only_done issues=%d no_artifacts_count=%d age_hours=%s' % (len(audit_issues), raw_no_artifact_count, getattr(args, 'audit_max_age_hours', 6.0)))
        return

    # Dispatcher health check — must run before strategist pass, every cycle
    dispatcher_check()

    # Workspace kind auto-fix — fix tasks with invalid workspace_kind before dispatch
    try:
        fix_invalid_workspace_kinds()
    except Exception as e:
        log('workspace_fix error (non-blocking): %s' % e)

    # Strategist pass: tag pending tasks with model hints
    try:
        sp = ROOT / 'scripts' / 'strategist.py'
        if sp.exists():
            import runpy
            runpy.run_path(str(sp), run_name='__strategist__')
            log('strategist pass completed')
        else:
            log('strategist.py not found — skipping')
    except Exception as e:
        log('strategist error (non-blocking): %s' % e)

    log('orchestrator start')
    reap_stale_claims()
    try:
        audit_issues = audit_completed_outputs()
    except Exception as e:
        audit_issues = []
        log('audit error: %s' % e)
    if audit_issues:
        for issue in audit_issues:
            log('output_audit task=%s title=%s missing=%s empty=%s' % (
                issue.get('task_id'),
                issue.get('title'),
                issue.get('missing'),
                issue.get('empty'),
            ))
    try:
        reroute_completed_outputs()
    except Exception as e:
        log('reroute error: %s' % e)
    try:
        audit_requeue = audit_requeue_failed_outputs()
        if audit_requeue:
            log('audit_requeue_count=%s' % len(audit_requeue))
    except Exception as e:
        log('audit_requeue error: %s' % e)
    try:
        data = get(MONITOR)
    except Exception as e:
        log('monitor fetch error: %s' % e)
        data = {}

    anomalies = detect_anomalies(data)
    log('anomalies=%d' % len(anomalies))

    # —— Model fallback detection from crash reasons ——
    for a in anomalies:
        crash_reason = parse_crash_reason(str(a.get('reason', '')) + ' ' + str(a.get('alive', '')))
        if crash_reason and should_fallback(crash_reason):
            fb = log_fallback('task=%s reason=%s' % (a.get('task_id', '?'), crash_reason))
            log('model_fallback task=%s consecutive=%d/%d' % (
                a.get('task_id'), fb.get('consecutive'), fb.get('threshold')))

    # Collect candidate task_ids: stale_run + task_runs crashed + no-run completed
    seen = set(state.get('seen_task_ids', []))
    new_ids = []
    for a in anomalies:
        tid = a.get('task_id')
        if not tid:
            continue
        if tid in seen:
            continue
        new_ids.append(tid)

    if not new_ids:
        # backlog backfill: nếu backlog thấp thì tự sinh task mới
        try:
            ready_count = data.get('tasks_summary', {}).get('active_count', 0)
        except Exception:
            ready_count = 0
        if int(ready_count) < 5 and can_act(state, 'backfill'):
            try:
                import runpy
                script = ROOT / 'scripts' / 'backfill_tasks.py'
                if script.exists():
                    runpy.run_path(str(script), run_name='__backfill__')
                    mark_acted(state, 'backfill')
                    log('backfill ran ready_count=%s' % ready_count)
            except Exception as e:
                log('backfill error: %s' % e)
        log('no new tasks to evaluate')
        return

    log('new task_ids=%d' % len(new_ids))
    log('new task_ids=%d' % len(new_ids))
    acted = 0
    for tid in new_ids:
        try:
            rows = sql_exec("SELECT * FROM tasks WHERE id=?", (tid,))
        except Exception as e:
            log('db error task=%s err=%s' % (tid, e))
            continue
        if not rows:
            log('missing task=%s' % tid)
            continue
        task = rows[0]

        # Skip needs_input: do not re-queue tasks blocked on missing input
        if task.get('status') == 'blocked' or task.get('block_kind') == 'needs_input':
            log('needs_input skip task=%s' % tid)
            seen.add(tid)
            continue

        # Retry storm guard: if claimed X times in Y minutes, cooldown Z minutes
        try:
            claim_history = sql_exec(
                """SELECT COUNT(*) AS cnt FROM task_events
                   WHERE task_id=? AND kind='claimed' AND created_at >= ?""",
                (tid, NOW() - RETRY_WINDOW),
            )
            claim_count = claim_history[0]['cnt'] if claim_history else 0
        except Exception:
            claim_count = 0
        if claim_count >= MAX_RETRIES_IN_WINDOW:
            log('cooldown_retry task=%s claims=%d in %dm window' % (tid, claim_count, RETRY_WINDOW // 60))
            if can_act(state, tid):
                mark_acted(state, tid)
                sql_exec(
                    "UPDATE tasks SET status='todo', claim_lock=NULL, claim_expires=NULL WHERE id=?",
                    (tid,),
                    readonly=False,
                )
            seen.add(tid)
            continue

        # eligibility gate
        gate = gating(task)
        if not gate['pass']:
            gatekeeper_comment(tid, gate['reasons'])
            seen.add(tid)
            continue

        # retry budget gate: count recent run attempts
        recent_runs = sql_exec("SELECT id, status, started_at, ended_at FROM task_runs WHERE task_id=? ORDER BY started_at DESC LIMIT 20", (tid,))
        retries_used = sum(1 for r in recent_runs if r['status'] in ('done', 'crashed', 'killed', 'blocked', 'gave_up'))
        max_retries = 3
        if retries_used >= max_retries:
            gatekeeper_comment(tid, ['retry:%d>=%d' % (retries_used, max_retries)])
            seen.add(tid)
            continue

        kind = next((a['kind'] for a in anomalies if a.get('task_id') == tid), 'unknown')
        action = 'log_only'
        meta = {'kind': kind, 'status': task.get('status'), 'retries_used': retries_used, 'max_retries': max_retries}
        assignee = task.get('assignee') or 'ops'
        prompt_stub = (task.get('title') or '').strip()
        # enqueue by actual assignee profile so dispatcher can route correctly later
        # embed model config based on current fallback state
        fb_state = _fallback_state()
        mo_payload = model_override_payload(fb_state.get('cycle_count', 0))
        try:
            claim = http_json('POST', '/api/task/%s/enqueue' % tid, {'profile': assignee, 'prompt': prompt_stub, 'model_override': mo_payload})
        except Exception as e:
            claim = {'ok': False, 'message': str(e)}
        if claim and claim.get('ok'):
            action = 'enqueued'
            meta['run_id'] = claim.get('run_id')
            meta['assigned_to'] = claim.get('assignee') or assignee
            meta['prompt'] = claim.get('prompt') or prompt_stub
            meta['next'] = 'dispatcher picks up'
            write_plan(tid, action, meta)
            mark_acted(state, tid)
            seen.add(tid)
            acted += 1
            log('plan task=%s action=%s assignee=%s' % (tid, action, meta['assigned_to']))
            continue

        action = 'enqueue_failed'
        meta['assignee'] = assignee
        meta['claim_message'] = (claim or {}).get('message', '')
        write_plan(tid, action, meta)
        mark_acted(state, tid)
        seen.add(tid)
        acted += 1
        log('plan task=%s action=%s reason=%s' % (tid, action, meta['claim_message']))

    if acted and can_act(state, 'dispatch_trig'):
        try:
            import runpy
            script = ROOT / 'scripts' / 'trigger_dispatch.py'
            if script.exists():
                runpy.run_path(str(script), run_name='__dispatch__')
                mark_acted(state, 'dispatch_trig')
                log('trigger_dispatch ran')
        except Exception as e:
            log('trigger_dispatch error: %s' % e)
    try:
        ensure_audit_retry_column()
        if dispatcher_watchdog():
            pass
    except Exception as e:
        log('watchdog error: %s' % e)
    try:
        requeue = audit_requeue_failed_outputs()
        if requeue:
            log('audit_requeue: %s' % len(requeue))
    except Exception as e:
        log('audit_requeue error: %s' % e)
    log('orchestrator act=%d' % acted)
    # —— Fallback state check for insight report ——
    fb_state = _fallback_state()
    if fb_state.get('cycle_count', 0) > 0:
        log('fallback_cycle_count=%d last_reason=%s' % (fb_state['cycle_count'], fb_state.get('last_reason', '?')))
    try:
        insight = write_insight_report(fb_state)
    except Exception as e:
        log('insight error: %s' % e)
    else:
        if insight and isinstance(insight, dict) and insight.get('path'):
            log('insight_report: %s projects=%s items=%s' % (
                insight.get('path'),
                len(insight.get('projects', [])),
                insight.get('items', 0),
            ))
        elif isinstance(insight, dict) and insight.get('error'):
            log('insight_error: %s' % insight.get('error'))
    state['seen_task_ids'] = list(seen)[-500:]
    save_state(state)
    log('orchestrator done acted=%d' % acted)


if __name__ == '__main__':
    main()
