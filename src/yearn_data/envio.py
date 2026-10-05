"""Envio-backed discovery and report ingestion for lifetime-yield.

The Envio Hasura endpoint supplies decoded report events and their block timestamps,
replacing the expensive RPC ``eth_getLogs``/``eth_getBlock`` backfill path. Yearn
Kong supplies missing vault and underlying-token metadata, including
``asset.decimals``, so Envio ingestion needs no RPC endpoint.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Callable, Iterable

import requests
from web3 import Web3

from .config import CHAINS, V2_REGISTRIES_BY_CHAIN, V3_ROLE_MANAGERS, get_envio_graphql_url
from .discovery import VaultRecord
from .kong import vault_metadata_from_kong
from .indexing import (
    ProgressCallback, get_index_state, insert_strategy_report,
    normalize_strategy_report, set_index_state,
)

V3_REPORT_FIELDS = (
    "gain loss current_debt protocol_fees total_fees total_refunds "
    "strategy vaultAddress chainId blockNumber blockTimestamp transactionHash logIndex"
)
V2_REPORT_FIELDS = (
    "gain loss debtAdded debtPaid totalGain totalLoss totalDebt debtRatio "
    "strategy vaultAddress chainId blockNumber blockTimestamp transactionHash logIndex"
)
DEFAULT_PAGE_SIZE = 1_000

V3_VAULT_INVENTORY_ENTITIES = (
    (
        "V3RegistryNewEndorsedVault",
        "registry",
        "registryAddress vault asset releaseVersion vaultType "
        "chainId blockNumber blockTimestamp transactionHash logIndex",
        "vault",
        "registryAddress",
        "asset",
        "added",
    ),
    (
        "V3VaultFactoryNewVault",
        "factory",
        "factoryAddress vault_address asset "
        "chainId blockNumber blockTimestamp transactionHash logIndex",
        "vault_address",
        "factoryAddress",
        "asset",
        "added",
    ),
    (
        "V3RoleManagerAddedNewVault",
        "role_manager",
        "roleManagerAddress vault debtAllocator category "
        "chainId blockNumber blockTimestamp transactionHash logIndex",
        "vault",
        "roleManagerAddress",
        None,
        "added",
    ),
    (
        "V3RoleManagerRemovedVault",
        "role_manager",
        "roleManagerAddress vault "
        "chainId blockNumber blockTimestamp transactionHash logIndex",
        "vault",
        "roleManagerAddress",
        None,
        "removed",
    ),
)
V2_VAULT_INVENTORY_ENTITIES = (
    (
        "V2RegistryNewVault",
        "registry",
        "registryAddress token deployment_id vault api_version "
        "chainId blockNumber blockTimestamp transactionHash logIndex",
        "vault",
        "registryAddress",
        "token",
        "added",
    ),
    (
        "V2RegistryNewExperimentalVault",
        "registry_experimental",
        "registryAddress token deployer vault api_version "
        "chainId blockNumber blockTimestamp transactionHash logIndex",
        "vault",
        "registryAddress",
        "token",
        "added",
    ),
    (
        "V2Registry2NewVault",
        "registry2",
        "registryAddress token vaultId vaultType vault apiVersion "
        "chainId blockNumber blockTimestamp transactionHash logIndex",
        "vault",
        "registryAddress",
        "token",
        "added",
    ),
)

DEFAULT_WINDOW_BLOCKS = 1_000_000
DEFAULT_REQUEST_TIMEOUT = 120


def _page_size() -> int:
    try:
        return max(100, int(os.environ.get("YEARN_ENVIO_PAGE_SIZE", DEFAULT_PAGE_SIZE)))
    except ValueError:
        return DEFAULT_PAGE_SIZE


def _window_blocks() -> int:
    try:
        return max(10_000, int(os.environ.get("YEARN_ENVIO_WINDOW_BLOCKS", DEFAULT_WINDOW_BLOCKS)))
    except ValueError:
        return DEFAULT_WINDOW_BLOCKS


def _gql(
    query: str,
    variables: dict[str, Any] | None = None,
    timeout: int = DEFAULT_REQUEST_TIMEOUT,
) -> dict[str, Any]:
    """POST a GraphQL query to Envio with bounded retry on transient errors."""
    payload: dict[str, Any] = {"query": query}
    if variables:
        payload["variables"] = variables

    last_error: Exception | None = None
    for attempt in range(5):
        try:
            response = requests.post(get_envio_graphql_url(), json=payload, timeout=timeout)
            if response.status_code == 429 or response.status_code >= 500:
                last_error = RuntimeError(f"envio HTTP {response.status_code}")
                time.sleep(2**attempt)
                continue
            response.raise_for_status()
            body = response.json()
            if "errors" in body:
                raise RuntimeError(f"envio GraphQL errors: {body['errors']}")
            return body["data"]
        except requests.RequestException as exc:
            last_error = exc
            time.sleep(2**attempt)
    raise RuntimeError("envio GraphQL request failed after retries") from last_error


def _page_query(
    entity: str,
    fields: str,
    chain_id: int,
    lo: int,
    hi: int,
    address_field: str | None = None,
) -> str:
    """Build a forward-cursored query over inclusive block window ``[lo, hi]``."""
    address_filter = f"{address_field}: {{_in: $addresses}}, " if address_field else ""
    where = (
        f"chainId: {{_eq: {chain_id}}}, "
        f"{address_filter}"
        f"blockNumber: {{_gte: {lo}, _lte: {hi}}}, "
        f"_or: [{{blockNumber: {{_gt: $lb}}}}, "
        f"{{_and: [{{blockNumber: {{_eq: $lb}}}}, {{logIndex: {{_gt: $li}}}}]}}]"
    )
    address_variable = ", $addresses: [String!]!" if address_field else ""
    return (
        f"query($lb: Int!, $li: Int!, $n: Int!{address_variable}) {{ "
        f"{entity}(where: {{{where}}}, "
        f"order_by: [{{blockNumber: asc}}, {{logIndex: asc}}], limit: $n) {{ {fields} }} }}"
    )


def _pull_window(
    entity: str,
    fields: str,
    chain_id: int,
    lo: int,
    hi: int,
    page_size: int | None = None,
    address_field: str | None = None,
    address_values: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Read every event in an inclusive Envio block range."""
    if lo > hi:
        return []

    query = _page_query(entity, fields, chain_id, lo, hi, address_field)
    nodes: list[dict[str, Any]] = []
    page_limit = page_size or _page_size()
    block_number, log_index = lo - 1, -1
    while True:
        variables = {"lb": block_number, "li": log_index, "n": page_limit}
        if address_field:
            variables["addresses"] = list(dict.fromkeys(
                form for address in address_values or []
                for form in (address.lower(), Web3.to_checksum_address(address))
            ))
        data = _gql(query, variables)
        if entity not in data or not isinstance(data[entity], list):
            raise ValueError(f"missing Envio entity response: {entity}")
        page = data[entity]
        cursor = (block_number, log_index)
        selected = {address.lower() for address in address_values or []}
        for node in page:
            position = (int(node["blockNumber"]), int(node["logIndex"]))
            if position <= cursor or position[1] < 0 or not lo <= position[0] <= hi:
                raise ValueError("Envio pagination or range mismatch")
            if int(node["chainId"]) != chain_id:
                raise ValueError("Envio chain mismatch")
            if address_field and node[address_field].lower() not in selected:
                raise ValueError("Envio address mismatch")
            cursor = position
        if not page:
            break
        nodes.extend(page)
        if len(page) < page_limit:
            break
        last = page[-1]
        block_number, log_index = int(last["blockNumber"]), int(last["logIndex"])
    return nodes


