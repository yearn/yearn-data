"""Explicit one-chain report acquisition; accounting and valuation stay separate."""

import json
import time

from web3 import Web3

from . import abis
from .chains import web3_for
from .config import CHAINS
from .coverage import commit_range, missing_ranges
from .history_sources import HistoricalSource, inventory_targets, report_scope, route, write_rows
from .indexing import _hex
from .kong import vault_metadata_from_kong


def inventory_vaults(conn, chain, end, experimental):
    """Membership comes from historical facts, not current active flags."""
    targets = list(inventory_targets(chain, experimental))
    selected = {}
    for target in targets:
        for row in conn.execute("""SELECT * FROM vault_inventory_events WHERE chain_id=?
            AND lower(source_address)=? AND source_kind=? AND version=? AND block_number<=?
            ORDER BY block_number,log_index""",
            (target.chain_id, target.address.lower(), target.kind, target.version, end)):
            address = row["vault_address"].lower()
            fact = selected.setdefault(address, dict(row))
            if row["asset"]:
                fact["asset"] = row["asset"]
    # Replay all lifecycle events together (the targets above are separated by ABI).
    for fact in selected.values():
        fact["as_of_block"] = end
        if fact["version"] == "v3":
            latest = conn.execute("""SELECT action FROM vault_inventory_events WHERE chain_id=?
                AND lower(vault_address)=? AND lower(source_address)=? AND source_kind='role_manager'
                AND block_number<=? ORDER BY block_number DESC,log_index DESC LIMIT 1""",
                (fact["chain_id"], fact["vault_address"].lower(), fact["source_address"].lower(), end)).fetchone()
            fact["active"] = int(latest[0] == "added")
        else:
            fact["active"] = 1
    return list(selected.values())


def materialize_inventory(conn, facts):
    with conn:
        for fact in facts:
            old = conn.execute("SELECT version,asset,active FROM vaults WHERE chain_id=? AND lower(address)=?",
                               (fact["chain_id"], fact["vault_address"].lower())).fetchone()
            if old and (old["version"] != fact["version"] or (old["asset"] and fact["asset"] and old["asset"].lower() != fact["asset"].lower())):
                raise ValueError("inventory conflicts with cached vault identity")
            active = fact["active"]
            if old and fact["version"] == "v3" and conn.execute(
                """SELECT 1 FROM vault_inventory_events WHERE chain_id=? AND lower(vault_address)=?
                AND lower(source_address)=? AND source_kind='role_manager' AND block_number>? LIMIT 1""",
                (fact["chain_id"], fact["vault_address"].lower(), fact["source_address"].lower(), fact["as_of_block"]),
            ).fetchone():
                active = old["active"]
            arguments = json.loads(fact["decoded_json"])
            conn.execute("""INSERT INTO vaults(chain_id,version,address,source_address,asset,api_version,active,updated_at)
                VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(chain_id,address) DO UPDATE SET
                active=CASE WHEN excluded.version='v2' THEN vaults.active ELSE excluded.active END,
                asset=coalesce(vaults.asset,excluded.asset),
                api_version=coalesce(vaults.api_version,excluded.api_version),updated_at=excluded.updated_at""",
                (fact["chain_id"], fact["version"], Web3.to_checksum_address(fact["vault_address"]),
                 fact["source_address"], fact["asset"], arguments.get("api_version", arguments.get("apiVersion")),
                 active, int(time.time())))


def enrich_metadata(conn, client, vault, block):
    address = vault["address"]
    try:
        metadata = vault_metadata_from_kong(client.chain_id, [address]).get(Web3.to_checksum_address(address))
    except Exception:
        metadata = None
    if not metadata or metadata.get("asset_decimals") is None or not metadata.get("api_version"):
        contract = client.w3.eth.contract(address=Web3.to_checksum_address(address), abi=abis.VAULT_METADATA_ABI)
        fn = contract.functions.token if vault["version"] == "v2" else contract.functions.asset
        asset = Web3.to_checksum_address(fn().call(block_identifier=block))
        token = client.w3.eth.contract(address=asset, abi=abis.ERC20_ABI)
        metadata = dict(asset=asset, asset_decimals=int(token.functions.decimals().call(block_identifier=block)),
                        api_version=contract.functions.apiVersion().call(block_identifier=block))
    if vault["asset"] and vault["asset"].lower() != metadata["asset"].lower():
        raise ValueError("metadata conflicts with inventory asset")
    if not 0 <= metadata["asset_decimals"] <= 255:
        raise ValueError("invalid asset decimals")
    with conn:
        conn.execute("""UPDATE vaults SET asset=?,asset_decimals=?,api_version=?,
            asset_symbol=coalesce(?,asset_symbol),name=coalesce(?,name),updated_at=?
            WHERE chain_id=? AND address=?""",
            (metadata["asset"], metadata["asset_decimals"], metadata["api_version"], metadata.get("asset_symbol"),
             metadata.get("name"), int(time.time()), client.chain_id, address))



