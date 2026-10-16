from decimal import Decimal

from yearn_data.analysis import run_lifetime_yield, run_vault_fees, run_vault_volume
from yearn_data.config import CHAINS
from yearn_data.incident_adjustments import adjustment_for_tx
from yearn_data.storage import connect, from_json, init_db, seed_chains


def test_lifetime_yield_uses_net_gain_minus_loss(tmp_path):
    conn = connect(tmp_path / "test.sqlite")
    init_db(conn)
    seed_chains(conn, CHAINS)
    conn.execute(
        """
        INSERT INTO vaults (
            chain_id, version, address, asset, asset_symbol, asset_decimals,
            updated_at
        )
        VALUES (1, 'v3', '0x0000000000000000000000000000000000000001',
                '0x0000000000000000000000000000000000000002', 'USDC', 6, 1)
        """
    )
    conn.execute(
        """
        INSERT INTO strategy_reports (
            chain_id, version, vault_address, strategy_address, tx_hash, log_index,
            block_number, block_timestamp, asset, asset_decimals, gain_raw, loss_raw,
            net_raw, extra_json
        )
        VALUES (1, 'v3', '0x0000000000000000000000000000000000000001',
                '0x0000000000000000000000000000000000000003',
                '0xabc', 0, 10, 100, '0x0000000000000000000000000000000000000002',
                6, '1500000', '250000', '1250000', '{}')
        """
    )
    conn.execute(
        """
        INSERT INTO prices (chain_id, token_address, timestamp, source, price_usd, status)
        VALUES (1, '0x0000000000000000000000000000000000000002', 100, 'defillama', 2.0, 'ok')
        """
    )
    conn.commit()

    run_id = run_lifetime_yield(conn)
    row = conn.execute(
        "SELECT row_json FROM analysis_outputs WHERE run_id=? AND name='total_yield_summary'",
        (run_id,),
    ).fetchone()
    output = from_json(row["row_json"])
    assert Decimal(output["gross_gain_usd"]) == Decimal("3.00")
    assert Decimal(output["loss_usd"]) == Decimal("0.500")
    assert Decimal(output["net_yield_usd"]) == Decimal("2.500")
    assert output["reports"] == 1
    assert output["priced_reports"] == 1


def test_lifetime_yield_selects_and_records_requested_price_source(tmp_path):
    conn = connect(tmp_path / "test.sqlite")
    init_db(conn)
    seed_chains(conn, CHAINS)
    asset = "0x0000000000000000000000000000000000000002"
    conn.execute(
        """
        INSERT INTO vaults (
            chain_id, version, address, asset, asset_symbol, asset_decimals,
            updated_at
        )
        VALUES (1, 'v3', '0x0000000000000000000000000000000000000001',
                ?, 'USDC', 6, 1)
        """,
        (asset,),
    )
    conn.execute(
        """
        INSERT INTO strategy_reports (
            chain_id, version, vault_address, strategy_address, tx_hash, log_index,
            block_number, block_timestamp, asset, asset_decimals, gain_raw, loss_raw,
            net_raw, extra_json
        )
        VALUES (1, 'v3', '0x0000000000000000000000000000000000000001',
                '0x0000000000000000000000000000000000000003',
                '0xabc', 0, 10, 100, ?, 6, '1000000', '0', '1000000', '{}')
        """,
        (asset,),
    )
    conn.executemany(
        """
        INSERT INTO prices (
            chain_id, token_address, timestamp, source, price_usd, status, raw_json
        ) VALUES (1, ?, 100, ?, ?, 'ok', ?)
        """,
        [
            (asset, "defillama", 2.0, "{}"),
            (
                asset,
                "yearn-prices",
                3.0,
                '{"adapter":"batchHistorical","confidence":0.99,'
                '"normalized_timestamp":86399,"upstream_source":"chainlink"}',
            ),
        ],
    )
    conn.commit()

    run_id = run_lifetime_yield(conn, price_source="yearn-prices")

    params = from_json(
        conn.execute("SELECT params_json FROM analysis_runs WHERE id=?", (run_id,)).fetchone()["params_json"]
    )
    assert params == {
        "price_source": "yearn-prices",
        "fallback_price_source": "defillama",
        "before_timestamp": None,
    }
    summary = from_json(
        conn.execute(
            "SELECT row_json FROM analysis_outputs WHERE run_id=? AND name='total_yield_summary'",
            (run_id,),
        ).fetchone()["row_json"]
    )
    assert Decimal(summary["net_yield_usd"]) == Decimal("3.0")
    assert summary["price_source"] == "yearn-prices"
    report = from_json(
        conn.execute(
            "SELECT row_json FROM analysis_outputs WHERE run_id=? AND name='reports'",
            (run_id,),
        ).fetchone()["row_json"]
    )
    assert report["price_source"] == "yearn-prices"
    assert report["price_evidence_timestamp"] == 86_399
    assert report["price_upstream_source"] == "chainlink"
    assert report["price_confidence"] == 0.99
    assert report["price_adapter"] == "batchHistorical"


