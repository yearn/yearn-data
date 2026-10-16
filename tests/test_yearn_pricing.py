import pytest
from yearn_data.config import CHAINS
from yearn_data.pricing import _yearn_prices_batches, fetch_yearn_price, normalize_yearn_price_timestamp, price_unpriced_reports
from yearn_data.storage import connect, from_json, init_db, seed_chains

def test_yearn_prices_batches_normalize_and_respect_route_limits():
    first = "0x0000000000000000000000000000000000000001"
    second = "0x0000000000000000000000000000000000000002"
    targets = [(1, first, day * 86_400) for day in range(91)] + [(1, second, 100)]

    batches = _yearn_prices_batches(targets, max_tokens=2)

    assert sum(len(batch) for batch in batches) == 92
    for batch in batches:
        by_token = {}
        for chain_id, token, timestamp in batch:
            by_token.setdefault((chain_id, token), []).append(timestamp)
            assert timestamp % 86_400 == 86_399
        assert len(by_token) <= 2
        assert all(len(timestamps) <= 90 for timestamps in by_token.values())


def test_price_unpriced_reports_uses_yearn_prices_eod_and_exact_fallback(monkeypatch, tmp_path):
    conn = connect(tmp_path / "test.sqlite")
    init_db(conn)
    seed_chains(conn, CHAINS)
    asset = "0x0000000000000000000000000000000000000002"
    reports = [("0xaaa", 100), ("0xbbb", 200), ("0xccc", 90_000)]
    for tx_hash, timestamp in reports:
        conn.execute(
            """
            INSERT INTO strategy_reports (
                chain_id, version, vault_address, strategy_address, tx_hash, log_index,
                block_number, block_timestamp, asset, asset_decimals, gain_raw, loss_raw,
                net_raw, extra_json
            )
            VALUES (1, 'v3', '0x0000000000000000000000000000000000000001',
                    '0x0000000000000000000000000000000000000003',
                    ?, 0, 10, ?, ?, 6, '1', '0', '1', '{}')
            """,
            (tx_hash, timestamp, asset),
        )
    conn.commit()

    batch_calls = []
    exact_calls = []

    def fake_batch(targets):
        batch_calls.extend(targets)
        first_eod = normalize_yearn_price_timestamp(100)
        return {
            (1, asset, first_eod): (
                1.0,
                "ok",
                {
                    "adapter": "batchHistorical",
                    "confidence": 0.99,
                    "normalized_timestamp": first_eod,
                    "provider": "yearn-prices",
                    "symbol": "USDC",
                    "upstream_source": "defillama",
                },
            )
        }

    def fake_exact(chain_id, token_address, timestamp):
        exact_calls.append((chain_id, token_address, timestamp))
        return (
            2.0,
            "ok",
            {
                "adapter": "historical",
                "confidence": 0.8,
                "normalized_timestamp": timestamp,
                "provider": "yearn-prices",
                "symbol": "USDC",
                "upstream_source": "chainlink",
            },
        )

    monkeypatch.setattr("yearn_data.pricing.fetch_yearn_prices_batch", fake_batch)
    monkeypatch.setattr("yearn_data.pricing.fetch_yearn_price", fake_exact)

    count = price_unpriced_reports(conn, source="yearn-prices")

    assert count == 3
    assert sorted(batch_calls) == [
        (1, asset, normalize_yearn_price_timestamp(100)),
        (1, asset, normalize_yearn_price_timestamp(90_000)),
    ]
    assert exact_calls == [(1, asset, normalize_yearn_price_timestamp(90_000))]
    rows = conn.execute(
        "SELECT timestamp, price_usd, status, raw_json FROM prices ORDER BY timestamp"
    ).fetchall()
    assert [(row["timestamp"], row["price_usd"], row["status"]) for row in rows] == [
        (100, 1.0, "ok"),
        (200, 1.0, "ok"),
        (90_000, 2.0, "ok"),
    ]
    payloads = [from_json(row["raw_json"]) for row in rows]
    assert [payload["requested_timestamp"] for payload in payloads] == [100, 200, 90_000]
    assert [payload["normalized_timestamp"] for payload in payloads] == [86_399, 86_399, 172_799]
    assert [payload["adapter"] for payload in payloads] == [
        "batchHistorical",
        "batchHistorical",
        "historical",
    ]


