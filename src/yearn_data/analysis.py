"""Analysis jobs built on top of normalized indexed data."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from .incident_adjustments import adjustment_for_tx
from .pricing import (
    DEFAULT_FALLBACK_PRICE_SOURCE,
    DEFAULT_PRICE_SOURCE,
    SUPPORTED_SOURCES,
    report_amount,
)
from .storage import from_json, to_json


def closed_cutoff(before_timestamp=None, now=None):
    today = int(time.time() if now is None else now) // 86400 * 86400
    cutoff = today if before_timestamp is None else int(before_timestamp)
    if cutoff <= 0 or cutoff % 86400 or cutoff > today:
        raise ValueError('before_timestamp must be a closed UTC midnight, no later than today')
    return cutoff


def _usd(raw_value: str, decimals: int | None, price: float | None) -> Decimal | None:
    if int(raw_value) == 0:
        return Decimal(0)
    if price is None:
        return None
    return report_amount(raw_value, decimals) * Decimal(str(price))


def create_analysis_run(conn, name: str, params: dict[str, Any] | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO analysis_runs (name, started_at, status, params_json) VALUES (?, ?, 'running', ?) RETURNING id",
        (name, int(time.time()), to_json(params or {})),
    )
    run_id = int(cur.fetchone()[0])
    conn.commit()
    return run_id


def complete_analysis_run(conn, run_id: int, status: str = "complete") -> None:
    conn.execute(
        "UPDATE analysis_runs SET completed_at=?, status=? WHERE id=?",
        (int(time.time()), status, run_id),
    )
    conn.commit()


def write_output(conn, run_id: int, name: str, row: dict[str, Any]) -> None:
    conn.execute(
        "INSERT INTO analysis_outputs (run_id, name, row_json) VALUES (?, ?, ?)",
        (run_id, name, to_json(row)),
    )


def _decimal_or_none(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value, "f")


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    try:
        value = row[key]
    except (KeyError, IndexError):
        return default
    return default if value is None else value


def _price_provenance(raw_json: str | None) -> dict[str, Any]:
    if not raw_json:
        return {}
    try:
        payload = from_json(raw_json)
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def run_lifetime_yield(
    conn,
    price_source: str = DEFAULT_PRICE_SOURCE,
    fallback_price_source: str | None = DEFAULT_FALLBACK_PRICE_SOURCE,
    *,
    before_timestamp: int | None = None,
) -> int:
    if before_timestamp is not None:
        before_timestamp = closed_cutoff(before_timestamp)
    if price_source not in SUPPORTED_SOURCES:
        raise ValueError(f"unsupported price source {price_source!r}")
    if fallback_price_source is not None and fallback_price_source not in SUPPORTED_SOURCES:
        raise ValueError(f"unsupported fallback price source {fallback_price_source!r}")
    price_sources = [price_source]
    if fallback_price_source is not None and fallback_price_source != price_source:
        price_sources.append(fallback_price_source)
    source_placeholders = ",".join("?" for _ in price_sources)
    priority = " ".join(
        f"WHEN ? THEN {position}" for position, _ in enumerate(price_sources)
    )
    run_id = create_analysis_run(
        conn,
        "lifetime-yield",
        {
            "price_source": price_source,
            "fallback_price_source": fallback_price_source,
            "before_timestamp": before_timestamp,
        },
    )
    rows = conn.execute(
        f"""
        SELECT
            r.*,
            v.asset_symbol,
            v.name AS vault_name,
            v.management,
            v.protocol,
            p.price_usd,
            p.source AS selected_price_source,
            p.status AS price_status,
            p.raw_json AS price_raw_json
        FROM strategy_reports r
        LEFT JOIN vaults v
          ON v.chain_id = r.chain_id AND v.address = r.vault_address
        LEFT JOIN prices p
         ON p.chain_id = r.chain_id
         AND p.token_address = r.asset
         AND p.timestamp = r.block_timestamp
         AND p.source = (
            SELECT p2.source
            FROM prices p2
            WHERE p2.chain_id = r.chain_id
              AND p2.token_address = r.asset
              AND p2.timestamp = r.block_timestamp
              AND p2.source IN ({source_placeholders})
            ORDER BY
                CASE WHEN p2.status = 'ok' THEN 0 ELSE 1 END,
                CASE p2.source {priority} ELSE {len(price_sources)} END
            LIMIT 1
         )
        WHERE (CAST(? AS BIGINT) IS NULL OR r.block_timestamp < ?)
        """,
        (*price_sources, *price_sources, before_timestamp, before_timestamp),
    ).fetchall()

    totals: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        price_provenance = _price_provenance(row["price_raw_json"])
        price = row["price_usd"]
        decimals = row["asset_decimals"]
        raw_gain_usd = _usd(row["gain_raw"], decimals, price)
        raw_loss_usd = _usd(row["loss_raw"], decimals, price)
        raw_net_usd = _usd(row["net_raw"], decimals, price)
        adjustment = adjustment_for_tx(row["tx_hash"])
        adjusted_gain_raw = adjustment.adjusted_gain_raw if adjustment else row["gain_raw"]
        adjusted_loss_raw = adjustment.adjusted_loss_raw if adjustment else row["loss_raw"]
        adjusted_net_raw = adjustment.adjusted_net_raw if adjustment else row["net_raw"]
        gain_usd = _usd(adjusted_gain_raw, decimals, price)
        loss_usd = _usd(adjusted_loss_raw, decimals, price)
        net_usd = _usd(adjusted_net_raw, decimals, price)
        priced = net_usd is not None
        valuation_status = (
            "priced"
            if price is not None
            else "zero_amount_no_price_required"
            if int(row["gain_raw"]) == 0 and int(row["loss_raw"]) == 0
            else "missing_price"
        )
        report_row = {
            "chain_id": row["chain_id"],
            "version": row["version"],
            "vault_address": row["vault_address"],
            "vault_name": row["vault_name"],
            "management": row["management"] or "yearn",
            "protocol": row["protocol"],
            "strategy_address": row["strategy_address"],
            "tx_hash": row["tx_hash"],
            "log_index": row["log_index"],
            "block_number": row["block_number"],
            "block_timestamp": row["block_timestamp"],
            "asset": row["asset"],
            "asset_symbol": row["asset_symbol"],
            "gain_raw": row["gain_raw"],
            "loss_raw": row["loss_raw"],
            "net_raw": row["net_raw"],
            "adjusted_gain_raw": adjusted_gain_raw,
            "adjusted_loss_raw": adjusted_loss_raw,
            "adjusted_net_raw": adjusted_net_raw,
            "price_usd": price,
            "price_source": row["selected_price_source"],
            "primary_price_source": price_source,
            "fallback_price_source": fallback_price_source,
            "price_status": row["price_status"] or "missing",
            "price_evidence_timestamp": price_provenance.get("normalized_timestamp"),
            "price_upstream_source": price_provenance.get("upstream_source"),
            "price_confidence": price_provenance.get("confidence"),
            "price_adapter": price_provenance.get("adapter"),
            "valuation_status": valuation_status,
            "raw_gross_gain_usd": _decimal_or_none(raw_gain_usd),
            "raw_loss_usd": _decimal_or_none(raw_loss_usd),
            "raw_net_yield_usd": _decimal_or_none(raw_net_usd),
            "gross_gain_usd": _decimal_or_none(gain_usd),
            "loss_usd": _decimal_or_none(loss_usd),
            "net_yield_usd": _decimal_or_none(net_usd),
            "is_adjusted": bool(adjustment),
            "incident_id": adjustment.incident_id if adjustment else None,
            "incident_classification": adjustment.classification if adjustment else None,
            "incident_description": adjustment.description if adjustment else None,
            "incident_disclosure_url": adjustment.disclosure_url if adjustment else None,
        }
        write_output(conn, run_id, "reports", report_row)

        dimensions = {
            "total_yield_summary": ("all", "all"),
            "yield_by_chain": ("chain", str(row["chain_id"])),
            "yield_by_vault": ("vault", f"{row['chain_id']}:{row['vault_address']}"),
            "yield_by_strategy": ("strategy", f"{row['chain_id']}:{row['strategy_address']}"),
            "yield_by_management": ("management", row["management"] or "yearn"),
        }
        if row["protocol"]:
            dimensions["yield_by_protocol"] = ("protocol", row["protocol"])
        for output_name, key in dimensions.items():
            bucket = totals.setdefault(
                (output_name, key[1]),
                {
                    "dimension": key[0],
                    "key": key[1],
                    "chain_id": row["chain_id"] if key[0] in {"chain", "vault", "strategy"} else None,
                    "vault_address": row["vault_address"] if key[0] == "vault" else None,
                    "strategy_address": row["strategy_address"] if key[0] == "strategy" else None,
                    "management": row["management"] if key[0] in {"management", "protocol", "vault", "strategy"} else None,
                    "protocol": row["protocol"] if key[0] == "protocol" else None,
                    "price_source": price_source,
                    "fallback_price_source": fallback_price_source,
                    "reports": 0,
                    "priced_reports": 0,
                    "unpriced_reports": 0,
                    "gross_gain_usd": Decimal(0),
                    "loss_usd": Decimal(0),
                    "net_yield_usd": Decimal(0),
                    "raw_gross_gain_usd": Decimal(0),
                    "raw_loss_usd": Decimal(0),
                    "raw_net_yield_usd": Decimal(0),
                    "adjusted_reports": 0,
                },
            )
            bucket["reports"] += 1
            if adjustment:
                bucket["adjusted_reports"] += 1
            if priced:
                bucket["priced_reports"] += 1
                bucket["raw_gross_gain_usd"] += raw_gain_usd or Decimal(0)
                bucket["raw_loss_usd"] += raw_loss_usd or Decimal(0)
                bucket["raw_net_yield_usd"] += raw_net_usd or Decimal(0)
                bucket["gross_gain_usd"] += gain_usd or Decimal(0)
                bucket["loss_usd"] += loss_usd or Decimal(0)
                bucket["net_yield_usd"] += net_usd or Decimal(0)
            else:
                bucket["unpriced_reports"] += 1

    for (output_name, _), row in totals.items():
        serialized = dict(row)
        for key in (
            "gross_gain_usd",
            "loss_usd",
            "net_yield_usd",
            "raw_gross_gain_usd",
            "raw_loss_usd",
            "raw_net_yield_usd",
        ):
            serialized[key] = _decimal_or_none(serialized[key])
        write_output(conn, run_id, output_name, serialized)
    conn.commit()
    complete_analysis_run(conn, run_id)
    return run_id


def _selected_report_price_join(table_alias: str) -> str:
    return f"""
        LEFT JOIN prices p
          ON p.chain_id = {table_alias}.chain_id
         AND p.token_address = {table_alias}.asset
         AND p.timestamp = {table_alias}.block_timestamp
         AND p.source = (
            SELECT p2.source
            FROM prices p2
            WHERE p2.chain_id = {table_alias}.chain_id
              AND p2.token_address = {table_alias}.asset
              AND p2.timestamp = {table_alias}.block_timestamp
              AND p2.status = 'ok'
            ORDER BY CASE p2.source WHEN 'defillama' THEN 0 ELSE 1 END
            LIMIT 1
         )
    """


def _empty_fee_bucket(dimension: str, key: str, row: dict[str, Any]) -> dict[str, Any]:
    return {
        "dimension": dimension,
        "key": key,
        "chain_id": row["chain_id"] if dimension in {"chain", "vault", "strategy", "recipient"} else None,
        "version": row["version"] if dimension in {"version", "vault", "strategy", "recipient"} else None,
        "vault_address": row["vault_address"] if dimension == "vault" else None,
        "strategy_address": row["strategy_address"] if dimension == "strategy" else None,
        "recipient": row["recipient"] if dimension == "recipient" else None,
        "management": row["management"] if dimension in {"management", "protocol", "vault", "strategy", "recipient"} else None,
        "protocol": row["protocol"] if dimension == "protocol" else None,
        "events": 0,
        "priced_events": 0,
        "unpriced_events": 0,
        "v2_fee_mint_raw": Decimal(0),
        "v2_fee_mint_usd": Decimal(0),
        "v3_protocol_fees_raw": Decimal(0),
        "v3_protocol_fees_usd": Decimal(0),
        "v3_total_fees_raw": Decimal(0),
        "v3_total_fees_usd": Decimal(0),
        "v3_total_refunds_raw": Decimal(0),
        "v3_total_refunds_usd": Decimal(0),
        "total_fees_usd": Decimal(0),
    }


def _fee_dimensions(row: dict[str, Any]) -> dict[str, tuple[str, str]]:
    dimensions = {
        "fee_summary": ("all", "all"),
        "fees_by_chain": ("chain", str(row["chain_id"])),
        "fees_by_version": ("version", row["version"]),
        "fees_by_vault": ("vault", f"{row['chain_id']}:{row['vault_address']}"),
        "fees_by_management": ("management", row["management"] or "yearn"),
    }
    if _row_get(row, "protocol"):
        dimensions["fees_by_protocol"] = ("protocol", row["protocol"])
    if _row_get(row, "strategy_address"):
        dimensions["fees_by_strategy"] = ("strategy", f"{row['chain_id']}:{row['strategy_address']}")
    if _row_get(row, "recipient"):
        dimensions["fees_by_recipient"] = ("recipient", f"{row['chain_id']}:{row['recipient']}")
    return dimensions


def _add_fee_to_totals(totals: dict[tuple[str, str], dict[str, Any]], row: dict[str, Any]) -> None:
    for output_name, key in _fee_dimensions(row).items():
        bucket = totals.setdefault((output_name, key[1]), _empty_fee_bucket(key[0], key[1], row))
        bucket["events"] += 1
        if row["priced"]:
            bucket["priced_events"] += 1
        else:
            bucket["unpriced_events"] += 1
        for field in (
            "v2_fee_mint_raw",
            "v2_fee_mint_usd",
            "v3_protocol_fees_raw",
            "v3_protocol_fees_usd",
            "v3_total_fees_raw",
            "v3_total_fees_usd",
            "v3_total_refunds_raw",
            "v3_total_refunds_usd",
            "total_fees_usd",
        ):
            bucket[field] += row[field] or Decimal(0)


def _fee_row_output(row: dict[str, Any]) -> dict[str, Any]:
    output = dict(row)
    output.pop("priced")
    for key in (
        "v2_fee_mint_raw",
        "v2_fee_mint_usd",
        "v3_protocol_fees_raw",
        "v3_protocol_fees_usd",
        "v3_total_fees_raw",
        "v3_total_fees_usd",
        "v3_total_refunds_raw",
        "v3_total_refunds_usd",
        "total_fees_usd",
    ):
        output[key] = _decimal_or_none(output[key])
    return output


def _write_fee_totals(conn, run_id: int, totals: dict[tuple[str, str], dict[str, Any]]) -> None:
    for (output_name, _), row in totals.items():
        serialized = dict(row)
        for key in (
            "v2_fee_mint_raw",
            "v2_fee_mint_usd",
            "v3_protocol_fees_raw",
            "v3_protocol_fees_usd",
            "v3_total_fees_raw",
            "v3_total_fees_usd",
            "v3_total_refunds_raw",
            "v3_total_refunds_usd",
            "total_fees_usd",
        ):
            serialized[key] = _decimal_or_none(serialized[key])
        write_output(conn, run_id, output_name, serialized)


def run_vault_fees(conn) -> int:
    """Analyze fees charged during report/harvest transactions.

    V3 exposes fee amounts directly on StrategyReported. V2 does not emit fee
    fields on StrategyReported, so V2 fees are inferred from vault share mints
    in the same transaction as a V2 StrategyReported event.
    """

    run_id = create_analysis_run(conn, "vault-fees")
    totals: dict[tuple[str, str], dict[str, Any]] = {}

    v3_rows = conn.execute(
        f"""
        SELECT
            r.*,
            v.asset_symbol,
            v.name AS vault_name,
            v.management,
            v.protocol,
            p.price_usd,
            p.status AS price_status
        FROM strategy_reports r
        LEFT JOIN vaults v
          ON v.chain_id = r.chain_id AND v.address = r.vault_address
        {_selected_report_price_join("r")}
        WHERE r.version='v3'
          AND (
            CAST(COALESCE(r.protocol_fees_raw, '0') AS INTEGER) > 0
            OR CAST(COALESCE(r.total_fees_raw, '0') AS INTEGER) > 0
            OR CAST(COALESCE(r.total_refunds_raw, '0') AS INTEGER) > 0
          )
        ORDER BY r.chain_id, r.block_number, r.log_index
        """
    ).fetchall()
    for row in v3_rows:
        protocol_raw = Decimal(int(row["protocol_fees_raw"] or 0))
        total_raw = Decimal(int(row["total_fees_raw"] or 0))
        refunds_raw = Decimal(int(row["total_refunds_raw"] or 0))
        protocol_usd = _usd(str(int(protocol_raw)), row["asset_decimals"], row["price_usd"])
        total_usd = _usd(str(int(total_raw)), row["asset_decimals"], row["price_usd"])
        refunds_usd = _usd(str(int(refunds_raw)), row["asset_decimals"], row["price_usd"])
        output = {
            "source": "v3_strategy_report",
            "chain_id": row["chain_id"],
            "version": "v3",
            "vault_address": row["vault_address"],
            "vault_name": row["vault_name"],
            "management": row["management"] or "yearn",
            "protocol": row["protocol"],
            "strategy_address": row["strategy_address"],
            "recipient": None,
            "tx_hash": row["tx_hash"],
            "log_index": row["log_index"],
            "block_number": row["block_number"],
            "block_timestamp": row["block_timestamp"],
            "asset": row["asset"],
            "asset_symbol": row["asset_symbol"],
            "price_usd": row["price_usd"],
            "price_status": row["price_status"] or "missing",
            "priced": total_usd is not None,
            "v2_fee_mint_raw": Decimal(0),
            "v2_fee_mint_usd": Decimal(0),
            "v3_protocol_fees_raw": protocol_raw,
            "v3_protocol_fees_usd": protocol_usd,
            "v3_total_fees_raw": total_raw,
            "v3_total_fees_usd": total_usd,
            "v3_total_refunds_raw": refunds_raw,
            "v3_total_refunds_usd": refunds_usd,
            "total_fees_usd": total_usd,
        }
        write_output(conn, run_id, "fee_events", _fee_row_output(output))
        _add_fee_to_totals(totals, output)

    v2_rows = conn.execute(
        f"""
        SELECT
            fe.*,
            v.asset_symbol,
            v.name AS vault_name,
            v.management,
            v.protocol,
            p.price_usd,
            p.status AS price_status
        FROM vault_fee_events fe
        LEFT JOIN vaults v
          ON v.chain_id = fe.chain_id AND v.address = fe.vault_address
        {_selected_report_price_join("fe")}
        WHERE fe.version='v2'
          AND CAST(fe.fee_raw AS INTEGER) > 0
        ORDER BY fe.chain_id, fe.block_number, fe.log_index
        """
    ).fetchall()
    for row in v2_rows:
        fee_raw = Decimal(int(row["fee_raw"]))
        fee_usd = _usd(row["fee_raw"], row["asset_decimals"], row["price_usd"])
        output = {
            "source": row["source"],
            "chain_id": row["chain_id"],
            "version": "v2",
            "vault_address": row["vault_address"],
            "vault_name": row["vault_name"],
            "management": row["management"] or "yearn",
            "protocol": row["protocol"],
            "strategy_address": row["strategy_address"],
            "recipient": row["recipient"],
            "tx_hash": row["tx_hash"],
            "log_index": row["log_index"],
            "block_number": row["block_number"],
            "block_timestamp": row["block_timestamp"],
            "asset": row["asset"],
            "asset_symbol": row["asset_symbol"],
            "price_usd": row["price_usd"],
            "price_status": row["price_status"] or "missing",
            "priced": fee_usd is not None,
            "v2_fee_mint_raw": fee_raw,
            "v2_fee_mint_usd": fee_usd,
            "v3_protocol_fees_raw": Decimal(0),
            "v3_protocol_fees_usd": Decimal(0),
            "v3_total_fees_raw": Decimal(0),
            "v3_total_fees_usd": Decimal(0),
            "v3_total_refunds_raw": Decimal(0),
            "v3_total_refunds_usd": Decimal(0),
            "total_fees_usd": fee_usd,
        }
        write_output(conn, run_id, "fee_events", _fee_row_output(output))
        _add_fee_to_totals(totals, output)

    _write_fee_totals(conn, run_id, totals)
    conn.commit()
    complete_analysis_run(conn, run_id)
    return run_id


def _selected_price_join(table_alias: str) -> str:
    return f"""
        LEFT JOIN prices p
          ON p.chain_id = {table_alias}.chain_id
         AND p.token_address = {table_alias}.asset
         AND p.timestamp = {table_alias}.block_timestamp
         AND p.source = 'defillama'
         AND p.status = 'ok'
    """


def _month(timestamp: int) -> str:
    return datetime.fromtimestamp(int(timestamp), tz=timezone.utc).strftime("%Y-%m")


def _empty_volume_bucket(dimension: str, key: str, row: Any) -> dict[str, Any]:
    return {
        "dimension": dimension,
        "key": key,
        "chain_id": row["chain_id"] if dimension in {"chain", "vault", "strategy", "token"} else None,
        "version": row["version"] if dimension in {"version", "vault", "strategy"} else None,
        "vault_address": row["vault_address"] if dimension == "vault" else None,
        "strategy_address": row["strategy_address"] if dimension == "strategy" else None,
        "asset": row["asset"] if dimension == "token" else None,
        "asset_symbol": row["asset_symbol"] if dimension == "token" else None,
        "management": row["management"] if dimension in {"management", "protocol", "vault", "strategy", "token"} else None,
        "protocol": row["protocol"] if dimension == "protocol" else None,
        "month": key if dimension == "month" else None,
        "events": 0,
        "priced_events": 0,
        "unpriced_events": 0,
        "deposit_usd": Decimal(0),
        "withdraw_usd": Decimal(0),
        "net_user_flow_usd": Decimal(0),
        "allocation_usd": Decimal(0),
        "deallocation_usd": Decimal(0),
        "net_strategy_flow_usd": Decimal(0),
        "gross_user_volume_usd": Decimal(0),
        "gross_strategy_volume_usd": Decimal(0),
        "gross_total_volume_usd": Decimal(0),
    }


def _volume_dimensions(row: Any) -> dict[str, tuple[str, str]]:
    dimensions = {
        "volume_summary": ("all", "all"),
        "volume_by_chain": ("chain", str(row["chain_id"])),
        "volume_by_vault": ("vault", f"{row['chain_id']}:{row['vault_address']}"),
        "volume_by_token": ("token", f"{row['chain_id']}:{row['asset']}"),
        "volume_by_month": ("month", _month(row["block_timestamp"])),
        "volume_by_management": ("management", row["management"] or "yearn"),
    }
    if _row_get(row, "protocol"):
        dimensions["volume_by_protocol"] = ("protocol", row["protocol"])
    if row["strategy_address"]:
        dimensions["volume_by_strategy"] = ("strategy", f"{row['chain_id']}:{row['strategy_address']}")
    return dimensions


def _add_volume_to_totals(
    totals: dict[tuple[str, str], dict[str, Any]],
    row: Any,
    amount_usd: Decimal | None,
    event_type: str,
) -> None:
    for output_name, key in _volume_dimensions(row).items():
        bucket = totals.setdefault((output_name, key[1]), _empty_volume_bucket(key[0], key[1], row))
        bucket["events"] += 1
        if amount_usd is None:
            bucket["unpriced_events"] += 1
            continue
        bucket["priced_events"] += 1
        if event_type == "deposit":
            bucket["deposit_usd"] += amount_usd
            bucket["net_user_flow_usd"] += amount_usd
            bucket["gross_user_volume_usd"] += amount_usd
        elif event_type == "withdraw":
            bucket["withdraw_usd"] += amount_usd
            bucket["net_user_flow_usd"] -= amount_usd
            bucket["gross_user_volume_usd"] += amount_usd
        elif event_type == "allocation":
            bucket["allocation_usd"] += amount_usd
            bucket["net_strategy_flow_usd"] += amount_usd
            bucket["gross_strategy_volume_usd"] += amount_usd
        elif event_type == "deallocation":
            bucket["deallocation_usd"] += amount_usd
            bucket["net_strategy_flow_usd"] -= amount_usd
            bucket["gross_strategy_volume_usd"] += amount_usd
        bucket["gross_total_volume_usd"] = bucket["gross_user_volume_usd"] + bucket["gross_strategy_volume_usd"]


def run_vault_volume(conn) -> int:
    run_id = create_analysis_run(conn, "vault-volume")
    totals: dict[tuple[str, str], dict[str, Any]] = {}

    vault_rows = conn.execute(
        f"""
        SELECT
            vf.*,
            NULL AS strategy_address,
            v.asset_symbol,
            v.name AS vault_name,
            v.management,
            v.protocol,
            p.price_usd,
            p.status AS price_status
        FROM vault_flows vf
        LEFT JOIN vaults v
          ON v.chain_id = vf.chain_id AND v.address = vf.vault_address
        {_selected_price_join("vf")}
        ORDER BY vf.chain_id, vf.block_number, vf.log_index
        """
    ).fetchall()
    for row in vault_rows:
        amount_usd = _usd(row["assets_raw"], row["asset_decimals"], row["price_usd"])
        output = {
            "chain_id": row["chain_id"],
            "version": row["version"],
            "vault_address": row["vault_address"],
            "vault_name": row["vault_name"],
            "management": row["management"] or "yearn",
            "protocol": row["protocol"],
            "direction": row["direction"],
            "sender": row["sender"],
            "owner": row["owner"],
            "receiver": row["receiver"],
            "tx_hash": row["tx_hash"],
            "log_index": row["log_index"],
            "block_number": row["block_number"],
            "block_timestamp": row["block_timestamp"],
            "asset": row["asset"],
            "asset_symbol": row["asset_symbol"],
            "assets_raw": row["assets_raw"],
            "shares_raw": row["shares_raw"],
            "price_usd": row["price_usd"],
            "price_status": row["price_status"] or "missing",
            "amount_usd": _decimal_or_none(amount_usd),
        }
        write_output(conn, run_id, "vault_flows", output)
        _add_volume_to_totals(totals, row, amount_usd, row["direction"])

    debt_rows = conn.execute(
        f"""
        SELECT
            sdf.*,
            v.asset_symbol,
            v.name AS vault_name,
            v.management,
            v.protocol,
            p.price_usd,
            p.status AS price_status
        FROM strategy_debt_flows sdf
        LEFT JOIN vaults v
          ON v.chain_id = sdf.chain_id AND v.address = sdf.vault_address
        {_selected_price_join("sdf")}
        ORDER BY sdf.chain_id, sdf.block_number, sdf.log_index, sdf.direction
        """
    ).fetchall()
    for row in debt_rows:
        amount_usd = _usd(row["debt_delta_raw"], row["asset_decimals"], row["price_usd"])
        output = {
            "chain_id": row["chain_id"],
            "version": row["version"],
            "vault_address": row["vault_address"],
            "vault_name": row["vault_name"],
            "management": row["management"] or "yearn",
            "protocol": row["protocol"],
            "strategy_address": row["strategy_address"],
            "direction": row["direction"],
            "source_event": row["source_event"],
            "tx_hash": row["tx_hash"],
            "log_index": row["log_index"],
            "block_number": row["block_number"],
            "block_timestamp": row["block_timestamp"],
            "asset": row["asset"],
            "asset_symbol": row["asset_symbol"],
            "debt_delta_raw": row["debt_delta_raw"],
            "current_debt_raw": row["current_debt_raw"],
            "new_debt_raw": row["new_debt_raw"],
            "price_usd": row["price_usd"],
            "price_status": row["price_status"] or "missing",
            "amount_usd": _decimal_or_none(amount_usd),
        }
        write_output(conn, run_id, "strategy_debt_flows", output)
        _add_volume_to_totals(totals, row, amount_usd, row["direction"])

    for (output_name, _), row in totals.items():
        serialized = dict(row)
        for key in (
            "deposit_usd",
            "withdraw_usd",
            "net_user_flow_usd",
            "allocation_usd",
            "deallocation_usd",
            "net_strategy_flow_usd",
            "gross_user_volume_usd",
            "gross_strategy_volume_usd",
            "gross_total_volume_usd",
        ):
            serialized[key] = _decimal_or_none(serialized[key])
        write_output(conn, run_id, output_name, serialized)
    conn.commit()
    complete_analysis_run(conn, run_id)
    return run_id
