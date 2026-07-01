import os, sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, r'C:\Users\YOURNAME\Documents\YourVault\Efforts\Office-Dashboard\scripts')
from orchestrator import audit_completed_outputs, audit_requeue_failed_outputs

issues = audit_completed_outputs()
print('audit_completed_outputs', len(issues))
for item in issues[:20]:
    print(item)
PY && python 'C:\Users\YOURNAME\Documents\YourVault\Efforts\Office-Dashboard\scripts\audit_smoke.py'
