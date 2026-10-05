"""SQLite storage layer."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable


DEFAULT_DB_PATH = Path("data/yearn.sqlite")


SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS chains (
    chain_id INTEGER PRIMARY KEY,
    chain_key TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    rpc_env TEXT NOT NULL,
    defillama_slug TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS contracts (
    chain_id INTEGER NOT NULL,
    address TEXT NOT NULL,
    kind TEXT NOT NULL,
    abi_json TEXT,
    source TEXT,
    updated_at INTEGER,
    PRIMARY KEY (chain_id, address, kind)
);

CREATE TABLE IF NOT EXISTS vaults (
    chain_id INTEGER NOT NULL,
    version TEXT NOT NULL,
    address TEXT NOT NULL,
    source_address TEXT,
    asset TEXT,
    asset_symbol TEXT,
    asset_decimals INTEGER,
    name TEXT,
    api_version TEXT,
    management TEXT NOT NULL DEFAULT 'yearn',
    protocol TEXT,
    deployment_block INTEGER,
    active INTEGER NOT NULL DEFAULT 1,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (chain_id, address)
);

CREATE TABLE IF NOT EXISTS vault_inventory_events (
    chain_id INTEGER NOT NULL,
    version TEXT NOT NULL,
    vault_address TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    action TEXT NOT NULL DEFAULT 'added',
    source_address TEXT NOT NULL,
    asset TEXT,
    tx_hash TEXT NOT NULL,
    log_index INTEGER NOT NULL,
    block_number INTEGER NOT NULL,
    block_timestamp INTEGER NOT NULL,
    decoded_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (chain_id, tx_hash, log_index, source_kind)
);

CREATE INDEX IF NOT EXISTS vault_inventory_events_vault_idx
ON vault_inventory_events (chain_id, version, vault_address, source_kind, block_number);

CREATE TABLE IF NOT EXISTS events_raw (
    chain_id INTEGER NOT NULL,
    contract_address TEXT NOT NULL,
    event_name TEXT NOT NULL,
    tx_hash TEXT NOT NULL,
    log_index INTEGER NOT NULL,
    block_number INTEGER NOT NULL,
    block_timestamp INTEGER NOT NULL,
    decoded_json TEXT NOT NULL,
    PRIMARY KEY (chain_id, tx_hash, log_index)
);

CREATE INDEX IF NOT EXISTS events_raw_contract_idx
ON events_raw (chain_id, contract_address, event_name, block_number);

CREATE TABLE IF NOT EXISTS strategy_reports (
    chain_id INTEGER NOT NULL,
    version TEXT NOT NULL,
    vault_address TEXT NOT NULL,
    strategy_address TEXT NOT NULL,
    tx_hash TEXT NOT NULL,
    log_index INTEGER NOT NULL,
    block_number INTEGER NOT NULL,
    block_timestamp INTEGER NOT NULL,
    asset TEXT,
    asset_decimals INTEGER,
    gain_raw TEXT NOT NULL,
    loss_raw TEXT NOT NULL,
    net_raw TEXT NOT NULL,
    current_debt_raw TEXT,
    protocol_fees_raw TEXT,
    total_fees_raw TEXT,
    total_refunds_raw TEXT,
    extra_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (chain_id, tx_hash, log_index)
);

CREATE INDEX IF NOT EXISTS strategy_reports_asset_idx
ON strategy_reports (chain_id, asset, block_timestamp);

CREATE TABLE IF NOT EXISTS vault_flows (
    chain_id INTEGER NOT NULL,
    version TEXT NOT NULL,
    vault_address TEXT NOT NULL,
    direction TEXT NOT NULL,
    sender TEXT,
    owner TEXT,
    receiver TEXT,
    tx_hash TEXT NOT NULL,
    log_index INTEGER NOT NULL,
    block_number INTEGER NOT NULL,
    block_timestamp INTEGER NOT NULL,
    asset TEXT,
    asset_decimals INTEGER,
    assets_raw TEXT NOT NULL,
    shares_raw TEXT NOT NULL,
    decoded_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (chain_id, tx_hash, log_index)
);

CREATE INDEX IF NOT EXISTS vault_flows_asset_idx
ON vault_flows (chain_id, asset, block_timestamp);

CREATE INDEX IF NOT EXISTS vault_flows_vault_idx
ON vault_flows (chain_id, vault_address, block_number);

CREATE TABLE IF NOT EXISTS vault_share_prices (
    chain_id INTEGER NOT NULL,
    vault_address TEXT NOT NULL,
    block_number INTEGER NOT NULL,
    price_per_share_raw TEXT NOT NULL,
    share_decimals INTEGER NOT NULL,
    source TEXT NOT NULL,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (chain_id, vault_address, block_number)
);

CREATE TABLE IF NOT EXISTS strategy_debt_flows (
    chain_id INTEGER NOT NULL,
    version TEXT NOT NULL,
    vault_address TEXT NOT NULL,
    strategy_address TEXT NOT NULL,
    direction TEXT NOT NULL,
    tx_hash TEXT NOT NULL,
    log_index INTEGER NOT NULL,
    block_number INTEGER NOT NULL,
    block_timestamp INTEGER NOT NULL,
    asset TEXT,
    asset_decimals INTEGER,
    debt_delta_raw TEXT NOT NULL,
    current_debt_raw TEXT,
    new_debt_raw TEXT,
    source_event TEXT NOT NULL,
    decoded_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (chain_id, tx_hash, log_index, direction)
);

CREATE INDEX IF NOT EXISTS strategy_debt_flows_asset_idx
ON strategy_debt_flows (chain_id, asset, block_timestamp);

CREATE INDEX IF NOT EXISTS strategy_debt_flows_strategy_idx
ON strategy_debt_flows (chain_id, strategy_address, block_number);

CREATE TABLE IF NOT EXISTS vault_fee_events (
    chain_id INTEGER NOT NULL,
    version TEXT NOT NULL,
    source TEXT NOT NULL,
    vault_address TEXT NOT NULL,
    strategy_address TEXT,
    recipient TEXT,
    tx_hash TEXT NOT NULL,
    log_index INTEGER NOT NULL,
    block_number INTEGER NOT NULL,
    block_timestamp INTEGER NOT NULL,
    asset TEXT,
    asset_decimals INTEGER,
    fee_raw TEXT NOT NULL,
    shares_raw TEXT,
    protocol_fees_raw TEXT,
    total_fees_raw TEXT,
    total_refunds_raw TEXT,
    decoded_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (chain_id, tx_hash, log_index, source)
);

CREATE INDEX IF NOT EXISTS vault_fee_events_asset_idx
ON vault_fee_events (chain_id, asset, block_timestamp);

CREATE INDEX IF NOT EXISTS vault_fee_events_vault_idx
ON vault_fee_events (chain_id, vault_address, block_number);

CREATE TABLE IF NOT EXISTS canonical_fee_reports (
    chain_id INTEGER NOT NULL,
    tx_hash TEXT NOT NULL,
    report_log_index INTEGER NOT NULL,
    event_log_index INTEGER,
    contract_family TEXT NOT NULL,
    api_version TEXT,
    method_version TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('ok', 'unresolved')),
    reason TEXT,
    accounting_json TEXT,
    evidence_json TEXT NOT NULL,
    PRIMARY KEY (chain_id, tx_hash, report_log_index),
    FOREIGN KEY (chain_id, tx_hash, report_log_index)
        REFERENCES strategy_reports(chain_id, tx_hash, log_index) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS prices (
    chain_id INTEGER NOT NULL,
    token_address TEXT NOT NULL,
    timestamp INTEGER NOT NULL,
    block_number INTEGER,
    source TEXT NOT NULL,
    price_usd REAL,
    status TEXT NOT NULL,
    raw_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (chain_id, token_address, timestamp, source)
);

CREATE TABLE IF NOT EXISTS block_timestamps (
    chain_id INTEGER NOT NULL,
    block_number INTEGER NOT NULL,
    timestamp INTEGER NOT NULL,
    PRIMARY KEY (chain_id, block_number)
);

CREATE TABLE IF NOT EXISTS index_state (
    chain_id INTEGER NOT NULL,
    target TEXT NOT NULL,
    event_name TEXT NOT NULL,
    last_block INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (chain_id, target, event_name)
);

CREATE TABLE IF NOT EXISTS analysis_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    started_at INTEGER NOT NULL,
    completed_at INTEGER,
    status TEXT NOT NULL,
    params_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS analysis_outputs (
    run_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    row_json TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES analysis_runs(id)
);
"""