def test_price_unpriced_reports_uses_defillama_only_for_unresolved_yearn_prices(
    monkeypatch, tmp_path
):
    conn = connect(tmp_path / "test.sqlite")
    init_db(conn)
    seed_chains(conn, CHAINS)
    first_asset = "0x0000000000000000000000000000000000000002"
    second_asset = "0x0000000000000000000000000000000000000004"
    for index, asset in enumerate((first_asset, second_asset)):
        conn.execute(
            """
            INSERT INTO strategy_reports (
                chain_id, version, vault_address, strategy_address, tx_hash, log_index,
                block_number, block_timestamp, asset, asset_decimals, gain_raw, loss_raw,
                net_raw, extra_json
            ) VALUES (1, 'v3', '0x0000000000000000000000000000000000000001',
                      '0x0000000000000000000000000000000000000003',
                      ?, 0, ?, 100, ?, 6, '1', '0', '1', '{}')
            """,
            (f"0x{index}", 10 + index, asset),
        )
    conn.commit()

    normalized = normalize_yearn_price_timestamp(100)

    def fake_yearn_batch(_targets):
        return {
            (1, first_asset, normalized): (
                1.0,
                "ok",
                {
                    "adapter": "batchHistorical",
                    "normalized_timestamp": normalized,
                    "provider": "yearn-prices",
                    "upstream_source": "chainlink",
                },
            )
        }

    monkeypatch.setattr("yearn_data.pricing.fetch_yearn_prices_batch", fake_yearn_batch)
    monkeypatch.setattr(
        "yearn_data.pricing.fetch_yearn_price",
        lambda *_args: (
            None,
            "missing",
            {
                "adapter": "historical",
                "failure_class": "not-found",
                "normalized_timestamp": normalized,
                "provider": "yearn-prices",
            },
        ),
    )
    defillama_requests = []

    def fake_defillama_batch(targets):
        defillama_requests.extend(targets)
        return {
            target: (2.0, "ok", {"source": "defillama"})
            for target in targets
        }

    monkeypatch.setattr("yearn_data.pricing.fetch_defillama_prices_batch", fake_defillama_batch)

    count = price_unpriced_reports(conn)

    assert count == 3
    assert defillama_requests == [(1, second_asset, 100)]
    rows = conn.execute(
        "SELECT token_address, source, price_usd, status FROM prices ORDER BY token_address, source"
    ).fetchall()
    assert [tuple(row) for row in rows] == [
        (first_asset, "yearn-prices", 1.0, "ok"),
        (second_asset, "defillama", 2.0, "ok"),
        (second_asset, "yearn-prices", None, "missing"),
    ]


def test_price_unpriced_reports_falls_back_after_yearn_prices_transport_failure(
    monkeypatch, tmp_path
):
    from yearn_data.pricing import YearnPricesRequestError

    conn = connect(tmp_path / "test.sqlite")
    init_db(conn)
    seed_chains(conn, CHAINS)
    asset = "0x0000000000000000000000000000000000000002"
    conn.execute(
        """
        INSERT INTO strategy_reports (
            chain_id, version, vault_address, strategy_address, tx_hash, log_index,
            block_number, block_timestamp, asset, asset_decimals, gain_raw, loss_raw,
            net_raw, extra_json
        ) VALUES (1, 'v3', '0x0000000000000000000000000000000000000001',
                  '0x0000000000000000000000000000000000000003',
                  '0xabc', 0, 10, 100, ?, 6, '1', '0', '1', '{}')
        """,
        (asset,),
    )
    conn.commit()

    monkeypatch.setattr(
        "yearn_data.pricing.fetch_yearn_prices_batch",
        lambda _targets: (_ for _ in ()).throw(YearnPricesRequestError("batch unavailable")),
    )
    monkeypatch.setattr(
        "yearn_data.pricing.fetch_yearn_price",
        lambda *_args: (
            None,
            "retryable",
            {"failure_class": "retryable", "provider": "yearn-prices"},
        ),
    )
    monkeypatch.setattr(
        "yearn_data.pricing.fetch_defillama_prices_batch",
        lambda targets: {target: (2.0, "ok", {"source": "defillama"}) for target in targets},
    )

    assert price_unpriced_reports(conn) == 2
    rows = conn.execute(
        "SELECT source, price_usd, status FROM prices ORDER BY source"
    ).fetchall()
    assert [tuple(row) for row in rows] == [
        ("defillama", 2.0, "ok"),
        ("yearn-prices", None, "retryable"),
    ]


