"""Historical TVL accounting. All USD values are Decimal strings; null means unknown."""
from __future__ import annotations

import csv
import json
from collections import defaultdict
from decimal import Decimal, localcontext
from pathlib import Path

from .storage import to_json

SCHEMA = """
CREATE TABLE IF NOT EXISTS tvl_discovery_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, started_at INTEGER NOT NULL, completed_at INTEGER,
    status TEXT NOT NULL, params_json TEXT NOT NULL, summary_json TEXT
);
CREATE TABLE IF NOT EXISTS tvl_catalog_evidence (
    chain_id INTEGER NOT NULL, address TEXT NOT NULL, source TEXT NOT NULL,
    metadata_status TEXT NOT NULL, evidence_json TEXT NOT NULL, updated_at INTEGER NOT NULL,
    PRIMARY KEY(chain_id,address,source)
);
CREATE TABLE IF NOT EXISTS tvl_strategies (
    chain_id INTEGER NOT NULL, parent TEXT NOT NULL, strategy TEXT NOT NULL,
    source TEXT NOT NULL, PRIMARY KEY(chain_id,parent,strategy)
);
CREATE TABLE IF NOT EXISTS tvl_targets (
    chain_id INTEGER NOT NULL, strategy TEXT NOT NULL, child TEXT NOT NULL,
    source TEXT NOT NULL, PRIMARY KEY(chain_id,strategy,child)
);
CREATE TABLE IF NOT EXISTS tvl_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, started_at INTEGER NOT NULL,
    completed_at INTEGER, status TEXT NOT NULL, params_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tvl_snapshots (
    run_id INTEGER NOT NULL REFERENCES tvl_runs(id), chain_id INTEGER NOT NULL,
    vault TEXT NOT NULL, timestamp INTEGER NOT NULL, block_number INTEGER,
    data_json TEXT NOT NULL, PRIMARY KEY(run_id,chain_id,vault,timestamp)
);
CREATE TABLE IF NOT EXISTS tvl_positions (
    run_id INTEGER NOT NULL REFERENCES tvl_runs(id), chain_id INTEGER NOT NULL,
    parent TEXT NOT NULL, strategy TEXT NOT NULL, child TEXT NOT NULL,
    timestamp INTEGER NOT NULL, data_json TEXT NOT NULL,
    PRIMARY KEY(run_id,chain_id,parent,strategy,child,timestamp)
);
"""


def decimal(value):
    if value is None:
        return None
    result = Decimal(str(value))
    if not result.is_finite() or result < 0:
        raise ValueError(f"invalid nonnegative quantity: {value}")
    return result


def asset_units(raw, decimals):
    if decimals is None:
        raise ValueError("missing asset decimals")
    with localcontext() as ctx:
        ctx.prec = 78
        return Decimal(int(raw)) / Decimal(10) ** int(decimals)


def account(snapshots, positions, *, include_curation=False):
    """Deduct each actual holding once, capped by parent debt and child TVL.

    A shared holder's balance is split among its parents by recorded debt. Several
    child positions share that same debt budget. This prevents duplicate deductions.
    Curation remains a separate product category unless explicitly combined.
    """
    with localcontext() as ctx:
        ctx.prec = 78
        return _account(snapshots, positions, include_curation=include_curation)


