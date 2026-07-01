import sqlite3, os, json
from datetime import datetime
db = os.path.expandvars(r'%LOCALAPPDATA%\hermes\kanban.db')
conn = sqlite3.connect(db)
c = conn.cursor()
ts = int(datetime.now().timestamp())

orphan = c.execute("""
    SELECT r.id, r.task_id, t.status, r.worker_pid
    FROM task_runs r
    JOIN tasks t ON t.id = r.task_id
    WHERE r.status='running' AND t.status != 'running'
""").fetchall()

print(f"Fixing {len(orphan)} orphan runs...")
for run_id, task_id, task_status, pid in orphan:
    c.execute("UPDATE task_runs SET status='killed', error='orphan cleanup - task already done' WHERE id=?", (run_id,))
    c.execute("INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, 'killed', ?, ?)",
              (task_id, json.dumps({"reason": "orphan run cleanup", "run_id": run_id, "worker_pid": pid}), ts))
    print(f"  run_id={run_id} task={task_id} pid={pid} -> killed")

conn.commit()
conn.close()
print("Done")
