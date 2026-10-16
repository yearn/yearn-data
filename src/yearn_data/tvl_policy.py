"""Explicit reporting scope; raw observations remain available for audit."""
import json
from pathlib import Path


VAULT_POLICIES = json.loads((Path(__file__).parent / 'inventories/tvl-vault-policy.json').read_text())


def classify_vault(row, policies=VAULT_POLICIES):
    row = dict(row)
    address = row.get('vault', row.get('address', '')).lower()
    for policy in policies:
        if (row['chain_id'], address) == (policy['chainId'], policy['address'].lower()):
            row['version'] = policy['version']
            row['category' if 'vault' in row else 'tvl_category'] = policy['category']
    return row


def correct_catalog(conn, chain_ids=None):
    for policy in VAULT_POLICIES:
        if chain_ids is None or policy['chainId'] in chain_ids:
            conn.execute('''UPDATE tvl_vaults SET version=?,tvl_category=?,protocol=NULL
                WHERE chain_id=? AND lower(address)=?''',
                (policy['version'],policy['category'],policy['chainId'],policy['address'].lower()))