def _windows(lo: int, hi: int, size: int) -> Iterable[tuple[int, int]]:
    """Yield inclusive, non-overlapping block windows."""
    current = lo
    while current <= hi:
        end = min(current + size - 1, hi)
        yield current, end
        current = end + 1


def latest_processed_block(chain_id: int) -> int:
    """Return Envio's highest fully processed block for a chain."""
    data = _gql(
        "query($chain: Int!) { chain_metadata(where: {chain_id: {_eq: $chain}}) "
        "{ latest_processed_block } }",
        {"chain": int(chain_id)},
    )
    rows = data.get("chain_metadata") or []
    if not rows:
        raise RuntimeError(f"envio chain_metadata missing for chain_id {chain_id}")
    return int(rows[0]["latest_processed_block"])


def _resolve_to_block(chain: str, to_block: int | None) -> int:
    if to_block is not None:
        return int(to_block)
    return latest_processed_block(CHAINS[chain].chain_id)


def _cursor_event(entity: str) -> str:
    return f"{entity}:envio"


def _get_envio_cursor(conn, chain_id: int, entity: str) -> int | None:
    return get_index_state(conn, chain_id, "*", _cursor_event(entity))


def _set_envio_cursor(conn, chain_id: int, entity: str, last_block: int) -> None:
    set_index_state(conn, chain_id, "*", _cursor_event(entity), last_block)