def _account(snapshots, positions, *, include_curation):
    by_key = {(s['chain_id'], s['vault'].lower()): s for s in snapshots}
    adjustments = defaultdict(lambda: Decimal(0))
    edges = [dict(p, overlap_usd=None) for p in positions]
    groups = defaultdict(list)
    issues = []
    chains = {s['chain_id'] for s in snapshots}
    for s in snapshots:
        if s.get('bridge_target_chain') in chains and decimal(s.get('tvl_usd')):
            issues.append('bridge_migration_timing_unverified')
    for edge in edges:
        if edge.get('method') == 'strategy-allocation':
            if (edge.get('reason') == 'unresolved_adapter_mapping'
                    or (edge.get('mapping_status') == 'unresolved' and edge.get('status') != 'ok')):
                issues.append(edge.get('reason') or 'unresolved_adapter_mapping')
            else:
                edge['overlap_usd'] = '0'
            continue
        parent = by_key.get((edge['chain_id'], edge['parent'].lower()))
        child = by_key.get((edge['chain_id'], edge['child'].lower()))
        if parent is None or child is None:
            edge['reason'] = 'endpoint_outside_selection'
            continue
        if edge['parent'].lower() == edge['child'].lower():
            edge['reason'] = 'self_edge'
            continue
        if not include_curation and child.get('category',child['version']) == 'curation':
            edge['reason'] = 'curation_separate_product'
            continue
        # A known zero needs no child valuation. Missing prices must not mask it.
        if edge.get('status') == 'ok' and decimal(edge.get('debt_usd')) == 0:
            edge['overlap_usd'] = '0'
            continue
        if (edge.get('status') != 'ok' or parent.get('tvl_usd') is None
                or child.get('tvl_usd') is None or edge.get('owned_usd') is None
                or edge.get('debt_usd') is None):
            edge['reason'] = edge.get('reason') or 'missing_position_valuation'
            issues.append(edge['reason'])
            continue
        kind = 'recorded-debt' if edge.get('method') == 'direct-strategy' else 'held-shares'
        groups[(edge['chain_id'], edge['strategy'].lower(), kind)].append(edge)

    candidates = []
    for group in groups.values():
        # One balance belongs to a holder, regardless of how many parents use it.
        debts = {}
        owned = {}
        for e in group:
            debts[e['parent'].lower()] = decimal(e['debt_usd'])
            owned[e['child'].lower()] = decimal(e['owned_usd'])
        total_debt = sum(debts.values(), Decimal(0))
        total_owned = sum(owned.values(), Decimal(0))
        budget = min(total_debt, total_owned)
        for e in group:
            value = (budget * debts[e['parent'].lower()] / total_debt
                     * owned[e['child'].lower()] / total_owned) if total_debt and total_owned else Decimal(0)
            candidates.append((e, value))

    # Cap all incoming ownership at the child's TVL, and all outgoing at parent TVL.
    incoming = defaultdict(lambda: Decimal(0))
    outgoing = defaultdict(lambda: Decimal(0))
    for e, value in candidates:
        incoming[(e['chain_id'], e['child'].lower())] += value
        outgoing[(e['chain_id'], e['parent'].lower())] += value
    for e, value in candidates:
        pk = (e['chain_id'], e['parent'].lower())
        ck = (e['chain_id'], e['child'].lower())
        scale = min(Decimal(1), decimal(by_key[ck]['tvl_usd']) / incoming[ck] if incoming[ck] else Decimal(1),
                    decimal(by_key[pk]['tvl_usd']) / outgoing[pk] if outgoing[pk] else Decimal(1))
        value *= scale
        e['overlap_usd'] = str(value)
        adjustments[ck] += value

    rows = []
    for key, s in by_key.items():
        row = dict(s)
        value = decimal(s.get('tvl_usd'))
        row['overlap_usd'] = str(adjustments[key]) if value is not None else None
        row['external_tvl_usd'] = str(max(Decimal(0), value - adjustments[key])) if value is not None else None
        rows.append(row)
    valued = [r for r in rows if r['tvl_usd'] is not None]
    complete = len(valued) == len(rows) and not issues
    gross = sum((decimal(r['tvl_usd']) for r in valued), Decimal(0))
    overlap = sum(adjustments.values(), Decimal(0))
    summary = {'timestamp': snapshots[0]['timestamp'] if snapshots else None,
               'gross_tvl_usd': str(gross) if complete else None,
               'overlap_usd': str(overlap) if complete else None,
               'external_tvl_usd': str(max(Decimal(0), gross-overlap)) if complete else None,
               'known_gross_tvl_usd': str(gross), 'known_overlap_usd': str(overlap),
               'known_external_tvl_usd': str(max(Decimal(0), gross-overlap)),
               'vault_count': len(rows), 'valued_vault_count': len(valued),
               'position_count': len(edges), 'unresolved_positions': len(issues),
               'status': 'complete' if complete else 'incomplete',
               'mapping_scope': 'known candidates', 'issues': sorted(set(issues))}
    return rows, edges, summary


def export_tvl(conn, out, run_id=None, *, include_curation=False):
    if run_id is None:
        row = conn.execute("SELECT id FROM tvl_runs WHERE status IN ('complete','incomplete') ORDER BY id DESC LIMIT 1").fetchone()
        if row is None:
            raise ValueError('no collected TVL run')
        run_id = row['id']
    run = conn.execute('SELECT * FROM tvl_runs WHERE id=?', (run_id,)).fetchone()
    if run is None or run['status'] not in ('complete', 'incomplete'):
        raise ValueError('TVL run is not finished')
    snapshots = defaultdict(list)
    positions = defaultdict(list)
    for row in conn.execute('SELECT data_json FROM tvl_snapshots WHERE run_id=? ORDER BY timestamp,chain_id,vault', (run_id,)):
        data = json.loads(row['data_json'])
        snapshots[data['timestamp']].append(data)
    for row in conn.execute('SELECT data_json FROM tvl_positions WHERE run_id=? ORDER BY timestamp,chain_id,parent,strategy,child', (run_id,)):
        data = json.loads(row['data_json'])
        positions[data['timestamp']].append(data)
    output = {'vaults': [], 'positions': [], 'history': []}
    for timestamp, points in snapshots.items():
        rows, edges, summary = account(points, positions[timestamp], include_curation=include_curation)
        output['vaults'].extend(rows)
        output['positions'].extend(edges)
        output['history'].append(summary)
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    for name, rows in output.items():
        with (out / f'{name}.csv').open('w', newline='') as f:
            fields = list(dict.fromkeys(k for row in rows for k in row))
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            writer.writerows({k: to_json(v) if isinstance(v, (dict, list)) else v for k, v in row.items()} for row in rows)
    metadata = dict(run)
    metadata['params'] = json.loads(metadata.pop('params_json'))
    metadata['include_curation'] = include_curation
    metadata['mapping_scope'] = 'known candidates; positive share scans expand coverage, absence is not proof of no nesting'
    (out / 'tvl.json').write_text(json.dumps({'run': metadata, **output}, indent=2) + '\n')
    return out
