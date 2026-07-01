# -*- coding: utf-8 -*-
"""System-level helpers — process kill, system health stats."""

import os, signal, subprocess, sys, time

try:
    import psutil
    _HAS_PSUTIL = True
except ImportError:
    _HAS_PSUTIL = False


def kill_by_pid(pid):
    """Cross-platform process kill. Returns (ok: bool, message: str)."""
    if not pid or pid == 0:
        return False, 'no PID'
    try:
        if sys.platform == 'win32':
            r = subprocess.run(
                ['taskkill', '/F', '/PID', str(pid)],
                capture_output=True, text=True, timeout=10,
            )
            if r.returncode == 0:
                return True, 'killed'
            return False, r.stderr.strip() or r.stdout.strip() or 'taskkill failed'
        else:
            os.kill(pid, signal.SIGTERM)
            return True, 'killed'
    except subprocess.TimeoutExpired:
        return False, 'timeout'
    except ProcessLookupError:
        return False, 'process already dead'
    except Exception as e:
        return False, str(e)


def get_system_health():
    """Return CPU, memory, disk, uptime, and Hermes process status."""
    if not _HAS_PSUTIL:
        return {
            'cpu': {'percent': 0, 'count': 0},
            'memory': {'total': 0, 'used': 0, 'percent': 0},
            'disk': {'total': 0, 'used': 0, 'percent': 0},
            'uptime': 0,
            'hermes': {'dispatcher_running': False, 'dispatcher_pid': None,
                       'orchestrator_running': False},
        }

    cpu = {
        'percent': psutil.cpu_percent(interval=0.5),
        'count': psutil.cpu_count(),
    }
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage('/')
    uptime_sec = int(time.time() - psutil.boot_time())

    hermes = {
        'dispatcher_running': False,
        'dispatcher_pid': None,
        'orchestrator_running': False,
    }
    try:
        for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
            try:
                cmd = ' '.join(proc.info.get('cmdline') or [])
                if 'dispatcher' in cmd.lower():
                    hermes['dispatcher_running'] = True
                    hermes['dispatcher_pid'] = proc.info['pid']
                if 'orchestrator' in cmd.lower():
                    hermes['orchestrator_running'] = True
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
    except Exception:
        pass

    return {
        'cpu': cpu,
        'memory': {'total': mem.total, 'used': mem.used, 'percent': mem.percent},
        'disk': {'total': disk.total, 'used': disk.used, 'percent': disk.percent},
        'uptime': uptime_sec,
        'hermes': hermes,
    }
