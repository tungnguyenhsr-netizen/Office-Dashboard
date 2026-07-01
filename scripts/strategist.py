# -*- coding: utf-8 -*-
"""Strategist Agent — Phase 1: tag-based model routing for kanban tasks.

Runs inline inside orchestrator cycle. Does NOT modify orchestrator/dispatcher logic.
Tags pending tasks with model_override based on brand/guideline keywords.
"""
import json, re
from pathlib import Path
from datetime import datetime, timezone, timedelta

ROOT = Path(r'C:\Users\YOURNAME\Documents\YourVault\Efforts\Office-Dashboard')
DB = Path(r'%LOCALAPPDATA%\hermes\kanban.db')
LOG_DIR = ROOT / 'logs'
TZ = timezone(timedelta(hours=7))

# Brand/guideline keywords → need deepseek for hallucination resistance
_BRAND_PATTERNS = re.compile(
    r'brand|guideline|thương hiệu|hướng dẫn|tone of voice|'
    r'visual identity|brandbook|partner brief|handoff|'
    r'kol brief|ugc guideline|acceptance criteria|persona',
    re.IGNORECASE
)

_FREE_MODEL = 'gpt-5.4-mini'
_DEEPSEEK_MODEL = 'deepseek-v4-flash'


def log(msg: str):
    line = '[%s] %s' % (datetime.now(TZ).strftime('%H:%M:%S'), msg)
    print(line, flush=True)
    log_path = LOG_DIR / ('strategist-%s.log' % datetime.now(TZ).strftime('%Y-%m-%d'))
    try:
        with open(log_path, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except Exception:
        pass


def db_conn():
    import sqlite3
    con = sqlite3.connect(str(DB), timeout=5)
    con.row_factory = sqlite3.Row
    return con


def needs_deepseek(task: dict) -> tuple[bool, str]:
    """Return (needs_deepseek: bool, reason: str)."""
    text = ' '.join(filter(None, [
        task.get('title', ''),
        task.get('body', ''),
        task.get('assignee', ''),
        task.get('workspace_path', ''),
    ]))
    if _BRAND_PATTERNS.search(text):
        return True, 'brand/guideline keywords'
    current_mo = task.get('model_override') or ''
    if current_mo and current_mo not in (_FREE_MODEL, _DEEPSEEK_MODEL, ''):
        return False, 'user-set override — skip'
    return False, 'general task'


def main():
    """One strategist pass: tag pending tasks with model_override."""
    log('strategist start')

    # ── Fix 2: Schema validation ──
    con_check = db_conn()
    cur_check = con_check.cursor()
    try:
        schema = cur_check.execute("PRAGMA table_info(task_events)").fetchall()
        col_names = [row[1] for row in schema]
        required = ['task_id', 'kind', 'payload', 'created_at']
        missing = [c for c in required if c not in col_names]
        if missing:
            log('ERROR task_events missing columns: %s' % missing)
            return
        log('schema OK: task_events has all required columns')
    except Exception as e:
        log('ERROR schema check failed: %s' % e)
        return
    finally:
        con_check.close()

    # ── Read pending tasks ──
    con = db_conn()
    cur = con.cursor()
    rows = cur.execute("""
        SELECT id, title, body, assignee, workspace_path, model_override
        FROM tasks
        WHERE status IN ('ready', 'todo')
        ORDER BY priority DESC, created_at ASC
    """).fetchall()

    tagged = 0
    for r in rows:
        task = dict(r)
        task_id = task['id']
        current_mo = task.get('model_override') or ''

        needs, _ = needs_deepseek(task)
        target_model = _DEEPSEEK_MODEL if needs else _FREE_MODEL

        # ── Fix 1: Idempotency — skip if already set correctly ──
        if current_mo == target_model:
            log('skip task=%s already model=%s' % (task_id, target_model))
            continue
        # Skip if user explicitly set a custom override (not one of ours)
        if current_mo and current_mo not in (_FREE_MODEL, _DEEPSEEK_MODEL, ''):
            log('skip task=%s user-override=%s' % (task_id, current_mo))
            continue

        cur.execute(
            "UPDATE tasks SET model_override=? WHERE id=?",
            (target_model, task_id)
        )
        reason_tag = 'requires_deepseek' if needs else 'free_tier_ok'
        cur.execute(
            "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) VALUES (?,?,?,?,?)",
            (task_id, None, 'strategist', json.dumps({
                'model': target_model,
                'reason': reason_tag,
            }), int(datetime.now(TZ).timestamp()))
        )
        tagged += 1
        log('tag task=%s model=%s reason=%s' % (task_id, target_model, reason_tag))

    con.commit()
    con.close()
    log('strategist done tagged=%d' % tagged)


if __name__ == '__main__':
    main()
