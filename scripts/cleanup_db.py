import sqlite3, os, sys, json
from datetime import datetime

db = os.path.expandvars(r'%LOCALAPPDATA%\hermes\kanban.db')
conn = sqlite3.connect(db)
c = conn.cursor()

ts = int(datetime.now().timestamp())

# 1. Archive blocked tasks to 'done'
blocked = c.execute("SELECT id, assignee FROM tasks WHERE status='blocked'").fetchall()
print(f"Blocked tasks to archive: {len(blocked)}")
for tid, assignee in blocked:
    c.execute("UPDATE tasks SET status='done', completed_at=? WHERE id=?", (ts, tid))
    c.execute("INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, 'archived', ?, ?)",
              (tid, json.dumps({"from": "blocked", "to": "done", "reason": "bulk cleanup"}), ts))
    print(f"  {tid} ({assignee}): blocked -> done")

# Verify
total = c.execute("SELECT COUNT(1) FROM tasks").fetchone()[0]
done = c.execute("SELECT COUNT(1) FROM tasks WHERE status='done'").fetchone()[0]
blocked_remaining = c.execute("SELECT COUNT(1) FROM tasks WHERE status='blocked'").fetchone()[0]
running = c.execute("SELECT COUNT(1) FROM tasks WHERE status='running'").fetchone()[0]

conn.commit()
conn.close()

print(f"\nAfter cleanup:")
print(f"  Total: {total}")
print(f"  Running: {running}")
print(f"  Done: {done}")
print(f"  Blocked remaining: {blocked_remaining}")
