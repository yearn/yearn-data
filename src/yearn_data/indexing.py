"""Generic event backfill and strategy report normalization."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

import requests
from eth_abi import decode as abi_decode
from eth_utils import function_signature_to_4byte_selector
from web3 import Web3

from .abis import VAULT_SHARE_PRICE_ABI
from .chains import cached_block_timestamps_many, latest_block, web3_for
from .config import CHAINS, get_rpc_url
from .events import (
    debt_updated_event_abis,
    debt_updated_topics,
    decode_event,
    erc20_transfer_event_abi,
    erc20_transfer_topic,
    strategy_report_event_abis,
    strategy_report_topics,
    vault_flow_event_abis,
    vault_flow_topics,
)
from .storage import to_json


ProgressCallback = Callable[[str], None]
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
ZERO_TOPIC = "0x" + "0" * 64
PRICE_PER_SHARE_SELECTOR = "0x" + function_signature_to_4byte_selector("pricePerShare()").hex()
GET_PRICE_PER_FULL_SHARE_SELECTOR = "0x" + function_signature_to_4byte_selector("getPricePerFullShare()").hex()


def _hex(value: Any) -> str:
    if hasattr(value, "hex"):
        text = value.hex()
        return text if text.startswith("0x") else f"0x{text}"
    return str(value)


def _jsonable_args(args: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in args.items():
        if isinstance(value, bytes):
            out[key] = value.hex()
        else:
            out[key] = value
    return out


def get_index_state(conn, chain_id: int, target: str, event_name: str) -> int | None:
    row = conn.execute(
        "SELECT last_block FROM index_state WHERE chain_id=? AND target=? AND event_name=?",
        (chain_id, target, event_name),
    ).fetchone()
    return int(row["last_block"]) if row else None


def set_index_state(conn, chain_id: int, target: str, event_name: str, last_block: int) -> None:
    conn.execute(
        """
        INSERT INTO index_state (chain_id, target, event_name, last_block, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(chain_id, target, event_name) DO UPDATE SET
            last_block=excluded.last_block,
            updated_at=excluded.updated_at
        """,
        (chain_id, target, event_name, int(last_block), int(time.time())),
    )
    conn.commit()


def normalize_strategy_report(
    chain_id: int,
    version: str,
    vault_address: str,
    asset: str | None,
    asset_decimals: int | None,
    log: dict[str, Any],
    block_ts: int,
    args: dict[str, Any],
) -> tuple[Any, ...]:
    gain = int(args.get("gain", 0))
    loss = int(args.get("loss", 0))
    current_debt = args.get("current_debt", args.get("totalDebt"))
    extra = {
        key: int(value) if isinstance(value, int) else value
        for key, value in args.items()
        if key
        not in {
            "strategy",
            "gain",
            "loss",
            "current_debt",
            "protocol_fees",
            "total_fees",
            "total_refunds",
        }
    }
    return (
        chain_id,
        version,
        Web3.to_checksum_address(vault_address),
        Web3.to_checksum_address(args["strategy"]),
        _hex(log["transactionHash"]),
        int(log["logIndex"]),
        int(log["blockNumber"]),
        int(block_ts),
        Web3.to_checksum_address(asset) if asset else None,
        asset_decimals,
        str(gain),
        str(loss),
        str(gain - loss),
        str(current_debt) if current_debt is not None else None,
        str(args.get("protocol_fees")) if args.get("protocol_fees") is not None else None,
        str(args.get("total_fees")) if args.get("total_fees") is not None else None,
        str(args.get("total_refunds")) if args.get("total_refunds") is not None else None,
        to_json(extra),
    )


def insert_raw_event(
    conn,
    chain_id: int,
    contract_address: str,
    event_name: str,
    log: dict[str, Any],
    block_ts: int,
    args: dict[str, Any],
) -> None:
    conn.execute(
        """
            INSERT INTO events_raw (
                chain_id, contract_address, event_name, tx_hash, log_index,
                block_number, block_timestamp, decoded_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT DO NOTHING
        """,
        (
            chain_id,
            Web3.to_checksum_address(contract_address),
            event_name,
            _hex(log["transactionHash"]),
            int(log["logIndex"]),
            int(log["blockNumber"]),
            int(block_ts),
            json.dumps(_jsonable_args(args), sort_keys=True, default=str),
        ),
    )


def normalize_vault_flow(
    chain_id: int,
    version: str,
    vault_address: str,
    asset: str | None,
    asset_decimals: int | None,
    log: dict[str, Any],
    block_ts: int,
    event_name: str,
    args: dict[str, Any],
) -> tuple[Any, ...]:
    direction = "deposit" if event_name == "Deposit" else "withdraw"
    if version == "v2":
        sender = args.get("recipient")
        owner = args.get("recipient")
        receiver = args.get("recipient")
        assets = args.get("amount", 0)
    else:
        sender = args.get("sender")
        owner = args.get("owner")
        receiver = args.get("receiver") if direction == "withdraw" else args.get("owner")
        assets = args.get("assets", 0)
    return (
        chain_id,
        version,
        Web3.to_checksum_address(vault_address),
        direction,
        Web3.to_checksum_address(sender) if sender else None,
        Web3.to_checksum_address(owner) if owner else None,
        Web3.to_checksum_address(receiver) if receiver else None,
        _hex(log["transactionHash"]),
        int(log["logIndex"]),
        int(log["blockNumber"]),
        int(block_ts),
        Web3.to_checksum_address(asset) if asset else None,
        asset_decimals,
        str(int(assets)),
        str(int(args.get("shares", 0))),
        json.dumps(_jsonable_args(args), sort_keys=True, default=str),
    )


def normalize_v2_share_transfer_flow(
    chain_id: int,
    vault_address: str,
    asset: str | None,
    asset_decimals: int | None,
    log: dict[str, Any],
    block_ts: int,
    args: dict[str, Any],
    price_per_share_raw: int,
    share_decimals: int,
) -> tuple[Any, ...]:
    from_address = Web3.to_checksum_address(args["from"])
    to_address = Web3.to_checksum_address(args["to"])
    shares = int(args.get("value", 0))
    direction = "deposit" if from_address == Web3.to_checksum_address(ZERO_ADDRESS) else "withdraw"
    user = to_address if direction == "deposit" else from_address
    assets = shares * int(price_per_share_raw) // (10 ** int(share_decimals))
    return (
        chain_id,
        "v2",
        Web3.to_checksum_address(vault_address),
        direction,
        user,
        user,
        user,
        _hex(log["transactionHash"]),
        int(log["logIndex"]),
        int(log["blockNumber"]),
        int(block_ts),
        Web3.to_checksum_address(asset) if asset else None,
        asset_decimals,
        str(int(assets)),
        str(shares),
        json.dumps(
            {
                **_jsonable_args(args),
                "source_event": "Transfer",
                "price_per_share_raw": str(int(price_per_share_raw)),
                "share_decimals": int(share_decimals),
            },
            sort_keys=True,
            default=str,
        ),
    )


def insert_vault_flow(conn, row: tuple[Any, ...]) -> int:
    cur = conn.execute(
        """
            INSERT INTO vault_flows (
                chain_id, version, vault_address, direction, sender, owner, receiver,
                tx_hash, log_index, block_number, block_timestamp, asset,
                asset_decimals, assets_raw, shares_raw, decoded_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT DO NOTHING
        """,
        row,
    )
    return int(cur.rowcount)


def normalize_v3_debt_flow(
    chain_id: int,
    version: str,
    vault_address: str,
    asset: str | None,
    asset_decimals: int | None,
    log: dict[str, Any],
    block_ts: int,
    args: dict[str, Any],
) -> tuple[Any, ...] | None:
    current_debt = int(args.get("current_debt", 0))
    new_debt = int(args.get("new_debt", 0))
    delta = new_debt - current_debt
    if delta == 0:
        return None
    direction = "allocation" if delta > 0 else "deallocation"
    return (
        chain_id,
        version,
        Web3.to_checksum_address(vault_address),
        Web3.to_checksum_address(args["strategy"]),
        direction,
        _hex(log["transactionHash"]),
        int(log["logIndex"]),
        int(log["blockNumber"]),
        int(block_ts),
        Web3.to_checksum_address(asset) if asset else None,
        asset_decimals,
        str(abs(delta)),
        str(current_debt),
        str(new_debt),
        "DebtUpdated",
        json.dumps(_jsonable_args(args), sort_keys=True, default=str),
    )


def insert_strategy_debt_flow(conn, row: tuple[Any, ...]) -> int:
    cur = conn.execute(
        """
            INSERT INTO strategy_debt_flows (
                chain_id, version, vault_address, strategy_address, direction,
                tx_hash, log_index, block_number, block_timestamp, asset,
                asset_decimals, debt_delta_raw, current_debt_raw, new_debt_raw,
                source_event, decoded_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT DO NOTHING
        """,
        row,
    )
    return int(cur.rowcount)


def insert_vault_fee_event(conn, row: tuple[Any, ...]) -> int:
    cur = conn.execute(
        """
            INSERT INTO vault_fee_events (
                chain_id, version, source, vault_address, strategy_address, recipient,
                tx_hash, log_index, block_number, block_timestamp, asset,
                asset_decimals, fee_raw, shares_raw, protocol_fees_raw,
                total_fees_raw, total_refunds_raw, decoded_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT DO NOTHING
        """,
        row,
    )
    return int(cur.rowcount)


def insert_strategy_report(conn, row: tuple[Any, ...]) -> None:
    conn.execute(
        """
            INSERT INTO strategy_reports (
                chain_id, version, vault_address, strategy_address, tx_hash, log_index,
                block_number, block_timestamp, asset, asset_decimals, gain_raw, loss_raw,
                net_raw, current_debt_raw, protocol_fees_raw, total_fees_raw,
                total_refunds_raw, extra_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT DO NOTHING
        """,
        row,
    )


def index_vault_reports(conn, vault, to_block: int | None = None, chunk_size: int = 50_000) -> int:
    cfg = next(c for c in CHAINS.values() if c.chain_id == int(vault["chain_id"]))
    chain = cfg.key
    w3 = web3_for(chain)
    target = Web3.to_checksum_address(vault["address"])
    start = vault["deployment_block"] or 0
    last_done = get_index_state(conn, cfg.chain_id, target, "StrategyReported")
    from_block = max(start, (last_done + 1) if last_done is not None else start)
    final_block = int(to_block) if to_block is not None else latest_block(chain)
    if from_block > final_block:
        return 0

    version = vault["version"]
    event_abis = strategy_report_event_abis(version)
    topics = strategy_report_topics(version)
    inserted = 0
    current = from_block
    while current <= final_block:
        end = min(current + chunk_size - 1, final_block)
        logs = w3.eth.get_logs(
            {
                "fromBlock": current,
                "toBlock": end,
                "address": target,
                "topics": [topics],
            }
        )
        ts_cache = cached_block_timestamps_many(conn, chain, [int(log["blockNumber"]) for log in logs])
        for log in logs:
            event_abi, args = decode_event(chain, event_abis, log)
            _ = event_abi
            block_number = int(log["blockNumber"])
            block_ts = ts_cache[block_number]
            insert_raw_event(conn, cfg.chain_id, target, "StrategyReported", log, block_ts, args)
            row = normalize_strategy_report(
                cfg.chain_id,
                version,
                target,
                vault["asset"],
                vault["asset_decimals"],
                log,
                block_ts,
                args,
            )
            insert_strategy_report(conn, row)
            inserted += 1
        set_index_state(conn, cfg.chain_id, target, "StrategyReported", end)
        current = end + 1
    conn.commit()
    return inserted


def _get_logs_with_split(w3, params: dict[str, Any], min_chunk_size: int = 1_000) -> list[dict[str, Any]]:
    try:
        return list(w3.eth.get_logs(params))
    except Exception:
        start = int(params["fromBlock"])
        end = int(params["toBlock"])
        if end - start + 1 <= min_chunk_size:
            raise
        mid = (start + end) // 2
        left = dict(params, toBlock=mid)
        right = dict(params, fromBlock=mid + 1)
        return _get_logs_with_split(w3, left, min_chunk_size) + _get_logs_with_split(w3, right, min_chunk_size)


def _set_index_state_many(conn, chain_id: int, targets: list[str], event_name: str, last_block: int) -> None:
    now = int(time.time())
    conn.executemany(
        """
        INSERT INTO index_state (chain_id, target, event_name, last_block, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(chain_id, target, event_name) DO UPDATE SET
            last_block=excluded.last_block,
            updated_at=excluded.updated_at
        """,
        [(chain_id, target, event_name, int(last_block), now) for target in targets],
    )
    conn.commit()


def _topic_address(address: str) -> str:
    return "0x" + Web3.to_checksum_address(address)[2:].lower().rjust(64, "0")


def _vault_share_contract(chain: str, address: str):
    return web3_for(chain).eth.contract(address=Web3.to_checksum_address(address), abi=VAULT_SHARE_PRICE_ABI)


def _vault_share_decimals(chain: str, address: str, default: int | None) -> int:
    try:
        return int(_vault_share_contract(chain, address).functions.decimals().call())
    except Exception:
        return int(default or 18)


def _fetch_vault_price_per_share(chain: str, address: str, block_number: int) -> tuple[int, str]:
    contract = _vault_share_contract(chain, address)
    try:
        return int(contract.functions.pricePerShare().call(block_identifier=int(block_number))), "pricePerShare"
    except Exception:
        return int(contract.functions.getPricePerFullShare().call(block_identifier=int(block_number))), "getPricePerFullShare"


def _batch_eth_call(
    chain: str,
    calls: list[tuple[str, int, str]],
    batch_size: int = 100,
    timeout: int = 60,
) -> list[str | None]:
    url = get_rpc_url(chain)
    out: list[str | None] = []
    next_id = 1
    for i in range(0, len(calls), batch_size):
        chunk = calls[i : i + batch_size]
        payload = []
        id_to_index: dict[int, int] = {}
        for offset, (address, block_number, data) in enumerate(chunk):
            request_id = next_id
            next_id += 1
            id_to_index[request_id] = offset
            payload.append(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": "eth_call",
                    "params": [
                        {"to": Web3.to_checksum_address(address), "data": data},
                        hex(int(block_number)),
                    ],
                }
            )
        for attempt in range(5):
            response = requests.post(url, json=payload, timeout=timeout)
            if response.status_code != 429:
                response.raise_for_status()
                break
            time.sleep(2 ** attempt)
        else:
            response.raise_for_status()
        values: list[str | None] = [None] * len(chunk)
        body = response.json()
        if isinstance(body, dict):
            body = [body]
        for item in body:
            idx = id_to_index.get(int(item.get("id", 0)))
            if idx is None:
                continue
            result = item.get("result")
            values[idx] = result if isinstance(result, str) and result != "0x" else None
        out.extend(values)
    return out


def _decode_uint256_hex(value: str | None) -> int | None:
    if not value:
        return None
    try:
        data = bytes.fromhex(value[2:] if value.startswith("0x") else value)
        return int(abi_decode(["uint256"], data)[0])
    except Exception:
        return None


def _fetch_vault_price_per_share_many(
    chain: str,
    keys: list[tuple[str, int]],
) -> list[tuple[str, int, int, str]]:
    calls = [(address, block, PRICE_PER_SHARE_SELECTOR) for address, block in keys]
    primary = _batch_eth_call(chain, calls)
    output: list[tuple[str, int, int, str] | None] = [None] * len(keys)
    fallback_keys: list[tuple[int, str, int]] = []
    for idx, ((address, block), result) in enumerate(zip(keys, primary, strict=True)):
        value = _decode_uint256_hex(result)
        if value is None:
            fallback_keys.append((idx, address, block))
        else:
            output[idx] = (address, block, value, "pricePerShare")
    if fallback_keys:
        fallback_calls = [
            (address, block, GET_PRICE_PER_FULL_SHARE_SELECTOR)
            for _, address, block in fallback_keys
        ]
        fallback_results = _batch_eth_call(chain, fallback_calls)
        for (idx, address, block), result in zip(fallback_keys, fallback_results, strict=True):
            value = _decode_uint256_hex(result)
            if value is None:
                value, source = _fetch_vault_price_per_share(chain, address, block)
                output[idx] = (address, block, value, source)
            else:
                output[idx] = (address, block, value, "getPricePerFullShare")
    return [item for item in output if item is not None]


def _cached_vault_share_prices_many(
    conn,
    chain: str,
    vaults_by_address: dict[str, Any],
    logs: list[dict[str, Any]],
    max_workers: int = 16,
) -> dict[tuple[str, int], tuple[int, int]]:
    cfg = CHAINS[chain]
    keys = sorted(
        {
            (Web3.to_checksum_address(log["address"]), int(log["blockNumber"]))
            for log in logs
        }
    )
    if not keys:
        return {}

    found: dict[tuple[str, int], tuple[int, int]] = {}
    for address_chunk in _chunks(sorted({address for address, _ in keys}), 100):
        block_numbers = sorted({block for address, block in keys if address in address_chunk})
        if not block_numbers:
            continue
        address_placeholders = ",".join("?" for _ in address_chunk)
        block_placeholders = ",".join("?" for _ in block_numbers)
        rows = conn.execute(
            f"""
            SELECT vault_address, block_number, price_per_share_raw, share_decimals
            FROM vault_share_prices
            WHERE chain_id=?
              AND vault_address IN ({address_placeholders})
              AND block_number IN ({block_placeholders})
            """,
            [cfg.chain_id, *address_chunk, *block_numbers],
        ).fetchall()
        for row in rows:
            found[(Web3.to_checksum_address(row["vault_address"]), int(row["block_number"]))] = (
                int(row["price_per_share_raw"]),
                int(row["share_decimals"]),
            )

    missing = [(address, block) for address, block in keys if (address, block) not in found]
    if not missing:
        return found

    share_decimals_by_address = {
        address: _vault_share_decimals(chain, address, vaults_by_address[address]["asset_decimals"])
        for address in sorted({address for address, _ in missing})
    }
    fetched: list[tuple[str, int, int, int, str]] = []
    for address, block, price_per_share, source in _fetch_vault_price_per_share_many(chain, missing):
        share_decimals = share_decimals_by_address[address]
        found[(address, block)] = (price_per_share, share_decimals)
        fetched.append((address, block, price_per_share, share_decimals, source))

    now = int(time.time())
    conn.executemany(
        """
            INSERT INTO vault_share_prices (
                chain_id, vault_address, block_number, price_per_share_raw,
                share_decimals, source, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(chain_id,vault_address,block_number) DO UPDATE SET
                price_per_share_raw=excluded.price_per_share_raw,
                share_decimals=excluded.share_decimals,
                source=excluded.source,
                updated_at=excluded.updated_at
        """,
        [
            (cfg.chain_id, address, block, str(price_per_share), share_decimals, source, now)
            for address, block, price_per_share, share_decimals, source in fetched
        ],
    )
    return found


def _strategy_report_txs_for_logs(conn, chain_id: int, logs: list[dict[str, Any]]) -> set[tuple[str, str]]:
    pairs = sorted(
        {
            (Web3.to_checksum_address(log["address"]), _hex(log["transactionHash"]))
            for log in logs
        }
    )
    if not pairs:
        return set()
    found: set[tuple[str, str]] = set()
    for pair_chunk in _chunks(pairs, 400):
        clauses = " OR ".join("(vault_address=? AND tx_hash=?)" for _ in pair_chunk)
        params: list[Any] = [chain_id]
        for address, tx_hash in pair_chunk:
            params.extend([address, tx_hash])
        rows = conn.execute(
            f"""
            SELECT vault_address, tx_hash
            FROM strategy_reports
            WHERE chain_id=? AND ({clauses})
            """,
            params,
        ).fetchall()
        found.update((Web3.to_checksum_address(row["vault_address"]), row["tx_hash"]) for row in rows)
    return found


def _index_v2_share_transfer_flow_batch(
    conn,
    chain: str,
    vaults: list[Any],
    to_block: int | None,
    chunk_size: int,
    progress: ProgressCallback | None = None,
) -> int:
    cfg = CHAINS[chain]
    w3 = web3_for(chain)
    final_block = int(to_block) if to_block is not None else latest_block(chain)
    event_abi = erc20_transfer_event_abi()
    transfer_topic = erc20_transfer_topic()

    by_address: dict[str, Any] = {Web3.to_checksum_address(v["address"]): v for v in vaults}
    start_by_address: dict[str, int] = {}
    for address, vault in by_address.items():
        deployment_block = int(vault["deployment_block"] or 0)
        last_done = get_index_state(conn, cfg.chain_id, address, "VaultShareTransfer")
        start_by_address[address] = max(deployment_block, (last_done + 1) if last_done is not None else deployment_block)

    pending_starts = [start for start in start_by_address.values() if start <= final_block]
    if not pending_starts:
        return 0

    inserted = 0
    current = min(pending_starts)
    addresses = list(by_address)
    while current <= final_block:
        end = min(current + chunk_size - 1, final_block)
        active = [address for address in addresses if start_by_address[address] <= end]
        if not active:
            current = end + 1
            continue
        if progress:
            progress(f"{chain} v2: scanning share mints/burns {current}-{end} across {len(active)} vaults")
        logs: list[dict[str, Any]] = []
        for direction, topics in (
            ("deposit", [transfer_topic, ZERO_TOPIC]),
            ("withdraw", [transfer_topic, None, ZERO_TOPIC]),
        ):
            _ = direction
            logs.extend(
                _get_logs_with_split(
                    w3,
                    {
                        "fromBlock": current,
                        "toBlock": end,
                        "address": active,
                        "topics": topics,
                    },
                )
            )
        logs.sort(key=lambda log: (int(log["blockNumber"]), int(log["transactionIndex"]), int(log["logIndex"])))
        ts_cache = cached_block_timestamps_many(conn, chain, [int(log["blockNumber"]) for log in logs])
        share_price_cache = _cached_vault_share_prices_many(conn, chain, by_address, logs)
        report_txs = _strategy_report_txs_for_logs(conn, cfg.chain_id, logs)
        for log in logs:
            target = Web3.to_checksum_address(log["address"])
            if int(log["blockNumber"]) < start_by_address[target]:
                continue
            tx_hash = _hex(log["transactionHash"])
            if (target, tx_hash) in report_txs:
                continue
            decoded = w3.codec.decode(["uint256"], bytes(log["data"]))
            args = {
                "from": Web3.to_checksum_address("0x" + Web3.to_hex(log["topics"][1])[-40:]),
                "to": Web3.to_checksum_address("0x" + Web3.to_hex(log["topics"][2])[-40:]),
                "value": int(decoded[0]),
            }
            if args["from"] != Web3.to_checksum_address(ZERO_ADDRESS) and args["to"] != Web3.to_checksum_address(ZERO_ADDRESS):
                continue
            block_number = int(log["blockNumber"])
            vault = by_address[target]
            block_ts = ts_cache[block_number]
            price_per_share, share_decimals = share_price_cache[(target, block_number)]
            insert_raw_event(conn, cfg.chain_id, target, event_abi["name"], log, block_ts, args)
            row = normalize_v2_share_transfer_flow(
                cfg.chain_id,
                target,
                vault["asset"],
                vault["asset_decimals"],
                log,
                block_ts,
                args,
                price_per_share,
                share_decimals,
            )
            inserted += insert_vault_flow(conn, row)
        _set_index_state_many(conn, cfg.chain_id, active, "VaultShareTransfer", end)
        current = end + 1
    conn.commit()
    return inserted


def _index_vault_batch(
    conn,
    chain: str,
    version: str,
    vaults: list[Any],
    to_block: int | None,
    chunk_size: int,
    progress: ProgressCallback | None = None,
) -> int:
    cfg = CHAINS[chain]
    w3 = web3_for(chain)
    final_block = int(to_block) if to_block is not None else latest_block(chain)
    event_abis = strategy_report_event_abis(version)
    topics = strategy_report_topics(version)

    by_address: dict[str, Any] = {Web3.to_checksum_address(v["address"]): v for v in vaults}
    start_by_address: dict[str, int] = {}
    for address, vault in by_address.items():
        deployment_block = int(vault["deployment_block"] or 0)
        last_done = get_index_state(conn, cfg.chain_id, address, "StrategyReported")
        start_by_address[address] = max(deployment_block, (last_done + 1) if last_done is not None else deployment_block)

    pending_starts = [start for start in start_by_address.values() if start <= final_block]
    if not pending_starts:
        return 0

    inserted = 0
    current = min(pending_starts)
    addresses = list(by_address)
    while current <= final_block:
        end = min(current + chunk_size - 1, final_block)
        active = [address for address in addresses if start_by_address[address] <= end]
        if not active:
            current = end + 1
            continue
        if progress:
            progress(f"{chain} {version}: scanning {current}-{end} across {len(active)} vaults")
        logs = _get_logs_with_split(
            w3,
            {
                "fromBlock": current,
                "toBlock": end,
                "address": active,
                "topics": [topics],
            },
        )
        ts_cache = cached_block_timestamps_many(conn, chain, [int(log["blockNumber"]) for log in logs])
        for log in logs:
            target = Web3.to_checksum_address(log["address"])
            if int(log["blockNumber"]) < start_by_address[target]:
                continue
            event_abi, args = decode_event(chain, event_abis, log)
            _ = event_abi
            block_number = int(log["blockNumber"])
            vault = by_address[target]
            block_ts = ts_cache[block_number]
            insert_raw_event(conn, cfg.chain_id, target, "StrategyReported", log, block_ts, args)
            row = normalize_strategy_report(
                cfg.chain_id,
                version,
                target,
                vault["asset"],
                vault["asset_decimals"],
                log,
                block_ts,
                args,
            )
            insert_strategy_report(conn, row)
            inserted += 1
        _set_index_state_many(conn, cfg.chain_id, active, "StrategyReported", end)
        current = end + 1
    conn.commit()
    return inserted


def _chunks(rows: list[Any], size: int) -> list[list[Any]]:
    return [rows[i : i + size] for i in range(0, len(rows), size)]


def _index_vault_flow_batch(
    conn,
    chain: str,
    version: str,
    vaults: list[Any],
    to_block: int | None,
    chunk_size: int,
    progress: ProgressCallback | None = None,
) -> int:
    cfg = CHAINS[chain]
    w3 = web3_for(chain)
    final_block = int(to_block) if to_block is not None else latest_block(chain)
    event_abis = vault_flow_event_abis(version)
    topics = vault_flow_topics(version)

    by_address: dict[str, Any] = {Web3.to_checksum_address(v["address"]): v for v in vaults}
    start_by_address: dict[str, int] = {}
    for address, vault in by_address.items():
        deployment_block = int(vault["deployment_block"] or 0)
        last_done = get_index_state(conn, cfg.chain_id, address, "VaultFlow")
        start_by_address[address] = max(deployment_block, (last_done + 1) if last_done is not None else deployment_block)

    pending_starts = [start for start in start_by_address.values() if start <= final_block]
    if not pending_starts:
        return 0

    inserted = 0
    current = min(pending_starts)
    addresses = list(by_address)
    while current <= final_block:
        end = min(current + chunk_size - 1, final_block)
        active = [address for address in addresses if start_by_address[address] <= end]
        if not active:
            current = end + 1
            continue
        if progress:
            progress(f"{chain} {version}: scanning vault flows {current}-{end} across {len(active)} vaults")
        logs = _get_logs_with_split(
            w3,
            {
                "fromBlock": current,
                "toBlock": end,
                "address": active,
                "topics": [topics],
            },
        )
        ts_cache = cached_block_timestamps_many(conn, chain, [int(log["blockNumber"]) for log in logs])
        for log in logs:
            target = Web3.to_checksum_address(log["address"])
            if int(log["blockNumber"]) < start_by_address[target]:
                continue
            event_abi, args = decode_event(chain, event_abis, log)
            block_number = int(log["blockNumber"])
            vault = by_address[target]
            block_ts = ts_cache[block_number]
            insert_raw_event(conn, cfg.chain_id, target, event_abi["name"], log, block_ts, args)
            row = normalize_vault_flow(
                cfg.chain_id,
                version,
                target,
                vault["asset"],
                vault["asset_decimals"],
                log,
                block_ts,
                event_abi["name"],
                args,
            )
            inserted += insert_vault_flow(conn, row)
        _set_index_state_many(conn, cfg.chain_id, active, "VaultFlow", end)
        current = end + 1
    conn.commit()
    return inserted


def _index_v3_debt_flow_batch(
    conn,
    chain: str,
    vaults: list[Any],
    to_block: int | None,
    chunk_size: int,
    progress: ProgressCallback | None = None,
) -> int:
    cfg = CHAINS[chain]
    w3 = web3_for(chain)
    final_block = int(to_block) if to_block is not None else latest_block(chain)
    event_abis = debt_updated_event_abis("v3")
    topics = debt_updated_topics("v3")

    by_address: dict[str, Any] = {Web3.to_checksum_address(v["address"]): v for v in vaults}
    start_by_address: dict[str, int] = {}
    for address, vault in by_address.items():
        deployment_block = int(vault["deployment_block"] or 0)
        last_done = get_index_state(conn, cfg.chain_id, address, "DebtUpdated")
        start_by_address[address] = max(deployment_block, (last_done + 1) if last_done is not None else deployment_block)

    pending_starts = [start for start in start_by_address.values() if start <= final_block]
    if not pending_starts:
        return 0

    inserted = 0
    current = min(pending_starts)
    addresses = list(by_address)
    while current <= final_block:
        end = min(current + chunk_size - 1, final_block)
        active = [address for address in addresses if start_by_address[address] <= end]
        if not active:
            current = end + 1
            continue
        if progress:
            progress(f"{chain} v3: scanning debt updates {current}-{end} across {len(active)} vaults")
        logs = _get_logs_with_split(
            w3,
            {
                "fromBlock": current,
                "toBlock": end,
                "address": active,
                "topics": [topics],
            },
        )
        ts_cache = cached_block_timestamps_many(conn, chain, [int(log["blockNumber"]) for log in logs])
        for log in logs:
            target = Web3.to_checksum_address(log["address"])
            if int(log["blockNumber"]) < start_by_address[target]:
                continue
            event_abi, args = decode_event(chain, event_abis, log)
            block_number = int(log["blockNumber"])
            vault = by_address[target]
            block_ts = ts_cache[block_number]
            insert_raw_event(conn, cfg.chain_id, target, event_abi["name"], log, block_ts, args)
            row = normalize_v3_debt_flow(
                cfg.chain_id,
                "v3",
                target,
                vault["asset"],
                vault["asset_decimals"],
                log,
                block_ts,
                args,
            )
            if row is not None:
                inserted += insert_strategy_debt_flow(conn, row)
        _set_index_state_many(conn, cfg.chain_id, active, "DebtUpdated", end)
        current = end + 1
    conn.commit()
    return inserted


def index_v2_debt_flows_from_reports(conn, chains: list[str] | None = None) -> int:
    params: list[Any] = []
    where = "WHERE r.version='v2'"
    if chains:
        chain_ids = [CHAINS[c].chain_id for c in chains]
        where += f" AND r.chain_id IN ({','.join('?' for _ in chain_ids)})"
        params.extend(chain_ids)
    rows = conn.execute(
        f"""
        SELECT r.*, v.asset_symbol
        FROM strategy_reports r
        LEFT JOIN vaults v
          ON v.chain_id = r.chain_id AND v.address = r.vault_address
        {where}
        """,
        params,
    ).fetchall()
    inserted = 0
    for row in rows:
        extra = json.loads(row["extra_json"] or "{}")
        values = [
            ("allocation", int(extra.get("debtAdded") or 0)),
            ("deallocation", int(extra.get("debtPaid") or 0)),
        ]
        for direction, amount in values:
            if amount <= 0:
                continue
            inserted += insert_strategy_debt_flow(
                conn,
                (
                    int(row["chain_id"]),
                    row["version"],
                    Web3.to_checksum_address(row["vault_address"]),
                    Web3.to_checksum_address(row["strategy_address"]),
                    direction,
                    row["tx_hash"],
                    int(row["log_index"]),
                    int(row["block_number"]),
                    int(row["block_timestamp"]),
                    Web3.to_checksum_address(row["asset"]) if row["asset"] else None,
                    row["asset_decimals"],
                    str(amount),
                    row["current_debt_raw"],
                    row["current_debt_raw"],
                    "StrategyReported",
                    row["extra_json"],
                ),
            )
    conn.commit()
    return inserted


def _receipt_with_retry(w3, tx_hash: str, attempts: int = 5):
    for attempt in range(attempts):
        try:
            return w3.eth.get_transaction_receipt(tx_hash)
        except Exception:
            if attempt == attempts - 1:
                raise
            time.sleep(2 ** attempt)


def _topic_hex(topic: Any) -> str:
    text = topic.hex() if hasattr(topic, "hex") else str(topic)
    return text if text.startswith("0x") else f"0x{text}"


def _address_from_topic(topic: Any) -> str:
    text = _topic_hex(topic)
    return Web3.to_checksum_address("0x" + text[-40:])


def _decode_transfer_value(log: Any) -> int:
    data = log["data"]
    if isinstance(data, bytes):
        raw = data
    else:
        text = data.hex() if hasattr(data, "hex") else str(data)
        raw = bytes.fromhex(text[2:] if text.startswith("0x") else text)
    return int(abi_decode(["uint256"], raw)[0])


def index_v2_fee_mints_from_reports(
    conn,
    chains: list[str] | None = None,
    progress: ProgressCallback | None = None,
) -> int:
    """Collect V2 harvest fee share mints from report transaction receipts."""

    selected = chains or ["eth"]
    total_inserted = 0
    transfer_topic = erc20_transfer_topic().lower()
    zero_topic = ZERO_TOPIC.lower()
    chain_by_id = {cfg.chain_id: cfg.key for cfg in CHAINS.values()}

    for chain in selected:
        cfg = CHAINS[chain]
        if cfg.key != "eth":
            continue
        last_done = get_index_state(conn, cfg.chain_id, "v2-fee-mints", "Transfer")
        params: list[Any] = [cfg.chain_id]
        block_filter = ""
        if last_done is not None:
            block_filter = "AND block_number > ?"
            params.append(int(last_done))
        report_rows = conn.execute(
            f"""
            SELECT
                chain_id,
                tx_hash,
                vault_address,
                MIN(strategy_address) AS strategy_address,
                COUNT(DISTINCT strategy_address) AS strategy_count,
                MIN(block_number) AS block_number,
                MIN(block_timestamp) AS block_timestamp,
                MIN(asset) AS asset,
                MIN(asset_decimals) AS asset_decimals
            FROM strategy_reports
            WHERE version='v2'
              AND chain_id=?
              {block_filter}
            GROUP BY chain_id, tx_hash, vault_address
            ORDER BY block_number, tx_hash, vault_address
            """,
            params,
        ).fetchall()
        if not report_rows:
            continue

        w3 = web3_for(chain_by_id[cfg.chain_id])
        by_vault = {
            Web3.to_checksum_address(row["address"]): row
            for row in conn.execute("SELECT * FROM vaults WHERE chain_id=? AND version='v2'", (cfg.chain_id,))
        }
        candidates: list[tuple[Any, Any, int, str, str]] = []
        processed_block = int(report_rows[0]["block_number"])
        for idx, row in enumerate(report_rows, start=1):
            vault = Web3.to_checksum_address(row["vault_address"])
            receipt = _receipt_with_retry(w3, row["tx_hash"])
            vault_transfer_out: list[tuple[Any, int, str, str]] = []
            fee_mint_logs: list[tuple[Any, int, str, str]] = []
            for log in receipt["logs"]:
                if Web3.to_checksum_address(log["address"]) != vault:
                    continue
                if not log["topics"] or _topic_hex(log["topics"][0]).lower() != transfer_topic:
                    continue
                if len(log["topics"]) < 3:
                    continue
                shares = _decode_transfer_value(log)
                if shares <= 0:
                    continue
                from_address = _address_from_topic(log["topics"][1])
                to_address = _address_from_topic(log["topics"][2])
                if _topic_hex(log["topics"][1]).lower() == zero_topic and to_address == vault:
                    fee_mint_logs.append((log, shares, to_address, "v2_harvest_fee_mint_unsplit"))
                elif from_address == vault and to_address != Web3.to_checksum_address(ZERO_ADDRESS):
                    vault_transfer_out.append((log, shares, to_address, "v2_harvest_fee_transfer"))
            if fee_mint_logs:
                if vault_transfer_out:
                    candidates.extend((row, log, shares, recipient, source) for log, shares, recipient, source in vault_transfer_out)
                else:
                    candidates.extend((row, log, shares, recipient, source) for log, shares, recipient, source in fee_mint_logs)
            processed_block = int(row["block_number"])
            if progress and (idx == 1 or idx % 500 == 0 or idx == len(report_rows)):
                progress(f"{chain} v2 fees: fetched {idx}/{len(report_rows)} report receipts")

        pps_cache = _cached_vault_share_prices_many(
            conn,
            chain,
            by_vault,
            [{"address": log["address"], "blockNumber": int(log["blockNumber"])} for _, log, _, _, _ in candidates],
        )
        for row, log, shares, recipient, source in candidates:
            vault = Web3.to_checksum_address(row["vault_address"])
            block_number = int(log["blockNumber"])
            price_per_share, share_decimals = pps_cache[(vault, block_number)]
            assets = shares * int(price_per_share) // (10 ** int(share_decimals))
            total_inserted += insert_vault_fee_event(
                conn,
                (
                    cfg.chain_id,
                    "v2",
                    source,
                    vault,
                    Web3.to_checksum_address(row["strategy_address"]) if int(row["strategy_count"] or 0) == 1 else None,
                    recipient,
                    _hex(log["transactionHash"]),
                    int(log["logIndex"]),
                    block_number,
                    int(row["block_timestamp"]),
                    Web3.to_checksum_address(row["asset"]) if row["asset"] else None,
                    row["asset_decimals"],
                    str(int(assets)),
                    str(int(shares)),
                    None,
                    None,
                    None,
                    json.dumps(
                        {
                            "source_event": "Transfer",
                            "fee_source": source,
                            "from": vault if source == "v2_harvest_fee_transfer" else ZERO_ADDRESS,
                            "to": recipient,
                            "value": str(int(shares)),
                            "price_per_share_raw": str(int(price_per_share)),
                            "share_decimals": int(share_decimals),
                        },
                        sort_keys=True,
                    ),
                ),
            )
        set_index_state(conn, cfg.chain_id, "v2-fee-mints", "Transfer", processed_block)
    conn.commit()
    return total_inserted


def index_all_volume(
    conn,
    chains: list[str] | None = None,
    versions: list[str] | None = None,
    to_block: int | None = None,
    chunk_size: int = 50_000,
    address_batch_size: int = 100,
    progress: ProgressCallback | None = None,
) -> int:
    params: list[Any] = []
    where_parts: list[str] = []
    if chains:
        chain_ids = [CHAINS[c].chain_id for c in chains]
        where_parts.append(f"chain_id IN ({','.join('?' for _ in chain_ids)})")
        params.extend(chain_ids)
    if versions:
        where_parts.append(f"version IN ({','.join('?' for _ in versions)})")
        params.extend(versions)
    where = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""
    rows = conn.execute(f"SELECT * FROM vaults {where} ORDER BY chain_id, version, address", params).fetchall()
    total = 0
    by_group: dict[tuple[str, str], list[Any]] = {}
    chain_by_id = {cfg.chain_id: cfg.key for cfg in CHAINS.values()}
    for vault in rows:
        by_group.setdefault((chain_by_id[int(vault["chain_id"])], vault["version"]), []).append(vault)
    for (chain, version), vaults in sorted(by_group.items()):
        for batch in _chunks(vaults, address_batch_size):
            if version == "v2":
                total += _index_v2_share_transfer_flow_batch(
                    conn,
                    chain,
                    batch,
                    to_block=to_block,
                    chunk_size=chunk_size,
                    progress=progress,
                )
            else:
                total += _index_vault_flow_batch(
                    conn,
                    chain,
                    version,
                    batch,
                    to_block=to_block,
                    chunk_size=chunk_size,
                    progress=progress,
                )
                total += _index_v3_debt_flow_batch(
                    conn,
                    chain,
                    batch,
                    to_block=to_block,
                    chunk_size=chunk_size,
                    progress=progress,
                )
    if not versions or "v2" in versions:
        total += index_v2_debt_flows_from_reports(conn, chains=chains)
    return total


def index_all_reports(
    conn,
    chains: list[str] | None = None,
    versions: list[str] | None = None,
    to_block: int | None = None,
    chunk_size: int = 50_000,
    address_batch_size: int = 100,
    progress: ProgressCallback | None = None,
) -> int:
    params: list[Any] = []
    where_parts: list[str] = []
    if chains:
        chain_ids = [CHAINS[c].chain_id for c in chains]
        where_parts.append(f"chain_id IN ({','.join('?' for _ in chain_ids)})")
        params.extend(chain_ids)
    if versions:
        where_parts.append(f"version IN ({','.join('?' for _ in versions)})")
        params.extend(versions)
    where = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""
    rows = conn.execute(f"SELECT * FROM vaults {where} ORDER BY chain_id, version, address", params).fetchall()
    total = 0
    by_group: dict[tuple[str, str], list[Any]] = {}
    chain_by_id = {cfg.chain_id: cfg.key for cfg in CHAINS.values()}
    for vault in rows:
        by_group.setdefault((chain_by_id[int(vault["chain_id"])], vault["version"]), []).append(vault)
    for (chain, version), vaults in sorted(by_group.items()):
        for batch in _chunks(vaults, address_batch_size):
            total += _index_vault_batch(
                conn,
                chain,
                version,
                batch,
                to_block=to_block,
                chunk_size=chunk_size,
                progress=progress,
            )
    return total