def test_lifetime_yield_falls_back_to_defillama_when_yearn_prices_is_missing(tmp_path):
    conn = connect(tmp_path / "test.sqlite")
    init_db(conn)
    seed_chains(conn, CHAINS)
    asset = "0x0000000000000000000000000000000000000002"
    conn.execute(
        """
        INSERT INTO vaults (
            chain_id, version, address, asset, asset_symbol, asset_decimals,
            updated_at
        ) VALUES (1, 'v3', '0x0000000000000000000000000000000000000001',
                  ?, 'USDC', 6, 1)
        """,
        (asset,),
    )
    conn.execute(
        """
        INSERT INTO strategy_reports (
            chain_id, version, vault_address, strategy_address, tx_hash, log_index,
            block_number, block_timestamp, asset, asset_decimals, gain_raw, loss_raw,
            net_raw, extra_json
        ) VALUES (1, 'v3', '0x0000000000000000000000000000000000000001',
                  '0x0000000000000000000000000000000000000003',
                  '0xabc', 0, 10, 100, ?, 6, '1000000', '0', '1000000', '{}')
        """,
        (asset,),
    )
    conn.executemany(
        """
        INSERT INTO prices (
            chain_id, token_address, timestamp, source, price_usd, status, raw_json
        ) VALUES (1, ?, 100, ?, ?, ?, ?)
        """,
        [
            (asset, "yearn-prices", None, "missing", '{"failure_class":"not-found"}'),
            (asset, "defillama", 2.0, "ok", '{"source":"curve_lp_balance_fallback"}'),
        ],
    )
    conn.commit()

    run_id = run_lifetime_yield(conn)

    report = from_json(
        conn.execute(
            "SELECT row_json FROM analysis_outputs WHERE run_id=? AND name='reports'",
            (run_id,),
        ).fetchone()["row_json"]
    )
    assert report["price_usd"] == 2.0
    assert report["price_source"] == "defillama"
    assert report["primary_price_source"] == "yearn-prices"
    assert report["fallback_price_source"] == "defillama"