def _vault_asset_map(conn, chain_id: int, include_inactive: bool = False) -> dict[str, tuple[str | None, int | None]]:
    rows = conn.execute(
        """
        SELECT address, asset, asset_decimals
        FROM vaults
        WHERE chain_id=? AND management='yearn' AND (active=1 OR ?)
        """,
        (chain_id, include_inactive),
    ).fetchall()
    return {
        Web3.to_checksum_address(row["address"]).lower(): (
            Web3.to_checksum_address(row["asset"]) if row["asset"] else None,
            int(row["asset_decimals"]) if row["asset_decimals"] is not None else None,
        )
        for row in rows
    }


def _log(node: dict[str, Any]) -> dict[str, Any]:
    return {
        "transactionHash": node["transactionHash"],
        "logIndex": int(node["logIndex"]),
        "blockNumber": int(node["blockNumber"]),
    }


def _required_vault_asset(
    node: dict[str, Any],
    assets: dict[str, tuple[str | None, int | None]],
    entity: str,
) -> tuple[str, tuple[str | None, int | None]]:
    vault = Web3.to_checksum_address(node["vaultAddress"])
    metadata = assets.get(vault.lower())
    if metadata is None:
        raise RuntimeError(
            f"cannot import {entity} for unclassified vault {vault}; "
            "refresh vault inventory before advancing the Envio cursor"
        )
    return vault, metadata


def _v3_report_args(node: dict[str, Any]) -> dict[str, Any]:
    return {
        "strategy": node["strategy"],
        "gain": int(node["gain"]),
        "loss": int(node["loss"]),
        "current_debt": int(node["current_debt"]),
        "protocol_fees": int(node["protocol_fees"]),
        "total_fees": int(node["total_fees"]),
        "total_refunds": int(node["total_refunds"]),
    }


def _v2_report_args(node: dict[str, Any]) -> dict[str, Any]:
    return {
        "strategy": node["strategy"],
        "gain": int(node["gain"]),
        "loss": int(node["loss"]),
        "debtPaid": int(node.get("debtPaid") or 0),
        "totalGain": int(node["totalGain"]),
        "totalLoss": int(node["totalLoss"]),
        "totalDebt": int(node["totalDebt"]),
        "debtAdded": int(node["debtAdded"]),
        "debtRatio": int(node["debtRatio"]),
    }