def test_yearn_prices_exact_keeps_exhausted_transport_failure_retryable(monkeypatch):
    from yearn_data.pricing import YearnPricesRequestError

    monkeypatch.setattr(
        "yearn_data.pricing._yearn_prices_get",
        lambda *args, **kwargs: (_ for _ in ()).throw(YearnPricesRequestError("request exhausted")),
    )

    price, status, payload = fetch_yearn_price(
        1,
        "0x0000000000000000000000000000000000000002",
        100,
    )

    assert price is None
    assert status == "retryable"
    assert payload["failure_class"] == "retryable"
    assert payload["normalized_timestamp"] == 86_399


def test_yearn_prices_exact_keeps_terminal_response_invalid(monkeypatch):
    from yearn_data.pricing import YearnPricesRequestError

    monkeypatch.setattr(
        "yearn_data.pricing._yearn_prices_get",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            YearnPricesRequestError("bad response", retryable=False)
        ),
    )

    price, status, payload = fetch_yearn_price(
        1,
        "0x0000000000000000000000000000000000000002",
        100,
    )

    assert price is None
    assert status == "invalid"
    assert payload["failure_class"] == "invalid"




@pytest.mark.parametrize('problem', ['wrong_day', 'wrong_asset', 'duplicate_zero', 'invalid_price', 'missing_source'])
def test_yearn_batch_rejects_invalid_evidence(monkeypatch, problem):
    from yearn_data.pricing import fetch_yearn_prices_batch
    asset = '0x0000000000000000000000000000000000000002'
    raw = {'timestamp': 86399, 'price': 2.5, 'source': 'oracle'}
    key = 'ethereum:' + asset
    points = [raw]
    if problem == 'wrong_day': raw['timestamp'] += 86400
    if problem == 'wrong_asset': key = 'ethereum:' + '0x' + '3' * 40
    if problem == 'duplicate_zero': points = [{**raw, 'price': 0}, raw]
    if problem == 'invalid_price': raw['price'] = True
    if problem == 'missing_source': raw.pop('source')
    monkeypatch.setattr('yearn_data.pricing._yearn_prices_get', lambda *a, **kw: {'coins': {key: {'prices': points}}})
    with pytest.raises(ValueError):
        fetch_yearn_prices_batch([(1, asset, 100)])


@pytest.mark.parametrize('status', [401, 403])
def test_yearn_authentication_stops_without_fallback(monkeypatch, status):
    from yearn_data import pricing
    monkeypatch.setenv('YEARN_PRICE_PROD_KEY', 'test-only')
    class Response:
        status_code = status
    calls = []
    monkeypatch.setattr(pricing.requests, 'get', lambda *a, **kw: calls.append(a) or Response())
    with pytest.raises(pricing.YearnPricesAuthenticationError):
        pricing.fetch_yearn_price(1, '0x' + '2' * 40, 100)
    assert len(calls) == 1


def test_yearn_only_command_disables_both_fallbacks(tmp_path, monkeypatch):
    from yearn_data import cli
    calls = []
    monkeypatch.setattr(cli, 'price_unpriced_reports', lambda conn, **kw: calls.append(kw) or 0)
    assert cli.main(['--db', str(tmp_path / 'prices.sqlite'), 'price', '--source', 'yearn-prices',
                     '--no-provider-fallback', '--no-onchain-fallbacks']) == 0
    assert calls[0]['source'] == 'yearn-prices'
    assert calls[0]['fallback'] is None
    assert calls[0]['onchain_fallbacks'] is False


def test_gnosis_price_namespace_requires_no_rpc():
    from yearn_data.pricing import yearn_prices_token_key
    assert yearn_prices_token_key(100, '0x' + '2' * 40) == 'gnosis:0x' + '2' * 40
