#!/usr/bin/env python3
"""Dispatcher loop: poll -> claim -> spawn workers via Hermes CLI."""

import subprocess, time, os, sys, signal, json
from datetime import datetime
from pathlib import Path

_log_dir = Path(os.path.expandvars(r'%LOCALAPPDATA%\hermes\logs'))
_log_dir.mkdir(parents=True, exist_ok=True)

CONFIG = {
    "board": os.getenv("HERMES_KANBAN_BOARD", "default"),
    "profile": os.getenv("HERMES_PROFILE", "content-factory"),
    "poll_interval": int(os.getenv("DISPATCH_POLL_INTERVAL", "5")),
    "max_spawn": int(os.getenv("DISPATCH_MAX_SPAWN", "1")),
    "hermes_cli": r"%LOCALAPPDATA%\hermes\hermes-agent\venv\Scripts\hermes.exe",
    "log_path": os.getenv("DISPATCH_LOG", str(_log_dir / "dispatcher.log")),
}

_running = True
_tick_count = 0
VALID_CORE_WORKSPACE_KINDS = {'scratch', 'worktree', 'dir'}


def log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    try:
        print(line, flush=True)
    except (OSError, ValueError):
        pass
    with open(CONFIG["log_path"], "a", encoding="utf-8") as f:
        f.write(line + "\n")


def normalize_workspace_kinds():
    """Hard guard: never let invalid workspace_kind reach core Hermes dispatch."""
    db_path = Path(os.path.expandvars(r'%LOCALAPPDATA%\hermes\kanban.db'))
    try:
        import sqlite3
        con = sqlite3.connect(db_path, timeout=10)
        con.row_factory = sqlite3.Row
        cur = con.cursor()
        rows = cur.execute(
            """SELECT id, title, status, workspace_kind
               FROM tasks
               WHERE status NOT IN ('done', 'archived')
                 AND COALESCE(workspace_kind, '') NOT IN ('scratch', 'worktree', 'dir')
               ORDER BY created_at DESC
               LIMIT 100"""
        ).fetchall()
        fixed = 0
        for r in rows:
            cur.execute(
                """UPDATE tasks
                   SET workspace_kind='scratch',
                       consecutive_failures=0,
                       last_failure_error=NULL
                   WHERE id=?""",
                (r['id'],)
            )
            try:
                cur.execute(
                    "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) VALUES (?, NULL, ?, ?, strftime('%s','now'))",
                    (r['id'], 'auto_fix', json.dumps({
                        'action': 'workspace_kind_fix',
                        'from': r['workspace_kind'],
                        'to': 'scratch',
                        'source': 'dispatcher',
                    }))
                )
            except Exception:
                pass
            fixed += 1
            log(f"workspace_fix task={r['id']} {r['workspace_kind']}->scratch status={r['status']}")
        if fixed:
            con.commit()
            log(f"workspace_fix total={fixed}")
        con.close()
        return fixed
    except Exception as e:
        log(f"workspace_fix error: {e}")
        return 0

def dispatch_tick() -> dict:
    cmd = [
        CONFIG["hermes_cli"],
        "kanban", "--board", CONFIG["board"],
        "dispatch",
        "--max", str(CONFIG["max_spawn"]),
        "--json",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
    if result.returncode != 0:
        log(f"dispatch error (rc={result.returncode}): {result.stderr.strip()}")
        return {}
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        log(f"dispatch parse error: {result.stdout[:200]}")
        return {}

def handle_signal(signum, frame):
    global _running
    log(f"Signal {signum} received, shutting down...")
    _running = False

signal.signal(signal.SIGTERM, handle_signal)
signal.signal(signal.SIGINT, handle_signal)

def main():
    global _tick_count
    global _running
    log(f"Dispatcher started - board={CONFIG['board']}, poll={CONFIG['poll_interval']}s")
    log(f"CLI: {CONFIG['hermes_cli']}")
    while _running:
        try:
            normalize_workspace_kinds()
            res = dispatch_tick()
            if res.get("spawned"):
                for item in res["spawned"]:
                    tid = item.get("task_id", "?")
                    who = item.get("assignee", "?")
                    ws = item.get("workspace", "")
                    log(f"Spawned {tid} -> {who} @ {ws or '-'}")
            n_reclaimed = res.get("reclaimed", 0)
            n_crashed = len(res.get("crashed", []))
            n_timeout = len(res.get("timed_out", []))
            n_skipped = len(res.get("skipped_nonspawnable", []))
            if n_reclaimed or n_crashed or n_timeout:
                log(f"Cleanup: reclaimed={n_reclaimed} crashed={n_crashed} timeout={n_timeout}")
            if n_skipped:
                log(f"Skipped {n_skipped} nonspawnable tasks")
        except KeyboardInterrupt:
            log("Interrupted, stopping")
            break
        except Exception as e:
            log(f"Loop error: {e}")
        _tick_count += 1
        if _tick_count % 12 == 0:
            log(f"Running - tick #{_tick_count}")
        time.sleep(CONFIG["poll_interval"])
    log("Dispatcher stopped")

if __name__ == "__main__":
    main()
