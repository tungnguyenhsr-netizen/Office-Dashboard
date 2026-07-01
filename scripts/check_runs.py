import sqlite3, os
db = os.path.expandvars(r'%LOCALAPPDATA%\hermes\kanban.db')
conn = sqlite3.connect(db)
c = conn.cursor()
runs = c.execute("SELECT status, COUNT(1) FROM task_runs GROUP BY status").fetchall()
print("task_runs by status:", runs)

orphan = c.execute("""
    SELECT r.id, r.task_id, t.status, r.worker_pid
    FROM task_runs r
    JOIN tasks t ON t.id = r.task_id
    WHERE r.status='running' AND t.status != 'running'
""").fetchall()
print("Orphan running runs (task not running):", len(orphan))
for o in orphan:
    print(f"  run_id={o[0]} task={o[1]} task_status={o[2]} worker_pid={o[3]}")
conn.close()