def test_lifetime_yield_excludes_known_incident_adjustments(tmp_path):
    conn = connect(tmp_path / "test.sqlite")
    init_db(conn)
    seed_chains(conn, CHAINS)
    conn.execute(
        """
        INSERT INTO vaults (
            chain_id, version, address, asset, asset_symbol, asset_decimals,
            updated_at
        )
        VALUES (1, 'v2', '0x0000000000000000000000000000000000000001',
                '0x0000000000000000000000000000000000000002', 'DAI', 18, 1)
        """
    )
    reports = [
        (
            "0x5558d40c511524b015cd307a134b3358f52326cf39c2c6e61604d5726c64dfdd",
            "0",
            "100000000000000000000",
            "-100000000000000000000",
            100,
        ),
        (
            "0x66847f4dc80a6b4c32666972a9a68416d802d78b54619503fd0aec358fedb185",
            "200000000000000000000",
            "0",
            "200000000000000000000",
            101,
        ),
    ]
    for tx_hash, gain, loss, net, timestamp in reports:
        conn.execute(
            """
            INSERT INTO strategy_reports (
                chain_id, version, vault_address, strategy_address, tx_hash, log_index,
                block_number, block_timestamp, asset, asset_decimals, gain_raw, loss_raw,
                net_raw, extra_json
            )
            VALUES (1, 'v2', '0x0000000000000000000000000000000000000001',
                    '0x0000000000000000000000000000000000000003',
                    ?, 0, 10, ?, '0x0000000000000000000000000000000000000002',
                    18, ?, ?, ?, '{}')
            """,
            (tx_hash, timestamp, gain, loss, net),
        )
        conn.execute(
            """
            INSERT INTO prices (chain_id, token_address, timestamp, source, price_usd, status)
            VALUES (1, '0x0000000000000000000000000000000000000002', ?, 'defillama', 1.0, 'ok')
            """,
            (timestamp,),
        )
    conn.commit()

    run_id = run_lifetime_yield(conn)
    row = conn.execute(
        "SELECT row_json FROM analysis_outputs WHERE run_id=? AND name='total_yield_summary'",
        (run_id,),
    ).fetchone()
    output = from_json(row["row_json"])
    assert Decimal(output["raw_gross_gain_usd"]) == Decimal("200.0")
    assert Decimal(output["raw_loss_usd"]) == Decimal("100.0")
    assert Decimal(output["raw_net_yield_usd"]) == Decimal("100.0")
    assert Decimal(output["gross_gain_usd"]) == Decimal("0.0")
    assert Decimal(output["loss_usd"]) == Decimal("0.0")
    assert Decimal(output["net_yield_usd"]) == Decimal("0.0")
    assert output["adjusted_reports"] == 2

    report_rows = conn.execute(
        "SELECT row_json FROM analysis_outputs WHERE run_id=? AND name='reports'",
        (run_id,),
    ).fetchall()
    outputs = [from_json(row["row_json"]) for row in report_rows]
    assert {row["incident_id"] for row in outputs} == {"yearn-2021-05-14-dai-curve-paper-loss"}
    assert all(row["is_adjusted"] for row in outputs)


def test_vault_volume_sums_user_and_strategy_gross_flows(tmp_path):
    conn = connect(tmp_path / "test.sqlite")
    init_db(conn)
    seed_chains(conn, CHAINS)
    conn.execute(
        """
        INSERT INTO vaults (
            chain_id, version, address, asset, asset_symbol, asset_decimals,
            updated_at
        )
        VALUES (1, 'v3', '0x0000000000000000000000000000000000000001',
                '0x0000000000000000000000000000000000000002', 'USDC', 6, 1)
        """
    )
    conn.executemany(
        """
        INSERT INTO vault_flows (
            chain_id, version, vault_address, direction, tx_hash, log_index,
            block_number, block_timestamp, asset, asset_decimals, assets_raw,
            shares_raw, decoded_json
        )
        VALUES (1, 'v3', '0x0000000000000000000000000000000000000001',
                ?, ?, ?, 10, 100, '0x0000000000000000000000000000000000000002',
                6, ?, ?, '{}')
        """,
        [
            ("deposit", "0xaaa", 0, "1000000", "1000000"),
            ("withdraw", "0xbbb", 1, "250000", "250000"),
        ],
    )
    conn.executemany(
        """
        INSERT INTO strategy_debt_flows (
            chain_id, version, vault_address, strategy_address, direction,
            tx_hash, log_index, block_number, block_timestamp, asset,
            asset_decimals, debt_delta_raw, current_debt_raw, new_debt_raw,
            source_event, decoded_json
        )
        VALUES (1, 'v3', '0x0000000000000000000000000000000000000001',
                '0x0000000000000000000000000000000000000003',
                ?, ?, ?, 10, 100, '0x0000000000000000000000000000000000000002',
                6, ?, NULL, NULL, 'DebtUpdated', '{}')
        """,
        [
            ("allocation", "0xccc", 2, "500000"),
            ("deallocation", "0xddd", 3, "125000"),
        ],
    )
    conn.execute(
        """
        INSERT INTO prices (chain_id, token_address, timestamp, source, price_usd, status)
        VALUES (1, '0x0000000000000000000000000000000000000002', 100, 'defillama', 2.0, 'ok')
        """
    )
    conn.commit()

    run_id = run_vault_volume(conn)
    row = conn.execute(
        "SELECT row_json FROM analysis_outputs WHERE run_id=? AND name='volume_summary'",
        (run_id,),
    ).fetchone()
    output = from_json(row["row_json"])
    assert Decimal(output["deposit_usd"]) == Decimal("2.00")
    assert Decimal(output["withdraw_usd"]) == Decimal("0.500")
    assert Decimal(output["net_user_flow_usd"]) == Decimal("1.500")
    assert Decimal(output["allocation_usd"]) == Decimal("1.00")
    assert Decimal(output["deallocation_usd"]) == Decimal("0.250")
    assert Decimal(output["net_strategy_flow_usd"]) == Decimal("0.750")
    assert Decimal(output["gross_user_volume_usd"]) == Decimal("2.500")
    assert Decimal(output["gross_strategy_volume_usd"]) == Decimal("1.250")
    assert Decimal(output["gross_total_volume_usd"]) == Decimal("3.750")


