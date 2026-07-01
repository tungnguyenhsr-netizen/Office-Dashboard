import os, pathlib

HERMES_ROOT = pathlib.Path(
    os.environ.get('HERMES_ROOT', str(pathlib.Path(__file__).resolve().parent.parent))
)
HERMES_DB_PATH = pathlib.Path(
    os.environ.get('HERMES_DB_PATH', os.path.expandvars(r'%LOCALAPPDATA%\hermes\kanban.db'))
)
HERMES_MONITOR_URL = os.environ.get('HERMES_MONITOR_URL', 'http://localhost:8093')
HERMES_VAULT_ROOT = HERMES_ROOT.parent
