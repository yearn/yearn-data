import pytest

from eth_abi import encode as abi_encode

from yearn_data.config import CHAINS
from yearn_data.pricing import (
    _aave_atoken_price,
    _balancer_bpt_price,
    _curve_lp_price,
    _curve_pool_from_registry,
    _erc4626_wrapper_price,
    _exchange_rate_wrapped_price,
    fetch_defillama_prices_batch,
    _yearn_v1_vault_price,
    fetch_defillama_price,
    price_unpriced_reports,
    price_unpriced_volume,
)
from yearn_data.storage import connect, from_json, init_db, seed_chains


class FakeResponse:
    status_code = 200
    headers = {}

    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


def test_defillama_batch_accepts_only_samples_within_twelve_hours(monkeypatch):
    asset = "0x0000000000000000000000000000000000000002"
    coin = f"ethereum:{asset}"

    def fake_get(url, *, params, timeout):
        assert url.endswith("/batchHistorical")
        assert timeout == 30
        return FakeResponse(
            {
                "coins": {
                    coin: {
                        "prices": [
                            {"timestamp": 143_200, "price": 2.0},
                            {"timestamp": 243_201, "price": 3.0},
                        ]
                    }
                }
            }
        )

    monkeypatch.setattr("yearn_data.pricing.requests.get", fake_get)

    results = fetch_defillama_prices_batch([(1, asset, 100_000), (1, asset, 200_000)])

    accepted = results[(1, asset, 100_000)]
    rejected = results[(1, asset, 200_000)]
    assert accepted[0:2] == (2.0, "ok")
    assert accepted[2]["matched_offset_seconds"] == 43_200
    assert accepted[2]["maximum_accepted_offset_seconds"] == 43_200
    assert rejected[0:2] == (None, "missing")


def test_defillama_batch_can_reuse_an_observation_for_nearby_reports(monkeypatch):
    asset = "0x0000000000000000000000000000000000000002"
    coin = f"ethereum:{asset}"

    monkeypatch.setattr(
        "yearn_data.pricing.requests.get",
        lambda *args, **kwargs: FakeResponse(
            {"coins": {coin: {"prices": [{"timestamp": 1_500, "price": 2.0}]}}}
        ),
    )

    results = fetch_defillama_prices_batch([(1, asset, 1_000), (1, asset, 2_000)])

    assert results[(1, asset, 1_000)][0:2] == (2.0, "ok")
    assert results[(1, asset, 2_000)][0:2] == (2.0, "ok")


def test_fetch_defillama_price_retries_rate_limit(monkeypatch):
    coin = "ethereum:0x0000000000000000000000000000000000000001"
    responses = [
        type("Response", (), {"status_code": 429, "headers": {"Retry-After": "0"}})(),
        type(
            "Response",
            (),
            {
                "status_code": 200,
                "headers": {},
                "raise_for_status": lambda self: None,
                "json": lambda self: {"coins": {coin: {"prices": [{"timestamp": 100, "price": 1.25}]}}},
            },
        )(),
    ]
    calls = []

    def fake_get(url, params=None, timeout=None):
        calls.append((url, params, timeout))
        return responses.pop(0)

    monkeypatch.setattr("yearn_data.pricing.requests.get", fake_get)
    monkeypatch.setattr("yearn_data.pricing.time.sleep", lambda _: None)

    price, status, _ = fetch_defillama_price(1, coin.split(":", 1)[1], 100)

    assert price == 1.25
    assert status == "ok"
    assert len(calls) == 2


