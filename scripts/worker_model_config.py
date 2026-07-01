# -*- coding: utf-8 -*-
"""
Worker model config with primary/fallback logic, tracking, and insight flagging.

Primary:   gpt-5.4-mini
Fallback:  deepseek-v4-flash
Triggers:  HTTP 500, 429, or timeout > 30s
Flag:      after 3 consecutive fallbacks in one cycle -> insight report
"""
import json, os, re
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(r'C:\Users\YOURNAME\Documents\YourVault\Efforts\Office-Dashboard')
LOG_DIR = ROOT / 'logs'
_FALLBACK_STATE_PATH = ROOT / 'state' / 'fallback-state.json'
TZ = timezone(timedelta(hours=7))

# ======== CONFIG ========
PRIMARY_MODEL = 'gpt-5.4-mini'
FALLBACK_MODEL = 'deepseek-v4-flash'
FALLBACK_TRIGGERS = {
    'http_statuses': [500, 429],
    'timeout_seconds': 30,
}
MAX_CONSECUTIVE_FALLBACKS = 3

# ======== STATE ========

def _fallback_state():
    try:
        if _FALLBACK_STATE_PATH.exists():
            return json.loads(_FALLBACK_STATE_PATH.read_text(encoding='utf-8'))
    except Exception:
        pass
    return {'cycle_count': 0, 'consecutive_since': None, 'last_reason': None}

def _save_fallback_state(state):
    try:
        _FALLBACK_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _FALLBACK_STATE_PATH.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
    except Exception:
        pass

# ======== PUBLIC API ========

def get_primary():
    return PRIMARY_MODEL

def get_fallback():
    return FALLBACK_MODEL

def should_fallback(error_info: dict) -> bool:
    """Check if error warrants a fallback."""
    status = error_info.get('status_code')
    if status in FALLBACK_TRIGGERS['http_statuses']:
        return True
    timeout = error_info.get('timeout')
    if timeout and timeout > FALLBACK_TRIGGERS['timeout_seconds']:
        return True
    return False

def log_fallback(reason: str) -> dict:
    """Log model_fallback event, update state, return tracking info."""
    state = _fallback_state()
    state['cycle_count'] = state.get('cycle_count', 0) + 1
    state['consecutive_since'] = state.get('consecutive_since') or datetime.now(TZ).isoformat()
    state['last_reason'] = reason
    _save_fallback_state(state)

    # Log to orchestrator log file
    line = '[%s] model_fallback: %s' % (datetime.now(TZ).strftime('%H:%M:%S'), reason)
    try:
        log_path = LOG_DIR / ('orchestrator-%s.log' % datetime.now(TZ).strftime('%Y-%m-%d'))
        with open(log_path, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except Exception:
        pass

    return {'consecutive': state['cycle_count'], 'threshold': MAX_CONSECUTIVE_FALLBACKS}

def flag_for_insight() -> bool:
    """True if 3+ consecutive fallbacks this cycle."""
    state = _fallback_state()
    return state.get('cycle_count', 0) >= MAX_CONSECUTIVE_FALLBACKS

def reset_fallback_cycle():
    """Call after a primary success resets the cycle."""
    state = _fallback_state()
    state['cycle_count'] = 0
    state['consecutive_since'] = None
    state['last_reason'] = None
    _save_fallback_state(state)

def model_override_payload(consecutive_fallback_count: int = 0) -> str:
    """Return model name to embed in a task at enqueue time (flat string for -m flag)."""
    if consecutive_fallback_count >= MAX_CONSECUTIVE_FALLBACKS:
        return FALLBACK_MODEL
    return PRIMARY_MODEL

def fallback_summary() -> str:
    """Return human-readable fallback state for insight reports."""
    state = _fallback_state()
    c = state.get('cycle_count', 0)
    if c >= MAX_CONSECUTIVE_FALLBACKS:
        return '⚠️ **Model fallback active** — %d consecutive fallbacks (threshold: %d)\n- Since: %s\n- Last reason: %s' % (
            c, MAX_CONSECUTIVE_FALLBACKS, state.get('consecutive_since', '?'), state.get('last_reason', '?'))
    if c > 0:
        return '- Model fallbacks this cycle: %d/%d\n- Last reason: %s' % (
            c, MAX_CONSECUTIVE_FALLBACKS, state.get('last_reason', '?'))
    return ''

# ======== CRASH REASON PARSING ========

_CRASH_MODEL_RE = re.compile(r'(500|429|timeout)', re.IGNORECASE)

def parse_crash_reason(text: str) -> dict:
    """Extract status_code/timeout from a crash reason string."""
    result = {}
    if not text:
        return result
    text_lower = text.lower()
    if '500' in text:
        result['status_code'] = 500
    elif '429' in text:
        result['status_code'] = 429
    if 'timeout' in text_lower:
        m = re.search(r'(\d+)', text)
        result['timeout'] = int(m.group(1)) if m else 999
    return result