def test_vault_fees_uses_v3_report_fee_fields(tmp_path):
    conn = connect(tmp_path / "test.sqlite")
    init_db(conn)
    seed_chains(conn, CHAINS)
    conn.execute(
        """
        INSERT INTO vaults (
            chain_id, version, address, asset, asset_symbol, asset_decimals,
            updated_at
        )
        VALUES (1, 'v3', '0x0000000000000000000000000000000000000001',
                '0x0000000000000000000000000000000000000002', 'USDC', 6, 1)
        """
    )
    conn.execute(
        """
        INSERT INTO strategy_reports (
            chain_id, version, vault_address, strategy_address, tx_hash, log_index,
            block_number, block_timestamp, asset, asset_decimals, gain_raw, loss_raw,
            net_raw, current_debt_raw, protocol_fees_raw, total_fees_raw,
            total_refunds_raw, extra_json
        )
        VALUES (1, 'v3', '0x0000000000000000000000000000000000000001',
                '0x0000000000000000000000000000000000000003',
                '0xabc', 0, 10, 100, '0x0000000000000000000000000000000000000002',
                6, '10000000', '0', '10000000', '0', '1000000', '2500000',
                '500000', '{}')
        """
    )
    conn.execute(
        """
        INSERT INTO prices (chain_id, token_address, timestamp, source, price_usd, status)
        VALUES (1, '0x0000000000000000000000000000000000000002', 100, 'defillama', 2.0, 'ok')
        """
    )
    conn.commit()

    run_id = run_vault_fees(conn)
    row = conn.execute(
        "SELECT row_json FROM analysis_outputs WHERE run_id=? AND name='fee_summary'",
        (run_id,),
    ).fetchone()
    output = from_json(row["row_json"])
    assert output["events"] == 1
    assert Decimal(output["v3_protocol_fees_usd"]) == Decimal("2.00")
    assert Decimal(output["v3_total_fees_usd"]) == Decimal("5.00")
    assert Decimal(output["v3_total_refunds_usd"]) == Decimal("1.00")
    assert Decimal(output["total_fees_usd"]) == Decimal("5.00")