def test_price_unpriced_reports_records_defillama_missing_without_fallback(monkeypatch, tmp_path):
    conn = connect(tmp_path / "test.sqlite")
    init_db(conn)
    seed_chains(conn, CHAINS)
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
                6, '1', '0', '1', '{}')
        """
    )
    def fake_defillama_batch(requests_):
        return {
            (chain_id, token_address, timestamp): (None, "error", {"source": "defillama"})
            for chain_id, token_address, timestamp in requests_
        }

    monkeypatch.setattr("yearn_data.pricing.fetch_defillama_prices_batch", fake_defillama_batch)
    count = price_unpriced_reports(conn, source="defillama", fallback=None, onchain_fallbacks=False)
    assert count == 1
    row = conn.execute("SELECT source, price_usd, status FROM prices").fetchone()
    assert row["source"] == "defillama"
    assert row["price_usd"] is None
    assert row["status"] == "error"


def test_price_unpriced_volume_uses_flow_tables(monkeypatch, tmp_path):
    conn = connect(tmp_path / "test.sqlite")
    init_db(conn)
    seed_chains(conn, CHAINS)
    conn.execute(
        """
        INSERT INTO vault_flows (
            chain_id, version, vault_address, direction, tx_hash, log_index,
            block_number, block_timestamp, asset, asset_decimals, assets_raw,
            shares_raw, decoded_json
        )
        VALUES (1, 'v3', '0x0000000000000000000000000000000000000001',
                'deposit', '0xabc', 0, 10, 100,
                '0x0000000000000000000000000000000000000002',
                6, '1000000', '1000000', '{}')
        """
    )
    conn.commit()

    def fake_defillama_batch(requests_):
        return {
            (chain_id, token_address, timestamp): (3.0, "ok", {"source": "defillama"})
            for chain_id, token_address, timestamp in requests_
        }

    monkeypatch.setattr("yearn_data.pricing.fetch_defillama_prices_batch", fake_defillama_batch)
    count = price_unpriced_volume(conn, source="defillama", fallback=None)
    assert count == 1
    row = conn.execute("SELECT price_usd, status FROM prices").fetchone()
    assert row["price_usd"] == 3.0
    assert row["status"] == "ok"


def test_price_unpriced_volume_can_retry_missing_with_polygon_stable_fallback(monkeypatch, tmp_path):
    conn = connect(tmp_path / "test.sqlite")
    init_db(conn)
    seed_chains(conn, CHAINS)
    usdt = "0xc2132D05D31c914a87C6611C10748AEb04B58e8F"
    conn.execute(
        """
        INSERT INTO vault_flows (
            chain_id, version, vault_address, direction, tx_hash, log_index,
            block_number, block_timestamp, asset, asset_decimals, assets_raw,
            shares_raw, decoded_json
        )
        VALUES (137, 'v3', '0x0000000000000000000000000000000000000001',
                'deposit', '0xabc', 0, 10, 100, ?, 6, '1000000',
                '1000000', '{}')
        """,
        (usdt,),
    )
    conn.execute(
        """
        INSERT INTO prices (
            chain_id, token_address, timestamp, block_number, source, price_usd,
            status, raw_json
        )
        VALUES (137, ?, 100, 10, 'defillama', NULL, 'missing', '{}')
        """,
        (usdt,),
    )
    conn.commit()

    def fake_defillama_batch(requests_):
        return {
            (chain_id, token_address, timestamp): (
                1.0,
                "ok",
                {"source": "canonical_polygon_stable_fallback"},
            )
            for chain_id, token_address, timestamp in requests_
        }

    monkeypatch.setattr("yearn_data.pricing.fetch_defillama_prices_batch", fake_defillama_batch)

    count = price_unpriced_volume(
        conn,
        source="defillama",
        fallback=None,
        retry_missing=True,
        chain_ids={137},
    )

    assert count == 1
    row = conn.execute("SELECT price_usd, status, raw_json FROM prices").fetchone()
    assert row["price_usd"] == 1.0
    assert row["status"] == "ok"
    assert "canonical_polygon_stable_fallback" in row["raw_json"]


def test_price_unpriced_reports_retries_missing_with_v3_alias(monkeypatch, tmp_path):
    conn = connect(tmp_path / "test.sqlite")
    init_db(conn)
    seed_chains(conn, CHAINS)
    usdt = "0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9"
    conn.execute(
        """
        INSERT INTO strategy_reports (
            chain_id, version, vault_address, strategy_address, tx_hash, log_index,
            block_number, block_timestamp, asset, asset_decimals, gain_raw, loss_raw,
            net_raw, extra_json
        )
        VALUES (42161, 'v3', '0x0000000000000000000000000000000000000001',
                '0x0000000000000000000000000000000000000003',
                '0xabc', 0, 10, 100, ?, 6, '1', '0', '1', '{}')
        """,
        (usdt,),
    )
    conn.execute(
        """
        INSERT INTO prices (
            chain_id, token_address, timestamp, block_number, source, price_usd,
            status, raw_json
        )
        VALUES (42161, ?, 100, 10, 'defillama', NULL, 'missing', '{}')
        """,
        (usdt,),
    )
    conn.commit()

    def fake_defillama_batch(requests_):
        return {
            (chain_id, token_address, timestamp): (None, "missing", {"source": "defillama"})
            for chain_id, token_address, timestamp in requests_
        }

    monkeypatch.setattr("yearn_data.pricing.fetch_defillama_prices_batch", fake_defillama_batch)
    count = price_unpriced_reports(
        conn,
        source="defillama",
        fallback=None,
        retry_missing=True,
        chain_ids={42161},
    )

    assert count == 1
    row = conn.execute("SELECT price_usd, status, raw_json FROM prices").fetchone()
    assert row["price_usd"] == 1.0
    assert row["status"] == "ok"
    assert "canonical_arbitrum_stable_fallback" in row["raw_json"]


def test_price_unpriced_reports_uses_curve_lp_fallback(monkeypatch, tmp_path):
    conn = connect(tmp_path / "test.sqlite")
    init_db(conn)
    seed_chains(conn, CHAINS)
    lp = "0x06325440D014e39736583c165C2963BA99fAf14E"
    conn.execute(
        """
        INSERT INTO strategy_reports (
            chain_id, version, vault_address, strategy_address, tx_hash, log_index,
            block_number, block_timestamp, asset, asset_decimals, gain_raw, loss_raw,
            net_raw, extra_json
        )
        VALUES (1, 'v2', '0x0000000000000000000000000000000000000001',
                '0x0000000000000000000000000000000000000003',
                '0xabc', 0, 10, 100, ?, 18, '1', '0', '1', '{}')
        """,
        (lp,),
    )
    conn.execute(
        """
        INSERT INTO prices (
            chain_id, token_address, timestamp, block_number, source, price_usd,
            status, raw_json
        )
        VALUES (1, ?, 100, 10, 'defillama', NULL, 'missing', '{}')
        """,
        (lp,),
    )
    conn.commit()

    def fake_defillama_batch(requests_):
        return {
            (chain_id, token_address, timestamp): (None, "missing", {"source": "defillama"})
            for chain_id, token_address, timestamp in requests_
        }

    def fake_curve(chain_id, token_address, timestamp, block_number):
        return 123.45, {"source": "curve_lp_balance_fallback"}

    monkeypatch.setattr("yearn_data.pricing.fetch_defillama_prices_batch", fake_defillama_batch)
    monkeypatch.setattr("yearn_data.pricing._curve_lp_price", fake_curve)
    count = price_unpriced_reports(conn, source="defillama", fallback=None, retry_missing=True, chain_ids={1})

    assert count == 1
    row = conn.execute("SELECT price_usd, status, raw_json FROM prices").fetchone()
    assert row["price_usd"] == 123.45
    assert row["status"] == "ok"
    assert "curve_lp_balance_fallback" in row["raw_json"]


def test_curve_pool_from_registry_resolves_lp_when_minter_is_missing(monkeypatch):
    registry = "0x00000000000000000000000000000000000000a1"
    pool = "0x00000000000000000000000000000000000000b2"
    lp = "0x00000000000000000000000000000000000000c3"

    _curve_pool_from_registry.cache_clear()

    def fake_call_address(chain_id, address, signature, block_number, args_hex=""):
        if signature == "get_address(uint256)" and args_hex == "0".rjust(64, "0"):
            return registry
        if signature == "get_pool_from_lp_token(address)" and address == registry:
            return pool
        return None

    monkeypatch.setattr("yearn_data.pricing._call_address", fake_call_address)

    assert _curve_pool_from_registry(1, lp, 100) == pool
    _curve_pool_from_registry.cache_clear()


def test_exchange_rate_wrapped_price_uses_underlying_value(monkeypatch):
    token = "0x00000000000000000000000000000000000000c0"
    underlying = "0x00000000000000000000000000000000000000d0"

    def fake_call_address(chain_id, address, signature, block_number, args_hex=""):
        if address == token and signature == "underlying()":
            return underlying
        return None

    def fake_call_uint256(chain_id, address, signature, block_number, args_hex=""):
        if address == token and signature == "exchangeRateStored()":
            return 2 * 10**26
        if signature == "decimals()" and address == token:
            return 8
        if signature == "decimals()" and address == underlying:
            return 18
        return None

    def fake_direct(chain_id, token_address, timestamp, block_number):
        assert token_address == underlying
        return 1.5, {"source": "underlying"}

    monkeypatch.setattr("yearn_data.pricing._call_address", fake_call_address)
    monkeypatch.setattr("yearn_data.pricing._call_uint256", fake_call_uint256)
    monkeypatch.setattr("yearn_data.pricing._direct_or_alias_price", fake_direct)

    price, payload = _exchange_rate_wrapped_price(1, token, 100, 10)

    assert price == 0.03
    assert payload["source"] == "exchange_rate_wrapper_fallback"


def test_aave_atoken_price_uses_underlying_value(monkeypatch):
    token = "0x00000000000000000000000000000000000000a0"
    underlying = "0x00000000000000000000000000000000000000d0"

    def fake_call_address(chain_id, address, signature, block_number, args_hex=""):
        if address == token and signature == "UNDERLYING_ASSET_ADDRESS()":
            return underlying
        return None

    def fake_direct(chain_id, token_address, timestamp, block_number):
        assert token_address == underlying
        return 1.25, {"source": "underlying"}

    monkeypatch.setattr("yearn_data.pricing._call_address", fake_call_address)
    monkeypatch.setattr("yearn_data.pricing._direct_or_alias_price", fake_direct)

    price, payload = _aave_atoken_price(1, token, 100, 10)

    assert price == 1.25
    assert payload["source"] == "aave_atoken_fallback"


def test_yearn_v1_vault_price_uses_historical_share_price(monkeypatch):
    token = "0x00000000000000000000000000000000000000b0"
    underlying = "0x00000000000000000000000000000000000000d0"

    monkeypatch.setattr(
        "yearn_data.pricing._call_address",
        lambda chain_id, address, signature, block_number, args_hex="": (
            underlying if address == token and signature == "token()" else None
        ),
    )
    monkeypatch.setattr(
        "yearn_data.pricing._call_uint256",
        lambda chain_id, address, signature, block_number, args_hex="": (
            1_025_000_000_000_000_000
            if address == token and signature == "getPricePerFullShare()"
            else None
        ),
    )
    monkeypatch.setattr(
        "yearn_data.pricing._direct_or_alias_price",
        lambda *args: (1.0, {"source": "canonical_stable"}),
    )

    price, payload = _yearn_v1_vault_price(1, token, 100, 10)

    assert price == 1.025
    assert payload["source"] == "yearn_v1_vault_fallback"


def test_curve_lp_price_fails_closed_when_reserve_is_unpriced(monkeypatch):
    _curve_lp_price.cache_clear()
    lp = "0x00000000000000000000000000000000000000aa"
    pool = "0x00000000000000000000000000000000000000ab"
    stable = "0x0000000000000000000000000000000000000001"
    unknown = "0x0000000000000000000000000000000000000002"

    def fake_address(chain_id, address, signature, block_number, args_hex=""):
        if address.lower() == lp and signature == "minter()":
            return pool
        if address.lower() == pool and signature == "coins(uint256)":
            return [stable, unknown][int(args_hex, 16)] if int(args_hex, 16) < 2 else None
        return None

    def fake_uint(chain_id, address, signature, block_number, args_hex=""):
        if address.lower() == lp and signature == "decimals()":
            return 18
        if address.lower() == lp and signature == "totalSupply()":
            return 1000 * 10**18
        if address.lower() == pool and signature == "balances(uint256)":
            return [100 * 10**6, 5 * 10**18][int(args_hex, 16)]
        if signature == "decimals()":
            return 6 if address.lower() == stable else 18
        return None

    monkeypatch.setattr("yearn_data.pricing._call_address", fake_address)
    monkeypatch.setattr("yearn_data.pricing._call_uint256", fake_uint)
    monkeypatch.setattr(
        "yearn_data.pricing._resolve_reserve_price",
        lambda chain_id, address, timestamp, block_number, depth: (
            (1.0, {"source": "stable"})
            if address.lower() == stable
            else (None, {"source": "missing"})
        ),
    )

    price, payload = _curve_lp_price(1, lp, 100, 10)

    assert price is None
    assert payload["status"] == "missing_reserve_price"
    assert payload["missing_reserves"][0]["coin"].lower() == unknown
    _curve_lp_price.cache_clear()


def test_erc4626_wrapper_price_uses_convert_to_assets(monkeypatch):
    token = "0x00000000000000000000000000000000000000e0"
    underlying = "0x00000000000000000000000000000000000000e1"

    def fake_address(chain_id, address, signature, block_number, args_hex=""):
        if address.lower() == token and signature == "asset()":
            return underlying
        return None

    def fake_uint(chain_id, address, signature, block_number, args_hex=""):
        if signature == "decimals()":
            return 18
        if address.lower() == token and signature == "convertToAssets(uint256)":
            return 105 * 10**16
        return None

    monkeypatch.setattr("yearn_data.pricing._call_address", fake_address)
    monkeypatch.setattr("yearn_data.pricing._call_uint256", fake_uint)
    monkeypatch.setattr(
        "yearn_data.pricing._resolve_reserve_price",
        lambda *args: (2.0, {"source": "underlying"}),
    )

    price, payload = _erc4626_wrapper_price(1, token, 100, 10)

    assert price == 2.1
    assert payload["rate_method"] == "convertToAssets(uint256)"


def test_wrapper_price_uses_normalized_get_rate(monkeypatch):
    token = "0x00000000000000000000000000000000000000e0"
    underlying = "0x00000000000000000000000000000000000000e1"

    monkeypatch.setattr(
        "yearn_data.pricing._call_address",
        lambda chain_id, address, signature, block_number, args_hex="": (
            underlying if address.lower() == token and signature == "asset()" else None
        ),
    )

    def fake_uint(chain_id, address, signature, block_number, args_hex=""):
        if address.lower() == token and signature == "decimals()":
            return 18
        if address.lower() == underlying and signature == "decimals()":
            return 6
        if address.lower() == token and signature == "getRate()":
            return 1_010_000_000_000_000_000
        return None

    monkeypatch.setattr("yearn_data.pricing._call_uint256", fake_uint)
    monkeypatch.setattr(
        "yearn_data.pricing._resolve_reserve_price",
        lambda *args: (1.0, {"source": "stable"}),
    )

    price, payload = _erc4626_wrapper_price(1, token, 100, 10)

    assert price == 1.01
    assert payload["rate_method"] == "getRate()"


def test_balancer_bpt_price_uses_actual_supply_for_phantom_pool(monkeypatch):
    bpt = "0x00000000000000000000000000000000000000f0"
    stable = "0x0000000000000000000000000000000000000001"
    pool_id = "0x" + "ab" * 32

    def fake_eth_call(chain_id, address, data, block_number, timeout=30):
        if address.lower() == bpt:
            return pool_id
        return "0x" + abi_encode(
            ["address[]", "uint256[]", "uint256"],
            ([stable, bpt], [100 * 10**6, 700 * 10**18], 1),
        ).hex()

    def fake_uint(chain_id, address, signature, block_number, args_hex=""):
        if address.lower() == bpt and signature == "decimals()":
            return 18
        if address.lower() == bpt and signature == "totalSupply()":
            return 10**30
        if address.lower() == bpt and signature == "getActualSupply()":
            return 100 * 10**18
        if address.lower() == stable and signature == "decimals()":
            return 6
        return None

    monkeypatch.setattr("yearn_data.pricing._eth_call", fake_eth_call)
    monkeypatch.setattr("yearn_data.pricing._call_uint256", fake_uint)
    monkeypatch.setattr(
        "yearn_data.pricing._resolve_reserve_price",
        lambda *args: (1.0, {"source": "stable"}),
    )

    price, payload = _balancer_bpt_price(1, bpt, 100, 10)

    assert price == 1.0
    assert payload["supply_method"] == "getActualSupply()"


def test_refresh_onchain_fallbacks_reprices_existing_ok_row(monkeypatch, tmp_path):
    conn = connect(tmp_path / "test.sqlite")
    init_db(conn)
    seed_chains(conn, CHAINS)
    token = "0x0000000000000000000000000000000000000002"
    conn.execute(
        """
        INSERT INTO strategy_reports (
            chain_id, version, vault_address, strategy_address, tx_hash, log_index,
            block_number, block_timestamp, asset, asset_decimals, gain_raw, loss_raw,
            net_raw, extra_json
        ) VALUES (1, 'v2', '0x0000000000000000000000000000000000000001',
                  '0x0000000000000000000000000000000000000003', '0xabc', 0,
                  10, 100, ?, 18, '1', '0', '1', '{}')
        """,
        (token,),
    )
    conn.execute(
        """
        INSERT INTO prices (chain_id, token_address, timestamp, block_number, source,
                            price_usd, status, raw_json)
        VALUES (1, ?, 100, 10, 'defillama', 0.5, 'ok',
                '{"source":"curve_lp_balance_fallback"}')
        """,
        (token,),
    )
    conn.commit()
    monkeypatch.setattr(
        "yearn_data.pricing.fetch_defillama_prices_batch",
        lambda requests_: {(1, token, 100): (2.0, "ok", {"source": "defillama"})},
    )

    count = price_unpriced_reports(conn, source="defillama", refresh_onchain_fallbacks=True)

    assert count == 1
    assert conn.execute("SELECT price_usd FROM prices").fetchone()["price_usd"] == 2.0


def test_price_unpriced_reports_uses_crv_derivative_fallback(monkeypatch, tmp_path):
    conn = connect(tmp_path / "test.sqlite")
    init_db(conn)
    seed_chains(conn, CHAINS)
    ycrv = "0xFCc5c47bE19d06BF83eB04298b026F81069ff65b"
    conn.execute(
        """
        INSERT INTO strategy_reports (
            chain_id, version, vault_address, strategy_address, tx_hash, log_index,
            block_number, block_timestamp, asset, asset_decimals, gain_raw, loss_raw,
            net_raw, extra_json
        )
        VALUES (1, 'v2', '0x0000000000000000000000000000000000000001',
                '0x0000000000000000000000000000000000000003',
                '0xabc', 0, 10, 100, ?, 18, '1', '0', '1', '{}')
        """,
        (ycrv,),
    )
    conn.execute(
        """
        INSERT INTO prices (
            chain_id, token_address, timestamp, block_number, source, price_usd,
            status, raw_json
        )
        VALUES (1, ?, 100, 10, 'defillama', NULL, 'missing', '{}')
        """,
        (ycrv,),
    )
    conn.commit()

    def fake_defillama_batch(requests_):
        return {
            (chain_id, token_address, timestamp): (None, "missing", {"source": "defillama"})
            for chain_id, token_address, timestamp in requests_
        }

    def fake_crv_derivative(chain_id, token_address, timestamp, block_number):
        return 0.42, {"source": "curve_get_dy_fallback"}

    monkeypatch.setattr("yearn_data.pricing.fetch_defillama_prices_batch", fake_defillama_batch)
    monkeypatch.setattr("yearn_data.pricing._crv_derivative_price", fake_crv_derivative)

    count = price_unpriced_reports(conn, source="defillama", fallback=None, retry_missing=True, chain_ids={1})

    assert count == 1
    row = conn.execute("SELECT price_usd, status, raw_json FROM prices").fetchone()
    assert row["price_usd"] == 0.42
    assert row["status"] == "ok"
    assert "curve_get_dy_fallback" in row["raw_json"]


@pytest.mark.parametrize("adapter", [_curve_lp_price, _balancer_bpt_price])
@pytest.mark.parametrize("reserve_decimals", [None, 0, 6])
def test_reserve_decimals_must_be_known(monkeypatch, adapter, reserve_decimals):
    lp = "0x00000000000000000000000000000000000000aa"
    reserve = "0x0000000000000000000000000000000000000001"
    balance = 100 * 10 ** (reserve_decimals if reserve_decimals is not None else 6)

    def fake_address(chain_id, address, signature, block_number, args_hex=""):
        if signature == "minter()":
            return lp
        if signature == "coins(uint256)" and int(args_hex, 16) == 0:
            return reserve
        return None

    def fake_uint(chain_id, address, signature, block_number, args_hex=""):
        if signature == "decimals()":
            return 18 if address.lower() == lp else reserve_decimals
        if signature == "totalSupply()":
            return 100 * 10**18
        if signature == "balances(uint256)":
            return balance
        return None

    def fake_eth_call(chain_id, address, data, block_number):
        if address.lower() == lp:
            return "0x" + "ab" * 32
        return "0x" + abi_encode(
            ["address[]", "uint256[]", "uint256"], ([reserve], [balance], 1)
        ).hex()

    monkeypatch.setattr("yearn_data.pricing._call_address", fake_address)
    monkeypatch.setattr("yearn_data.pricing._call_uint256", fake_uint)
    monkeypatch.setattr("yearn_data.pricing._eth_call", fake_eth_call)
    monkeypatch.setattr("yearn_data.pricing._resolve_reserve_price", lambda *args: (1.0, {}))
    _curve_lp_price.cache_clear()
    try:
        price, payload = adapter(1, lp, 100, 10)
        if reserve_decimals is None:
            assert price is None
            assert payload["missing_reserves"][0]["reason"] == "missing_decimals"
        else:
            assert price == 1.0
    finally:
        _curve_lp_price.cache_clear()


@pytest.mark.parametrize("balance_getter", ["balances(uint256)", "balances(int128)"])
def test_curve_zero_balance_does_not_require_valuation(monkeypatch, balance_getter):
    lp = "0x00000000000000000000000000000000000000aa"
    funded = "0x0000000000000000000000000000000000000001"
    empty = "0x0000000000000000000000000000000000000002"

    def fake_address(chain_id, address, signature, block_number, args_hex=""):
        if signature == "minter()":
            return lp
        if signature == "coins(uint256)":
            idx = int(args_hex, 16)
            return [funded, empty][idx] if idx < 2 else None
        return None

    def fake_uint(chain_id, address, signature, block_number, args_hex=""):
        if signature == "decimals()":
            assert address.lower() != empty
            return 18
        if signature == "totalSupply()":
            return 100 * 10**18
        if signature == balance_getter:
            return [100 * 10**18, 0][int(args_hex, 16)]
        return None

    def reserve_price(chain_id, address, *args):
        assert address.lower() == funded
        return 1.0, {}

    monkeypatch.setattr("yearn_data.pricing._call_address", fake_address)
    monkeypatch.setattr("yearn_data.pricing._call_uint256", fake_uint)
    monkeypatch.setattr("yearn_data.pricing._resolve_reserve_price", reserve_price)
    _curve_lp_price.cache_clear()
    try:
        price, _ = _curve_lp_price(1, lp, 100, 10)
        assert price == 1.0
    finally:
        _curve_lp_price.cache_clear()


def test_yearn_only_missing_price_never_calls_optional_adapters(tmp_path, monkeypatch):
    from yearn_data import pricing
    conn = connect(tmp_path / 'yearn-only.sqlite')
    init_db(conn)
    asset = '0x' + '2' * 40
    conn.execute('''INSERT INTO strategy_reports (
        chain_id,version,vault_address,strategy_address,tx_hash,log_index,
        block_number,block_timestamp,asset,asset_decimals,gain_raw,loss_raw,net_raw,extra_json)
        VALUES (1,'v3',?,?, '0xabc',0,10,100,?,6,'1','0','1','{}')''', (asset, asset, asset))
    monkeypatch.setattr(pricing, 'fetch_yearn_prices_batch', lambda _: {})
    monkeypatch.setattr(pricing, 'fetch_yearn_price', lambda *a: (None, 'missing', {}))
    def forbidden(*args, **kwargs):
        pytest.fail('Yearn-only pricing called an optional provider or adapter')
    monkeypatch.setattr(pricing, 'fetch_defillama_prices_batch', forbidden)
    monkeypatch.setattr(pricing, '_row_level_fallback_price', forbidden)
    assert pricing.price_unpriced_reports(conn, fallback=None) == 1
    assert tuple(conn.execute('SELECT source,status,price_usd FROM prices').fetchone()) == ('yearn-prices', 'missing', None)