def _import_report_entity(
    conn,
    chain: str,
    entity: str,
    version: str,
    fields: str,
    hi: int,
    assets: dict[str, tuple[str | None, int | None]],
    args_for_node: Callable[[dict[str, Any]], dict[str, Any]],
    progress: ProgressCallback | None,
    from_block: int | None = None,
) -> int:
    cfg = CHAINS[chain]
    cursor = _get_envio_cursor(conn, cfg.chain_id, entity)
    lo = from_block if from_block is not None else cursor + 1 if cursor is not None else 0
    if lo > hi:
        return 0

    inserted = 0
    for window_lo, window_hi in _windows(lo, hi, _window_blocks()):
        nodes = _pull_window(
            entity,
            fields,
            cfg.chain_id,
            window_lo,
            window_hi,
            address_field="vaultAddress",
            address_values=[Web3.to_checksum_address(address) for address in assets],
        )
        for node in nodes:
            vault, (asset, decimals) = _required_vault_asset(node, assets, entity)
            # An observed report is evidence of existence, even when legacy
            # metadata incorrectly labels a later registration as deployment.
            row = normalize_strategy_report(
                cfg.chain_id,
                version,
                vault,
                asset,
                decimals,
                _log(node),
                int(node["blockTimestamp"]),
                args_for_node(node),
            )
            insert_strategy_report(conn, row)
            inserted += 1
        if from_block is None:
            _set_envio_cursor(conn, cfg.chain_id, entity, window_hi)
        conn.commit()
        if progress and nodes:
            progress(f"{chain} {version}: {entity} pulled through block {window_hi} (+{len(nodes)} reports)")
    return inserted


def import_reports_from_envio(
    conn,
    chains: list[str],
    versions: list[str] | None = None,
    to_block: int | None = None,
    progress: ProgressCallback | None = None,
    include_inactive: bool = False,
    from_block: int | None = None,
    vault_addresses: list[str] | None = None,
) -> int:
    """Import report deltas, or bounded history without changing forward cursors."""
    if from_block is not None and (to_block is None or from_block < 0 or from_block > to_block):
        raise ValueError("historical import requires 0 <= from_block <= explicit to_block")
    if vault_addresses is not None and from_block is None:
        raise ValueError("vault-scoped import requires from_block to preserve global cursors")
    requested_versions = set(versions or ("v2", "v3"))
    total = 0
    for chain in chains:
        cfg = CHAINS[chain]
        hi = _resolve_to_block(chain, to_block)
        assets = _vault_asset_map(conn, cfg.chain_id, include_inactive)
        if vault_addresses is not None:
            selected = {address.lower() for address in vault_addresses}
            missing = selected - assets.keys()
            if missing:
                raise ValueError(f"unclassified or excluded vaults: {sorted(missing)}")
            assets = {key: value for key, value in assets.items() if key in selected}
        if not assets:
            continue
        if "v3" in requested_versions:
            total += _import_report_entity(
                conn,
                chain,
                "StrategyReported",
                "v3",
                V3_REPORT_FIELDS,
                hi,
                assets,
                _v3_report_args,
                progress,
                from_block,
            )
        if "v2" in requested_versions:
            total += _import_report_entity(
                conn,
                chain,
                "V2StrategyReported",
                "v2",
                V2_REPORT_FIELDS,
                hi,
                assets,
                _v2_report_args,
                progress,
                from_block,
            )
    conn.commit()
    return total


V2_FEE_TRANSFER_TX_BATCH_SIZE = 500