def test_vault_fees_infers_v2_same_tx_share_mints(tmp_path):
    conn = connect(tmp_path / "test.sqlite")
    init_db(conn)
    seed_chains(conn, CHAINS)
    conn.execute(
        """
        INSERT INTO vaults (
            chain_id, version, address, asset, asset_symbol, asset_decimals,
            updated_at
        )
        VALUES (1, 'v2', '0x0000000000000000000000000000000000000001',
                '0x0000000000000000000000000000000000000002', 'DAI', 18, 1)
        """
    )
    conn.execute(
        """
        INSERT INTO strategy_reports (
            chain_id, version, vault_address, strategy_address, tx_hash, log_index,
            block_number, block_timestamp, asset, asset_decimals, gain_raw, loss_raw,
            net_raw, extra_json
        )
        VALUES (1, 'v2', '0x0000000000000000000000000000000000000001',
                '0x0000000000000000000000000000000000000003',
                '0xabc', 5, 10, 100, '0x0000000000000000000000000000000000000002',
                18, '10000000000000000000', '0', '10000000000000000000', '{}')
        """
    )
    conn.execute(
        """
        INSERT INTO vault_fee_events (
            chain_id, version, source, vault_address, strategy_address, recipient,
            tx_hash, log_index, block_number, block_timestamp, asset,
            asset_decimals, fee_raw, shares_raw, decoded_json
        )
        VALUES (1, 'v2', 'v2_harvest_share_mint',
                '0x0000000000000000000000000000000000000001',
                '0x0000000000000000000000000000000000000003',
                '0x0000000000000000000000000000000000000004',
                '0xabc', 6, 10, 100, '0x0000000000000000000000000000000000000002',
                18, '3000000000000000000', '3000000000000000000',
                '{"from":"0x0000000000000000000000000000000000000000","to":"0x0000000000000000000000000000000000000004","source_event":"Transfer"}')
        """
    )
    conn.execute(
        """
        INSERT INTO prices (chain_id, token_address, timestamp, source, price_usd, status)
        VALUES (1, '0x0000000000000000000000000000000000000002', 100, 'defillama', 1.5, 'ok')
        """
    )
    conn.commit()

    run_id = run_vault_fees(conn)
    row = conn.execute(
        "SELECT row_json FROM analysis_outputs WHERE run_id=? AND name='fee_summary'",
        (run_id,),
    ).fetchone()
    output = from_json(row["row_json"])
    assert output["events"] == 1
    assert Decimal(output["v2_fee_mint_usd"]) == Decimal("4.50")
    assert Decimal(output["total_fees_usd"]) == Decimal("4.50")

    event_row = conn.execute(
        "SELECT row_json FROM analysis_outputs WHERE run_id=? AND name='fee_events'",
        (run_id,),
    ).fetchone()
    event = from_json(event_row["row_json"])
    assert event["source"] == "v2_harvest_share_mint"
    assert event["recipient"] == "0x0000000000000000000000000000000000000004"


def test_zero_amount_report_does_not_require_a_price(tmp_path):
    conn = connect(tmp_path / "test.sqlite")
    init_db(conn)
    seed_chains(conn, CHAINS)
    conn.execute(
        """
        INSERT INTO vaults (chain_id, version, address, asset, asset_decimals, updated_at)
        VALUES (1, 'v2', '0x0000000000000000000000000000000000000001',
                '0x0000000000000000000000000000000000000002', 18, 1)
        """
    )
    conn.execute(
        """
        INSERT INTO strategy_reports (
            chain_id, version, vault_address, strategy_address, tx_hash, log_index,
            block_number, block_timestamp, asset, asset_decimals, gain_raw, loss_raw,
            net_raw, extra_json
        ) VALUES (1, 'v2', '0x0000000000000000000000000000000000000001',
                  '0x0000000000000000000000000000000000000003', '0xzero', 0,
                  10, 100, '0x0000000000000000000000000000000000000002',
                  18, '0', '0', '0', '{}')
        """
    )
    conn.commit()

    run_id = run_lifetime_yield(conn)
    summary = from_json(
        conn.execute(
            "SELECT row_json FROM analysis_outputs WHERE run_id=? AND name='total_yield_summary'",
            (run_id,),
        ).fetchone()["row_json"]
    )
    report = from_json(
        conn.execute(
            "SELECT row_json FROM analysis_outputs WHERE run_id=? AND name='reports'",
            (run_id,),
        ).fetchone()["row_json"]
    )

    assert summary["priced_reports"] == 1
    assert summary["unpriced_reports"] == 0
    assert report["valuation_status"] == "zero_amount_no_price_required"
    assert report["net_yield_usd"] == "0"


def test_yrecover_loss_is_an_incident_adjustment():
    adjustment = adjustment_for_tx(
        "0x9cc9c42e2da1a2ae774a6ac393aff5974462c999773ea0b04cdd41aebdf5d650"
    )

    assert adjustment is not None
    assert adjustment.classification == "intentional_recovery_accounting_loss"
    assert adjustment.adjusted_net_raw == "0"