def connect(path: str | Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    from .coverage import SCHEMA as COVERAGE_SCHEMA

    conn.executescript(SCHEMA)
    conn.executescript(COVERAGE_SCHEMA)
    _ensure_column(conn, "vault_inventory_events", "action", "TEXT NOT NULL DEFAULT 'added'")
    conn.execute("""CREATE TABLE IF NOT EXISTS tokenized_fee_events (
        chain_id INTEGER NOT NULL,tx_hash TEXT NOT NULL,log_index INTEGER NOT NULL,
        event_json TEXT NOT NULL,PRIMARY KEY(chain_id,tx_hash,log_index))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS tokenized_fee_ranges (
        chain_id INTEGER NOT NULL,vault_address TEXT NOT NULL,from_block INTEGER NOT NULL,
        to_block INTEGER NOT NULL,before_timestamp INTEGER NOT NULL,inventory_hash TEXT NOT NULL,
        PRIMARY KEY(chain_id,vault_address,from_block,to_block,before_timestamp,inventory_hash))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS fee_state_evidence (
        chain_id INTEGER NOT NULL,tx_hash TEXT NOT NULL,report_log_index INTEGER NOT NULL,
        block_hash TEXT NOT NULL,evidence_json TEXT NOT NULL,
        PRIMARY KEY(chain_id,tx_hash,report_log_index,block_hash))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS fee_block_log_evidence (
        chain_id INTEGER NOT NULL,vault_address TEXT NOT NULL,block_hash TEXT NOT NULL,
        logs_json TEXT NOT NULL,PRIMARY KEY(chain_id,vault_address,block_hash))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS fee_log_acquisitions (
        acquisition_id TEXT PRIMARY KEY,manifest_json TEXT NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS fee_filtered_log_evidence (
        chain_id INTEGER NOT NULL,tx_hash TEXT NOT NULL,report_log_index INTEGER NOT NULL,
        payload_json TEXT NOT NULL,payload_sha256 TEXT NOT NULL,
        PRIMARY KEY(chain_id,tx_hash,report_log_index))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS fee_receipt_evidence (
        chain_id INTEGER NOT NULL,tx_hash TEXT NOT NULL,block_number INTEGER NOT NULL,
        block_hash TEXT NOT NULL,receipt_json TEXT NOT NULL,PRIMARY KEY(chain_id,tx_hash))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS fee_execution_traces (
        chain_id INTEGER NOT NULL,tx_hash TEXT NOT NULL,vault_address TEXT NOT NULL,
        block_hash TEXT NOT NULL,trace_version TEXT NOT NULL,trace_json TEXT NOT NULL,
        PRIMARY KEY(chain_id,tx_hash,vault_address,block_hash,trace_version))""")
    _ensure_column(conn, "tokenized_fee_ranges", "finality_policy", "TEXT NOT NULL DEFAULT 'finalized'")
    _ensure_column(conn, "vaults", "management", "TEXT NOT NULL DEFAULT 'yearn'")
    _ensure_column(conn, "vaults", "protocol", "TEXT")
    _ensure_column(conn, "canonical_fee_reports", "candidate_json", "TEXT")
    _ensure_column(conn, "canonical_fee_reports", "decision_json", "TEXT")
    # Old direct-zero projections must not survive a newly corrected release rule.
    from .fee_zero_rules import ZERO_GAIN_RELEASES
    for row in conn.execute("""SELECT f.*,r.gain_raw FROM canonical_fee_reports f JOIN strategy_reports r
        ON r.chain_id=f.chain_id AND r.tx_hash=f.tx_hash AND r.log_index=f.report_log_index
        WHERE f.contract_family='yearn-v2-vault' AND f.candidate_json IS NULL
        AND f.accounting_json IS NOT NULL AND r.gain_raw='0'""").fetchall():
        evidence = json.loads(row['evidence_json'])
        amounts = json.loads(row['accounting_json'])
        if (row['api_version'] not in ZERO_GAIN_RELEASES and evidence.get('fee_log_index') is None
                and row['event_log_index'] in (None,row['report_log_index'])
                and amounts.get('total_fees_paid_raw') == '0'):
            from .fee_acceptance import POLICY_VERSION
            decision={'policy_version':POLICY_VERSION,'status':'unavailable',
                'reason':'zero_gain_requires_fee_evidence','state_scope':None,'state_suitability':'unverified'}
            conn.execute("""UPDATE canonical_fee_reports SET accounting_json=NULL,status='unresolved',reason=?,decision_json=?
                WHERE chain_id=? AND tx_hash=? AND report_log_index=?""",
                (decision['reason'],to_json(decision),row['chain_id'],row['tx_hash'],row['report_log_index']))
    # Conservatively reassess legacy derived values without refetching evidence.
    from .fee_acceptance import assess_candidate, normalize_candidate
    for row in conn.execute("SELECT * FROM canonical_fee_reports WHERE candidate_json IS NULL AND accounting_json IS NOT NULL").fetchall():
        amounts = json.loads(row['accounting_json'])
        if amounts.get('amount_source') != 'derived-contract':
            continue
        evidence = json.loads(row['evidence_json'])
        candidate = normalize_candidate(evidence.pop('reconstruction', {
            'method': 'legacy-unverified', 'total_fees_paid_raw': amounts['total_fees_paid_raw'], 'interval': None}))
        decision = assess_candidate(candidate, evidence)
        accepted = decision['status'] == 'accepted'
        conn.execute("""UPDATE canonical_fee_reports SET candidate_json=?,decision_json=?,evidence_json=?,
            accounting_json=?,status=?,reason=? WHERE chain_id=? AND tx_hash=? AND report_log_index=?""",
            (to_json(candidate),to_json(decision),to_json(evidence),row['accounting_json'] if accepted else None,
             'ok' if accepted else 'unresolved',decision['reason'],row['chain_id'],row['tx_hash'],row['report_log_index']))
    conn.commit()


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def seed_chains(conn: sqlite3.Connection, chains: dict[str, Any]) -> None:
    rows = [
        (cfg.chain_id, cfg.key, cfg.name, cfg.rpc_env, cfg.defillama_slug)
        for cfg in chains.values()
    ]
    conn.executemany(
        """
        INSERT INTO chains (chain_id, chain_key, name, rpc_env, defillama_slug)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(chain_id) DO UPDATE SET
            chain_key=excluded.chain_key,
            name=excluded.name,
            rpc_env=excluded.rpc_env,
            defillama_slug=excluded.defillama_slug
        """,
        rows,
    )
    conn.commit()


def to_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def from_json(value: str) -> Any:
    return json.loads(value)


def upsert_many(conn: sqlite3.Connection, sql: str, rows: Iterable[tuple[Any, ...]]) -> int:
    cur = conn.executemany(sql, rows)
    conn.commit()
    return cur.rowcount
