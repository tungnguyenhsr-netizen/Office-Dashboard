# -*- coding: utf-8 -*-
"""Vault directory configuration — read/write vault-config.json."""

import json, os

VAULT_CONFIG_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), '..', '..', 'vault-config.json'
)


def read_vault_config():
    if os.path.exists(VAULT_CONFIG_FILE):
        try:
            with open(VAULT_CONFIG_FILE, 'r', encoding='utf-8') as f:
                return json.load(f).get('vault_dir', '')
        except Exception:
            pass
    return ''


def write_vault_config(vault_dir):
    with open(VAULT_CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump({'vault_dir': vault_dir}, f, indent=2, ensure_ascii=False)
