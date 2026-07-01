import sqlite3, os
db = os.path.expandvars(r'%LOCALAPPDATA%\hermes\kanban.db')
conn = sqlite3.connect(db)
c = conn.cursor()

print('=== TASK STATUS COUNTS ===')
for r in c.execute('SELECT status, COUNT(1) FROM tasks GROUP BY status ORDER BY status'):
    print(f"  {r[0]}: {r[1]}")

print()
print(f"Total: {c.execute('SELECT COUNT(1) FROM tasks').fetchone()[0]}")

print()
print('=== RUNNING tasks with NULL worker_pid ===')
zombies = c.execute("SELECT id, title, status, worker_pid FROM tasks WHERE status='running' AND worker_pid IS NULL").fetchall()
for z in zombies:
    print(f"  {z[0]}: {z[1]} (pid={z[2]})")

print()
import sys
sys.stdout.reconfigure(encoding='utf-8')

print('=== BLOCKED tasks ===')
blocked = c.execute("SELECT id, title, status, assignee FROM tasks WHERE status='blocked'").fetchall()
print(f"  Count: {len(blocked)}")
for b in blocked:
    print(f"  {b[0]}: (assignee={b[3]})")

print()
print('=== RUNNING tasks without active runs ===')
for r in c.execute("""
    SELECT t.id, t.title, r.worker_pid
    FROM tasks t
    LEFT JOIN task_runs r ON r.task_id = t.id AND r.status='running'
    WHERE t.status='running' AND r.id IS NULL
"""):
    print(f"  {r[0]}: {r[1]}: worker_pid={r[2]}")

print()
print('=== RUNNING task detail ===')
r = c.execute("SELECT id, title, status, assignee, worker_pid FROM tasks WHERE status='running'").fetchone()
if r:
    print(f"  {r[0]}: {r[1]} (assignee={r[3]}, worker_pid={r[4]})")

# Check workspace folder existence
import glob
ws_dir = os.path.expandvars(r'%LOCALAPPDATA%\hermes\kanban\workspaces')
print(f"\n=== Workspace folders in {ws_dir} ===")
folders = [f for f in os.listdir(ws_dir) if os.path.isdir(os.path.join(ws_dir, f))]
print(f"  Total folders: {len(folders)}")
# Check which tasks exist in DB vs workspace
db_tasks = set(r[0] for r in c.execute("SELECT id FROM tasks").fetchall())
orphan_folders = [f for f in folders if f not in db_tasks]
print(f"  Orphan folders (no DB task): {len(orphan_folders)}")
if orphan_folders:
    for f in orphan_folders[:10]:
        print(f"    {f}")

conn.close()
