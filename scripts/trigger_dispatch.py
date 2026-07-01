# -*- coding: utf-8 -*-
"""Trigger a single dispatcher pass after enqueue, with profile-assignment alias."""
import os, subprocess, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import HERMES_ROOT as ROOT_PATH

ROOT = Path(ROOT_PATH)
_DEFAULT_BOARD = os.getenv('HERMES_KANBAN_BOARD', 'default')
_HERMES_CLI = os.getenv('HERMES_CLI', r'%LOCALAPPDATA%\hermes\hermes-agent\venv\Scripts\hermes.exe')

# map domain -> executable dispatcher script
_DISPATCH_SCRIPT = ROOT / 'scripts' / 'dispatcher.py'

def main():
    print('trigger_dispatch start board=%s' % _DEFAULT_BOARD)
    # Use the existing CLI dispatcher for core routing rather than reimplementing spawn,
    # because the Hermes CLI already encapsulates project/path resolution.
    cmd = [
        _HERMES_CLI,
        'kanban', '--board', _DEFAULT_BOARD,
        'assignees',
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
        print('assignees rc=%s' % res.returncode)
        if res.stdout:
            print(res.stdout[:400])
        if res.stderr:
            print('stderr:', res.stderr[:400])
    except Exception as e:
        print('assignees error: %s' % e)
        return
    # try 1 dispatch pass
    cmd = [
        _HERMES_CLI,
        'kanban', '--board', _DEFAULT_BOARD,
        'dispatch',
        '--max', '3',
        '--json',
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=180, stdin=subprocess.DEVNULL)
    print('dispatch rc=%s' % res.returncode)
    if res.stdout:
        print(res.stdout[:600])
    if res.stderr:
        print('stderr:', res.stderr[:600])
    print('trigger_dispatch end')

if __name__ == '__main__':
    main()