def catch_up(conn, chain, from_block, to_block, *, source="auto", confirmations=None,
             chunk_size=50_000, discover=False, experimental=False, client=None):
    """Acquire an explicit window and reuse successfully committed ranges.

    The caller chooses historical bounds; existing observations are not treated
    as proof of coverage. Retained classified Yearn vaults stay in scope even
    when they have no current registry membership. Exceptions stop this call;
    completed ranges are reusable when the same command is repeated.
    """
    if from_block < 0 or to_block < from_block or chunk_size < 1:
        raise ValueError("invalid catch-up bounds or chunk size")
    if experimental and not discover:
        raise ValueError("experimental discovery requires --discover")
    if confirmations is not None and confirmations < 1:
        raise ValueError("confirmations must be positive")
    chain_id = CHAINS[chain].chain_id
    report_source = route(chain_id, "reports", source)
    client = client or HistoricalSource(chain, web3_for(chain))
    pin = client.pin(end=to_block, confirmations=confirmations, use_envio=report_source == "envio")
    summary = dict(chain=chain, from_block=from_block, pin=pin, source=report_source,
                   completed_ranges=0, inserted_rows=0, vaults=0,
                   discovery="bounded" if discover else "stored inventory")

    def acquire(scope, fetch, table, selected_source):
        # Verify only reused coverage relevant to this request, not old unrelated
        # scans. New range rows and their coverage commit in one transaction.
        for number, block_hash in conn.execute(
            """SELECT DISTINCT to_block,end_hash FROM history_coverage
               WHERE chain_id=? AND target=? AND family=? AND policy=?
               AND to_block>=? AND from_block<=?""",
            (*scope.key, from_block, to_block),
        ):
            client.verify_pin(dict(number=number, hash=block_hash))
        for lo, hi in list(missing_ranges(conn, scope, from_block, to_block, chunk_size)):
            rows = fetch(lo, hi)
            end_hash = _hex(client.block(hi)["hash"])
            client.verify_pin(dict(number=hi, hash=end_hash))
            inserted = commit_range(conn, scope, lo, hi, source=selected_source,
                                    end_hash=end_hash, run_id=None,
                                    write=lambda db: write_rows(db, table, rows))
            summary["completed_ranges"] += 1
            summary["inserted_rows"] += inserted

    if discover:
        for target in inventory_targets(chain, experimental):
            selected_source = route(chain_id, target.scope.family, source)
            acquire(target.scope, lambda lo, hi: client.inventory(target, lo, hi, selected_source),
                    "vault_inventory_events", selected_source)
        materialize_inventory(conn, inventory_vaults(conn, chain, to_block, experimental))

    # No active flag or current registry join: both previously retained and newly
    # discovered Yearn vaults are eligible. External classifications stay excluded.
    vaults = [dict(row) for row in conn.execute(
        "SELECT * FROM vaults WHERE chain_id=? AND management='yearn' ORDER BY address", (chain_id,))]
    if not vaults:
        raise ValueError("no retained Yearn vaults; discover or import classified inventory first")
    for vault in vaults:
        if not vault["asset"] or vault["asset_decimals"] is None or not vault["api_version"]:
            enrich_metadata(conn, client, vault, to_block)
            vault = dict(conn.execute("SELECT * FROM vaults WHERE chain_id=? AND address=?",
                                      (chain_id, vault["address"])).fetchone())
        if vault["version"] not in {"v2", "v3"} or not vault["api_version"].startswith(
                "0." if vault["version"] == "v2" else "3."):
            raise ValueError(f"unsupported vault release: {vault['address']}")
        acquire(report_scope(vault), lambda lo, hi: client.reports(vault, lo, hi, report_source),
                "strategy_reports", report_source)
        summary["vaults"] += 1
    client.verify_pin(pin)
    return summary