def _import_vault_inventory_entities(
    conn,
    chain: str,
    hi: int,
    version: str,
    definitions,
    progress: ProgressCallback | None,
) -> int:
    """Persist the explicit deployment/classification events used for discovery."""
    cfg = CHAINS[chain]
    inserted = 0
    for entity, source_kind, fields, vault_field, source_field, asset_field, action in definitions:
        cursor = _get_envio_cursor(conn, cfg.chain_id, entity)
        lo = cursor + 1 if cursor is not None else 0
        if lo > hi:
            continue
        for window_lo, window_hi in _windows(lo, hi, _window_blocks()):
            nodes = _pull_window(entity, fields, cfg.chain_id, window_lo, window_hi)
            for node in nodes:
                asset = node.get(asset_field) if asset_field else None
                cur = conn.execute(
                    """
                    INSERT OR IGNORE INTO vault_inventory_events (
                        chain_id, version, vault_address, source_kind, source_address,
                        action, asset, tx_hash, log_index, block_number, block_timestamp,
                        decoded_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        cfg.chain_id,
                        version,
                        Web3.to_checksum_address(node[vault_field]),
                        source_kind,
                        Web3.to_checksum_address(node[source_field]),
                        action,
                        Web3.to_checksum_address(asset) if asset else None,
                        node["transactionHash"],
                        int(node["logIndex"]),
                        int(node["blockNumber"]),
                        int(node["blockTimestamp"]),
                        json.dumps(node, sort_keys=True),
                    ),
                )
                inserted += cur.rowcount
            _set_envio_cursor(conn, cfg.chain_id, entity, window_hi)
            conn.commit()
            if progress and nodes:
                progress(
                    f"{chain} {version}: imported {len(nodes)} {source_kind} inventory events "
                    f"through block {window_hi}"
                )
    return inserted


def _import_v3_vault_inventory(conn, chain: str, hi: int, progress: ProgressCallback | None) -> int:
    return _import_vault_inventory_entities(
        conn, chain, hi, "v3", V3_VAULT_INVENTORY_ENTITIES, progress
    )


def _import_v2_vault_inventory(conn, chain: str, hi: int, progress: ProgressCallback | None) -> int:
    return _import_vault_inventory_entities(
        conn, chain, hi, "v2", V2_VAULT_INVENTORY_ENTITIES, progress
    )


def _inventory_addresses(
    conn,
    chain_id: int,
    version: str,
    *,
    source_kinds: tuple[str, ...] | None = None,
    source_addresses: tuple[str, ...] | None = None,
    max_block: int | None = None,
) -> set[str]:
    filters = ["chain_id=?", "version=?"]
    params: list[Any] = [chain_id, version]
    if source_kinds:
        filters.append(f"source_kind IN ({','.join('?' for _ in source_kinds)})")
        params.extend(source_kinds)
    if source_addresses:
        filters.append(f"lower(source_address) IN ({','.join('?' for _ in source_addresses)})")
        params.extend(address.lower() for address in source_addresses)
    if max_block is not None:
        filters.append("block_number<=?")
        params.append(max_block)
    rows = conn.execute(
        f"""
        SELECT DISTINCT vault_address
        FROM vault_inventory_events
        WHERE {' AND '.join(filters)}
        """,
        params,
    ).fetchall()
    return {Web3.to_checksum_address(row["vault_address"]) for row in rows}


def _inventory_facts(
    conn,
    chain_id: int,
    version: str,
    max_block: int | None = None,
    source_kinds: tuple[str, ...] | None = None,
    source_addresses: tuple[str, ...] | None = None,
) -> dict[str, dict[str, Any]]:
    filters = ["chain_id=?", "version=?"]
    params: list[Any] = [chain_id, version]
    if max_block is not None:
        filters.append("block_number<=?")
        params.append(max_block)
    if source_kinds:
        filters.append(f"source_kind IN ({','.join('?' for _ in source_kinds)})")
        params.extend(source_kinds)
    if source_addresses:
        filters.append(f"lower(source_address) IN ({','.join('?' for _ in source_addresses)})")
        params.extend(address.lower() for address in source_addresses)
    rows = conn.execute(
        f"""
        SELECT *
        FROM vault_inventory_events
        WHERE {' AND '.join(filters)}
        ORDER BY block_number, log_index
        """,
        params,
    ).fetchall()
    facts: dict[str, dict[str, Any]] = {}
    for row in rows:
        address = Web3.to_checksum_address(row["vault_address"]).lower()
        fact = facts.setdefault(
            address,
            {
                "source_address": row["source_address"],
                "asset": row["asset"],
                "registration_block": int(row["block_number"]),
                "deployment_block": None,
                "source_kinds": set(),
            },
        )
        # Factory NewVault is creation evidence; registry and role-manager
        # membership may occur long after deployment.
        if row["source_kind"] == "factory" and fact["deployment_block"] is None:
            fact["deployment_block"] = int(row["block_number"])
        fact["source_kinds"].add(row["source_kind"])
        if fact["asset"] is None and row["asset"]:
            fact["asset"] = row["asset"]
    return facts


def _active_v3_role_manager_addresses(
    conn,
    chain_id: int,
    role_manager_address: str,
    max_block: int,
) -> set[str]:
    """Replay role-manager lifecycle events to reconstruct membership at a block."""
    rows = conn.execute(
        """
        SELECT vault_address, action
        FROM vault_inventory_events
        WHERE chain_id=?
          AND version='v3'
          AND source_kind='role_manager'
          AND lower(source_address)=?
          AND block_number<=?
        ORDER BY block_number, log_index
        """,
        (chain_id, role_manager_address.lower(), max_block),
    ).fetchall()
    active: set[str] = set()
    for row in rows:
        address = Web3.to_checksum_address(row["vault_address"])
        if row["action"] == "removed":
            active.discard(address)
        else:
            active.add(address)
    return active


def _existing_vaults(conn, chain_id: int) -> dict[str, Any]:
    rows = conn.execute("SELECT * FROM vaults WHERE chain_id=?", (chain_id,)).fetchall()
    return {Web3.to_checksum_address(row["address"]).lower(): row for row in rows}


def _build_envio_vault_records(
    conn,
    chain: str,
    version: str,
    addresses: list[str],
    source_address_override: str | None = None,
    max_block: int | None = None,
    inventory_source_kinds: tuple[str, ...] | None = None,
    inventory_source_addresses: tuple[str, ...] | None = None,
) -> list[VaultRecord]:
    """Reuse cached metadata; resolve only missing records through Yearn Kong."""
    cfg = CHAINS[chain]
    existing = _existing_vaults(conn, cfg.chain_id)
    inventory = _inventory_facts(
        conn,
        cfg.chain_id,
        version,
        max_block=max_block,
        source_kinds=inventory_source_kinds,
        source_addresses=inventory_source_addresses,
    )
    normalized = [Web3.to_checksum_address(address) for address in addresses]
    metadata_targets = [
        address
        for address in normalized
        if (row := existing.get(address.lower())) is None
        or row["asset"] is None
        or row["asset_decimals"] is None
    ]
    metadata_by_address = vault_metadata_from_kong(cfg.chain_id, metadata_targets) if metadata_targets else {}

    records: list[VaultRecord] = []
    for address in normalized:
        row = existing.get(address.lower())
        inventory_fact = inventory.get(address.lower())
        metadata = metadata_by_address.get(address)
        asset = (
            metadata["asset"]
            if metadata
            else row["asset"]
            if row
            else inventory_fact["asset"]
            if inventory_fact
            else None
        )
        asset_symbol = metadata["asset_symbol"] if metadata else row["asset_symbol"] if row else None
        asset_decimals = metadata["asset_decimals"] if metadata else row["asset_decimals"] if row else None
        name = metadata["name"] if metadata else row["name"] if row else None
        api_version = metadata["api_version"] if metadata else row["api_version"] if row else None
        if metadata is None and row is None:
            continue

        records.append(
            VaultRecord(
                chain_id=cfg.chain_id,
                version=version,
                address=address,
                source_address=(
                    source_address_override
                    if source_address_override
                    else row["source_address"]
                    if row
                    else inventory_fact["source_address"]
                    if inventory_fact
                    else None
                ),
                asset=asset,
                asset_symbol=asset_symbol,
                asset_decimals=int(asset_decimals) if asset_decimals is not None else None,
                name=name,
                api_version=api_version,
                deployment_block=(
                    row["deployment_block"]
                    if row
                    else inventory_fact["deployment_block"]
                    if inventory_fact
                    else None
                ),
                management=row["management"] if row else "yearn",
                protocol=row["protocol"] if row else None,
            )
        )
    return records


def _upsert_envio_vaults(conn, vaults: list[VaultRecord]) -> int:
    """Refresh metadata without replacing facts absent from Envio event rows."""
    if not vaults:
        return 0
    now = int(time.time())
    rows = [
        (
            vault.chain_id,
            vault.version,
            vault.address,
            vault.source_address,
            vault.asset,
            vault.asset_symbol,
            vault.asset_decimals,
            vault.name,
            vault.api_version,
            vault.management,
            vault.protocol,
            vault.deployment_block,
            now,
        )
        for vault in vaults
    ]
    cur = conn.executemany(
        """
        INSERT INTO vaults (
            chain_id, version, address, source_address, asset, asset_symbol,
            asset_decimals, name, api_version, management, protocol,
            deployment_block, updated_at, active
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
        ON CONFLICT(chain_id, address) DO UPDATE SET
            version=excluded.version,
            asset=excluded.asset,
            asset_symbol=excluded.asset_symbol,
            asset_decimals=excluded.asset_decimals,
            name=excluded.name,
            api_version=excluded.api_version,
            active=1,
            updated_at=excluded.updated_at
        """,
        rows,
    )
    conn.commit()
    return cur.rowcount


def _set_active_membership(
    conn,
    chain_id: int,
    version: str,
    addresses: set[str],
) -> None:
    """Keep cached rows and mark only the selected block's membership active."""
    conn.execute(
        "UPDATE vaults SET active=0 WHERE chain_id=? AND version=? AND management='yearn'",
        (chain_id, version),
    )
    if addresses:
        placeholders = ",".join("?" for _ in addresses)
        conn.execute(
            f"""
            UPDATE vaults
            SET active=1
            WHERE chain_id=? AND version=? AND lower(address) IN ({placeholders})
            """,
            (chain_id, version, *(address.lower() for address in addresses)),
        )
    conn.commit()


def discover_from_envio(
    conn,
    chains: list[str],
    skip_v2: bool = False,
    include_experimental_v2: bool = False,
    include_retired: bool = False,
    to_block: int | None = None,
    progress: ProgressCallback | None = None,
) -> int:
    """Discover explicitly registered V2/V3 vaults from Envio and cache metadata."""
    total = 0
    for chain in chains:
        cfg = CHAINS[chain]
        hi = _resolve_to_block(chain, to_block)
        _import_v3_vault_inventory(conn, chain, hi, progress)
        if not skip_v2:
            _import_v2_vault_inventory(conn, chain, hi, progress)
        role_manager = V3_ROLE_MANAGERS.get(cfg.chain_id)
        active_v3 = (
            _active_v3_role_manager_addresses(conn, cfg.chain_id, role_manager, hi)
            if role_manager else set()
        )
        v3_addresses = (
            _inventory_addresses(
                conn, cfg.chain_id, "v3", source_kinds=("role_manager",),
                source_addresses=(role_manager,), max_block=hi,
            )
            if include_retired and role_manager else active_v3
        )
        v2_source_kinds = (
            ("registry", "registry2", "registry_experimental")
            if include_experimental_v2
            else ("registry", "registry2")
        )
        v2_addresses = (
            set()
            if skip_v2 or cfg.chain_id not in V2_REGISTRIES_BY_CHAIN
            else _inventory_addresses(
                conn,
                cfg.chain_id,
                "v2",
                source_kinds=v2_source_kinds,
                source_addresses=V2_REGISTRIES_BY_CHAIN.get(cfg.chain_id),
                max_block=hi,
            )
            - v3_addresses
        )
        if progress:
            progress(f"{chain}: discovered {len(v3_addresses)} v3 + {len(v2_addresses)} v2 vaults from envio")
        records = _build_envio_vault_records(
            conn,
            chain,
            "v3",
            sorted(v3_addresses),
            source_address_override=role_manager,
            max_block=hi,
        )
        records.extend(
            _build_envio_vault_records(
                conn,
                chain,
                "v2",
                sorted(v2_addresses),
                max_block=hi,
                inventory_source_kinds=v2_source_kinds,
                inventory_source_addresses=V2_REGISTRIES_BY_CHAIN.get(cfg.chain_id),
            )
        )
        total += _upsert_envio_vaults(conn, records)
        if role_manager is not None:
            _set_active_membership(conn, cfg.chain_id, "v3", active_v3)
        if not skip_v2:
            _set_active_membership(conn, cfg.chain_id, "v2", v2_addresses)
    return total
