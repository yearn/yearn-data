"""Evidence of successful, inclusive block scans, independent of legacy cursors."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import time


SCHEMA = """
CREATE TABLE IF NOT EXISTS history_sync_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at INTEGER NOT NULL,
    manifest_json TEXT NOT NULL,
    summary_json TEXT,
    status TEXT NOT NULL DEFAULT 'pending'
);
CREATE TABLE IF NOT EXISTS history_coverage (
    chain_id INTEGER NOT NULL,
    target TEXT NOT NULL,
    family TEXT NOT NULL,
    policy TEXT NOT NULL,
    from_block INTEGER NOT NULL CHECK(from_block >= 0),
    to_block INTEGER NOT NULL CHECK(to_block >= from_block),
    source TEXT NOT NULL,
    end_hash TEXT NOT NULL,
    row_count INTEGER NOT NULL CHECK(row_count >= 0),
    run_id INTEGER REFERENCES history_sync_runs(id),
    completed_at INTEGER NOT NULL,
    PRIMARY KEY(chain_id, target, family, policy, from_block, to_block)
);
"""


@dataclass(frozen=True)
class Scope:
    chain_id: int
    target: str
    family: str
    policy: str = "v1"

    @property
    def key(self):
        return self.chain_id, self.target.lower(), self.family, self.policy


def fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _coverage_table(table):
    if table not in ("history_coverage", "tvl_history_coverage"):
        raise ValueError("unsupported coverage table")
    return table


def missing_ranges(conn, scope: Scope, start: int, end: int, chunk_size: int, *, table="history_coverage"):
    """Subtract the union of covered intervals; chunk only the remaining gaps."""
    if start < 0 or end < start or chunk_size < 1:
        raise ValueError("invalid coverage bounds or chunk size")
    table = _coverage_table(table)
    cursor = start
    rows = conn.execute(
        f"""SELECT from_block, to_block FROM {table}
        WHERE chain_id=? AND target=? AND family=? AND policy=?
          AND to_block>=? AND from_block<=? ORDER BY from_block, to_block""",
        (*scope.key, start, end),
    )
    for lo, hi in rows:
        while cursor < lo:
            stop = min(lo - 1, cursor + chunk_size - 1, end)
            yield cursor, stop
            cursor = stop + 1
        cursor = max(cursor, hi + 1)
    while cursor <= end:
        stop = min(end, cursor + chunk_size - 1)
        yield cursor, stop
        cursor = stop + 1


def commit_range(conn, scope: Scope, start: int, end: int, *, source: str,
                 end_hash: str, run_id: int | None, write, table="history_coverage"):
    """Commit rows and their coverage together. Writers must never commit internally."""
    if conn.in_transaction:
        raise ValueError("coverage writer requires a clean transaction")
    if start < 0 or end < start or not end_hash:
        raise ValueError("invalid range evidence")
    table = _coverage_table(table)
    with conn:
        count = write(conn)
        conn.execute(
            f"INSERT INTO {table} VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (*scope.key, start, end, source, end_hash, count, run_id, int(time.time())),
        )
    return count


def insert_checked(conn, table: str, values: dict, identity: tuple[str, ...], enrich=()):
    """Reuse identical observations, reject conflicts instead of silently ignoring them.

    Table/column names come exclusively from in-process adapters, never user input.
    """
    old = conn.execute(
        f"SELECT * FROM {table} WHERE " + " AND ".join(f"{key}=?" for key in identity),
        tuple(values[key] for key in identity),
    ).fetchone()
    if old is not None:
        updates = {key: values[key] for key in enrich if old[key] is None and values[key] is not None}
        def canonical(key, value):
            if key.endswith("_json") and value is not None:
                def normalize(item):
                    if isinstance(item, dict):
                        return {k: normalize(v) for k, v in item.items()}
                    if isinstance(item, list):
                        return [normalize(v) for v in item]
                    return item.lower() if isinstance(item, str) and item.startswith("0x") else item
                return normalize(json.loads(value))
            if isinstance(value, str) and value.startswith("0x"):
                return value.lower()
            return value
        if any(key not in updates and canonical(key, old[key]) != canonical(key, value) for key, value in values.items()):
            raise ValueError(f"conflicting observation in {table}")
        if updates:
            conn.execute(f"UPDATE {table} SET " + ','.join(f"{key}=?" for key in updates)
                         + " WHERE " + ' AND '.join(f"{key}=?" for key in identity),
                         (*updates.values(), *(values[key] for key in identity)))
        return 0
    conn.execute(
        f"INSERT INTO {table} ({','.join(values)}) VALUES ({','.join('?' for _ in values)})",
        tuple(values.values()),
    )
    return 1
