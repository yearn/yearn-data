"""Historical pricing adapters."""

from __future__ import annotations

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from decimal import Decimal
from functools import lru_cache
from math import isfinite
from typing import Any
import json
import os
import re
import time
from urllib.parse import quote

import requests
from eth_abi import decode as abi_decode
from eth_utils import function_signature_to_4byte_selector
from web3 import Web3

from .config import CHAINS
from .config import get_rpc_url
from .storage import to_json


DEFILLAMA_BASE = "https://coins.llama.fi"
DEFILLAMA_SEARCH_WIDTH_SECONDS = 12 * 60 * 60
DEFILLAMA_SEARCH_WIDTH = "12h"
YEARN_PRICES_DEFAULT_BASE = "https://prices.yearn.dev"
YEARN_PRICES_MAX_TOKEN_KEYS = 50
YEARN_PRICES_MAX_TIMESTAMPS_PER_TOKEN = 90
SUPPORTED_SOURCES = {"defillama", "yearn-prices"}
DEFAULT_PRICE_SOURCE = "yearn-prices"
DEFAULT_FALLBACK_PRICE_SOURCE = "defillama"
TOKEN_ADDRESS_PATTERN = re.compile(r"^0x[0-9a-fA-F]{40}$")
DEFILLAMA_RETRY_ATTEMPTS = 7
DEFILLAMA_RETRY_MAX_SECONDS = 30.0
ETHEREUM_WETH = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
POLYGON_STABLE_ALIASES = {
    "0x2791bca1f2de4661ed88a30c99a7a9449aa84174",  # bridged USDC
    "0x3c499c542cef5e3811e1192ce70d8cc03d5c3359",  # native USDC
    "0xc2132d05d31c914a87c6611c10748aeb04b58e8f",  # bridged USDT
    "0x8f3cf7ad23cd3cadbd9735aff958023239c6a063",  # DAI
}
POLYGON_WETH = "0x7ceb23fd6bc0add59e62ac25578270cff1b9f619"
ARBITRUM_STABLE_ALIASES = {
    "0xfd086bc7cd5c481dcc9c85ebe478a1c0b69fcbb9",  # bridged USDT / USDt0
}
ETHEREUM_STABLE_ALIASES = {
    "0x6b175474e89094c44da98b954eedeac495271d0f",  # DAI
    "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",  # USDC
    "0xdac17f958d2ee523a2206206994597c13d831ec7",  # USDT
    "0x57ab1ec28d129707052df4df418d58a2d46d5f51",  # sUSD
    "0x85e30b8b263bc64d94b827ed450f2edfee8579da",  # USDaf
}
KATANA_USDC_WRAPPERS = {
    "0x203a662b0bd271a6ed5a60edfbd04bfce608fd36",  # vbUSDC
}
KATANA_ETH_WRAPPERS = {
    "0xee7d8bcfb72bc1880d0cf19822eb0a2e6577ab62",  # vbETH
}
ETH_EQUIVALENTS = {
    ETHEREUM_WETH.lower(),
    "0xae7ab96520de3a18e5e111b5eaab095312d7fe84",  # stETH
    "0x5e74c9036fb86bd7ecdcb084a0673efc32ea31cb",  # sETH
    "0x9559aaa82d9649c7a7b220e7c461d2e74c9a3593",  # rETH
    "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
}
BTC_EQUIVALENTS = {
    "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599",  # WBTC
    "0x0316eb71485b0ab14103307bf65a021042c6d380",  # HBTC
    "0x8751d4196027d4e6da63716fa7786b5174f04c15",  # Curve sBTC
}
WBTC = "0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599"
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
LINK = "0x514910771AF9Ca656af840dff83E8264EcF986CA"
AAVE = "0x7Fc66500c84A76Ad7e9c93437bFc5Ac33E2DDaE9"
TOKEN_PRICE_ALIASES = {
    "0xbbc455cb4f1b9e4bfc4b73970d360c8f032efee6": (LINK, "canonical_slink_link_fallback"),  # sLINK
    "0x4da9b813057d04baef4e5800e36083717b4a0341": (AAVE, "canonical_saave_aave_fallback"),  # stkAAVE / saAAVE wrappers
}
CRV = "0xD533a949740bb3306d119CC777fa900bA034cd52"
YVECRV = "0xc5bddf9843308380375a611c18b50fb9341f502a"
YCRV = "0xfcc5c47be19d06bf83eb04298b026f81069ff65b"
SUSHI_WETH_YVECRV_PAIR = "0x10b47177e92ef9d5c6059055d92ddf6290848991"
OLD_CRV_YCRV_POOL = "0x453D92C7d4263201C69aACfaf589Ed14202d83a4"
CURVE_ADDRESS_PROVIDER = "0x0000000022D53366457F9d5E68Ec105046FC4383"
CRV_BASED_CURVE_LPS = {
    "0x99f5acc8ec2da2bc0771c32814eff52b712de1e5",  # yCRV-f
    "0xca0253a98d16e9c1e3614cafda19318ee69772d0",  # sdCRVlp-f
    "0x9d0464996170c6b9e75eed71c68b99ddedf279e8",  # cvxcrv-f
}
CURVE_LP_CHAINS = {1, 137, 8453, 42161}
CURVE_LEGACY_POOLS = {
    # Curve sUSD LP token -> pre-registry pool.
    "0xc25a3a3b969415c80451098fa907ec722572917f": "0xA5407eAE9Ba41422680e2e00537571bcC53efBfD",
}
BALANCER_VAULT = "0xBA12222222228d8Ba445958a75a0704d566BF2C8"
BALANCER_CHAINS = {1, 137, 8453, 42161}


def defillama_coin_id(chain_id: int, token_address: str) -> str:
    cfg = next(c for c in CHAINS.values() if c.chain_id == int(chain_id))
    return f"{cfg.defillama_slug}:{token_address.lower()}"


def normalize_yearn_price_timestamp(timestamp: int) -> int:
    """Normalize an event timestamp to the canonical Yearn Prices UTC day-end."""
    timestamp = int(timestamp)
    if timestamp < 0:
        raise ValueError("price timestamp must be non-negative")
    return timestamp // 86_400 * 86_400 + 86_399


def yearn_prices_token_key(chain_id: int, token_address: str) -> str:
    if not TOKEN_ADDRESS_PATTERN.fullmatch(token_address):
        raise ValueError(f"invalid token address {token_address!r}")
    # Canonical Yearn Prices namespaces for catalog chains outside fee defaults.
    additional_chains = {100: 'gnosis', 146: 'sonic', 80094: 'berachain', 4663: 'robinhood'}
    if int(chain_id) in additional_chains:
        return f"{additional_chains[int(chain_id)]}:{token_address.lower()}"
    try:
        cfg = next(c for c in CHAINS.values() if c.chain_id == int(chain_id))
    except StopIteration as error:
        raise ValueError(f"Yearn Prices does not support configured chain {chain_id}") from error
    return f"{cfg.defillama_slug}:{token_address.lower()}"


def _yearn_prices_config() -> tuple[str, str]:
    api_key = os.environ.get("YEARN_PRICE_PROD_KEY", "").strip()
    if not api_key:
        raise ValueError("missing Yearn Prices API key; set YEARN_PRICE_PROD_KEY")
    base_url = os.environ.get("YEARN_PRICE_PROD_BASE_URL", YEARN_PRICES_DEFAULT_BASE).strip().rstrip("/")
    if not base_url.startswith(("http://", "https://")):
        raise ValueError("YEARN_PRICE_PROD_BASE_URL must be an absolute HTTP(S) URL")
    return base_url, api_key


class YearnPricesAuthenticationError(RuntimeError):
    pass


class YearnPricesRequestError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


def _yearn_prices_get(
    path: str,
    *,
    params: dict[str, str] | None = None,
    timeout: int = 10,
    allow_not_found: bool = False,
) -> dict[str, Any] | None:
    base_url, api_key = _yearn_prices_config()
    attempts = max(1, int(os.environ.get("YEARN_PRICE_HTTP_ATTEMPTS", "3")))
    retry_base = max(0.0, float(os.environ.get("YEARN_PRICE_HTTP_RETRY_BASE_SECONDS", "0.25")))
    last_error: Exception | None = None
    last_retryable = True

    for attempt in range(attempts):
        response = None
        try:
            response = requests.get(
                f"{base_url}{path}",
                params=params,
                headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
                timeout=timeout,
            )
            if response.status_code in {401, 403}:
                raise YearnPricesAuthenticationError(
                    f"Yearn Prices authentication failed (HTTP {response.status_code})"
                )
            if allow_not_found and response.status_code == 404:
                return None
            if response.status_code == 408 or response.status_code == 429 or response.status_code >= 500:
                raise requests.HTTPError(f"Yearn Prices returned HTTP {response.status_code}", response=response)
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("Yearn Prices returned a non-object JSON response")
            return payload
        except YearnPricesAuthenticationError:
            raise
        except (requests.RequestException, ValueError) as error:
            last_error = error
            retryable = response is None or response.status_code in {408, 429} or response.status_code >= 500
            last_retryable = retryable
            if not retryable or attempt == attempts - 1:
                break
            retry_after = response.headers.get("retry-after") if response is not None else None
            try:
                delay = min(float(retry_after), 30.0) if retry_after is not None else retry_base * (2**attempt)
            except ValueError:
                delay = retry_base * (2**attempt)
            time.sleep(max(0.0, delay))

    raise YearnPricesRequestError(
        f"Yearn Prices request failed after {attempts} attempt(s)",
        retryable=last_retryable,
    ) from last_error


def _parse_yearn_price_point(
    raw_point: Any,
    *,
    expected_timestamp: int,
    symbol: Any,
    adapter: str,
) -> tuple[float, str, dict[str, Any]] | None:
    if not isinstance(raw_point, dict):
        raise ValueError("Yearn Prices returned a malformed price point")
    price = raw_point.get("price")
    if price == 0:
        return None
    if (
        raw_point.get("timestamp") != expected_timestamp
        or not isinstance(price, (int, float))
        or isinstance(price, bool)
        or not isfinite(float(price))
        or float(price) <= 0
        or not isinstance(raw_point.get("source"), str)
        or not raw_point["source"]
    ):
        raise ValueError("Yearn Prices returned a malformed price point")
    confidence = raw_point.get("confidence")
    if confidence is not None and (
        not isinstance(confidence, (int, float))
        or isinstance(confidence, bool)
        or not isfinite(float(confidence))
    ):
        raise ValueError("Yearn Prices returned malformed confidence metadata")
    payload = {
        "adapter": adapter,
        "confidence": confidence,
        "normalized_timestamp": expected_timestamp,
        "provider": "yearn-prices",
        "symbol": symbol if isinstance(symbol, str) else None,
        "upstream_source": raw_point["source"],
    }
    return float(price), "ok", payload


def fetch_yearn_prices_batch(
    targets: list[tuple[int, str, int]],
    timeout: int = 10,
) -> dict[tuple[int, str, int], tuple[float, str, dict[str, Any]]]:
    """Fetch normalized EOD targets from the Yearn Prices batch endpoint.

    Omitted targets are intentionally absent from the result. The caller verifies
    each omission against the exact endpoint before recording it as missing.
    """
    coins: dict[str, list[int]] = defaultdict(list)
    target_by_key: dict[tuple[str, int], tuple[int, str, int]] = {}
    for chain_id, token_address, timestamp in targets:
        normalized = normalize_yearn_price_timestamp(timestamp)
        token_key = yearn_prices_token_key(chain_id, token_address)
        key = (token_key, normalized)
        if key not in target_by_key:
            coins[token_key].append(normalized)
            target_by_key[key] = (int(chain_id), token_address.lower(), normalized)
    if len(coins) > YEARN_PRICES_MAX_TOKEN_KEYS or any(
        len(timestamps) > YEARN_PRICES_MAX_TIMESTAMPS_PER_TOKEN for timestamps in coins.values()
    ):
        raise ValueError("Yearn Prices batch exceeds the documented API limits")

    payload = _yearn_prices_get(
        "/api/prices/batchHistorical",
        params={"coins": json.dumps(coins, separators=(",", ":"), sort_keys=True)},
        timeout=timeout,
    )
    raw_coins = payload.get("coins") if payload is not None else None
    if not isinstance(raw_coins, dict):
        raise ValueError("Yearn Prices returned a malformed batch response")

    output: dict[tuple[int, str, int], tuple[float, str, dict[str, Any]]] = {}
    seen_targets: set[tuple[int, str, int]] = set()
    for raw_token_key, raw_coin in raw_coins.items():
        if not isinstance(raw_token_key, str) or not isinstance(raw_coin, dict):
            raise ValueError("Yearn Prices returned a malformed batch coin")
        token_key = raw_token_key.lower()
        prices = raw_coin.get("prices")
        if not isinstance(prices, list):
            raise ValueError("Yearn Prices returned malformed batch prices")
        for raw_point in prices:
            if not isinstance(raw_point, dict) or not isinstance(raw_point.get("timestamp"), int):
                raise ValueError("Yearn Prices returned a malformed batch price point")
            lookup = (token_key, raw_point["timestamp"])
            target = target_by_key.get(lookup)
            if target is None or target in seen_targets:
                raise ValueError("Yearn Prices returned an unexpected or duplicate batch price point")
            seen_targets.add(target)
            parsed = _parse_yearn_price_point(
                raw_point,
                expected_timestamp=target[2],
                symbol=raw_coin.get("symbol"),
                adapter="batchHistorical",
            )
            if parsed is not None:
                output[target] = parsed
    return output


def fetch_yearn_price(
    chain_id: int,
    token_address: str,
    timestamp: int,
    timeout: int = 10,
) -> tuple[float | None, str, dict[str, Any]]:
    normalized = normalize_yearn_price_timestamp(timestamp)
    token_key = yearn_prices_token_key(chain_id, token_address)
    try:
        payload = _yearn_prices_get(
            f"/api/prices/historical/{normalized}/{quote(token_key, safe=':')}",
            timeout=timeout,
            allow_not_found=True,
        )
    except YearnPricesRequestError as error:
        failure_class = "retryable" if error.retryable else "invalid"
        return None, failure_class, {
            "adapter": "historical",
            "failure_class": failure_class,
            "failure_reason": str(error),
            "normalized_timestamp": normalized,
            "provider": "yearn-prices",
        }
    if payload is None:
        return None, "missing", {
            "adapter": "historical",
            "failure_class": "not-found",
            "normalized_timestamp": normalized,
            "provider": "yearn-prices",
        }
    raw_coins = payload.get("coins")
    if not isinstance(raw_coins, dict) or len(raw_coins) != 1:
        raise ValueError("Yearn Prices returned a malformed exact response")
    raw_key, raw_point = next(iter(raw_coins.items()))
    if not isinstance(raw_key, str) or raw_key.lower() != token_key:
        raise ValueError("Yearn Prices returned an unexpected exact token key")
    parsed = _parse_yearn_price_point(
        raw_point,
        expected_timestamp=normalized,
        symbol=raw_point.get("symbol") if isinstance(raw_point, dict) else None,
        adapter="historical",
    )
    if parsed is None:
        return None, "missing", {
            "adapter": "historical",
            "failure_class": "not-found",
            "normalized_timestamp": normalized,
            "provider": "yearn-prices",
        }
    return parsed


def _accepted_defillama_sample(sample: Any, requested_timestamp: int) -> dict[str, Any] | None:
    if not isinstance(sample, dict):
        return None
    timestamp = sample.get("timestamp")
    price = sample.get("price")
    if (
        not isinstance(timestamp, (int, float))
        or isinstance(timestamp, bool)
        or not isfinite(float(timestamp))
        or not isinstance(price, (int, float))
        or isinstance(price, bool)
        or not isfinite(float(price))
        or float(price) <= 0
        or abs(int(timestamp) - int(requested_timestamp)) > DEFILLAMA_SEARCH_WIDTH_SECONDS
    ):
        return None
    return sample


def _match_defillama_samples(
    requested_timestamps: list[int],
    samples: list[Any],
) -> dict[int, dict[str, Any]]:
    """Match each request to its nearest valid observation within twelve hours."""
    requested = sorted(set(int(timestamp) for timestamp in requested_timestamps))
    valid_samples = [
        sample
        for sample in samples
        if isinstance(sample, dict)
        and isinstance(sample.get("timestamp"), (int, float))
        and not isinstance(sample.get("timestamp"), bool)
        and isfinite(float(sample["timestamp"]))
        and isinstance(sample.get("price"), (int, float))
        and not isinstance(sample.get("price"), bool)
        and isfinite(float(sample["price"]))
        and float(sample["price"]) > 0
    ]
    matched = {}
    for requested_timestamp in requested:
        candidates = [
            sample
            for sample in valid_samples
            if abs(int(sample["timestamp"]) - requested_timestamp) <= DEFILLAMA_SEARCH_WIDTH_SECONDS
        ]
        if candidates:
            matched[requested_timestamp] = min(
                candidates,
                key=lambda sample: abs(int(sample["timestamp"]) - requested_timestamp),
            )
    return matched


def _defillama_get(url: str, *, timeout: int, params: dict[str, str] | None = None):
    """Retry rate-limited DefiLlama requests with bounded backoff."""

    for attempt in range(DEFILLAMA_RETRY_ATTEMPTS):
        response = requests.get(url, params=params, timeout=timeout)
        if response.status_code != 429:
            response.raise_for_status()
            return response
        if attempt == DEFILLAMA_RETRY_ATTEMPTS - 1:
            response.raise_for_status()
        retry_after = response.headers.get("Retry-After")
        try:
            delay = float(retry_after) if retry_after is not None else 2**attempt
        except ValueError:
            delay = 2**attempt
        time.sleep(min(max(delay, 0.0), DEFILLAMA_RETRY_MAX_SECONDS))

    raise RuntimeError("unreachable DefiLlama retry state")


def fetch_defillama_price(chain_id: int, token_address: str, timestamp: int, timeout: int = 30) -> tuple[float | None, str, dict[str, Any]]:
    key = (int(chain_id), token_address, int(timestamp))
    return fetch_defillama_prices_batch([key], timeout=timeout).get(
        key,
        (
            None,
            "missing",
            {"coin": defillama_coin_id(chain_id, token_address), "timestamp": int(timestamp)},
        ),
    )


def fetch_defillama_prices_batch(
    requests_: list[tuple[int, str, int]],
    timeout: int = 30,
) -> dict[tuple[int, str, int], tuple[float | None, str, dict[str, Any]]]:
    coins: dict[str, list[int]] = {}
    key_by_coin: dict[str, tuple[int, str]] = {}
    for chain_id, token_address, timestamp in requests_:
        coin = defillama_coin_id(chain_id, token_address)
        coins.setdefault(coin, []).append(int(timestamp))
        key_by_coin[coin] = (int(chain_id), token_address)
    response = _defillama_get(
        f"{DEFILLAMA_BASE}/batchHistorical",
        params={"coins": json.dumps(coins, separators=(",", ":"))},
        timeout=timeout,
    )
    payload = response.json()
    output: dict[tuple[int, str, int], tuple[float | None, str, dict[str, Any]]] = {}
    missing_keys: list[tuple[int, str, int, str]] = []
    for coin, timestamps in coins.items():
        chain_id, token_address = key_by_coin[coin]
        prices = payload.get("coins", {}).get(coin, {}).get("prices") or []
        matched_prices = _match_defillama_samples(timestamps, prices)
        for requested_ts in timestamps:
            key = (chain_id, token_address, int(requested_ts))
            matched = matched_prices.get(int(requested_ts))
            if matched is None:
                missing_keys.append((chain_id, token_address, int(requested_ts), coin))
                continue
            output[key] = (
                float(matched["price"]),
                "ok",
                {
                    "coin": coin,
                    "timestamp": int(requested_ts),
                    "matched": matched,
                    "matched_offset_seconds": int(matched["timestamp"]) - int(requested_ts),
                    "maximum_accepted_offset_seconds": DEFILLAMA_SEARCH_WIDTH_SECONDS,
                },
            )
    output.update(_canonical_defillama_fallbacks_batch(missing_keys, timeout=timeout))
    return output


def _canonical_defillama_fallback(
    chain_id: int,
    token_address: str,
    timestamp: int,
    timeout: int = 30,
) -> tuple[float | None, str, dict[str, Any]] | None:
    address = token_address.lower()
    coin = defillama_coin_id(chain_id, token_address)
    stable_source = _canonical_stable_source(chain_id, address)
    if stable_source:
        return 1.0, "ok", {"coin": coin, "timestamp": int(timestamp), "source": stable_source}
    eth_source = _canonical_eth_source(chain_id, address)
    if eth_source:
        eth_coin = defillama_coin_id(1, ETHEREUM_WETH)
        url = f"{DEFILLAMA_BASE}/prices/historical/{int(timestamp)}/{eth_coin}"
        response = _defillama_get(url, params={"searchWidth": DEFILLAMA_SEARCH_WIDTH}, timeout=timeout)
        payload = response.json()
        entry = payload.get("coins", {}).get(eth_coin)
        if _accepted_defillama_sample(entry, timestamp) is not None:
            return (
                float(entry["price"]),
                "ok",
                {
                    "coin": coin,
                    "timestamp": int(timestamp),
                    "source": eth_source,
                    "fallback_coin": eth_coin,
                    "fallback_payload": payload,
                    "matched_offset_seconds": int(entry["timestamp"]) - int(timestamp),
                    "maximum_accepted_offset_seconds": DEFILLAMA_SEARCH_WIDTH_SECONDS,
                },
            )
    return None


def _canonical_defillama_fallbacks_batch(
    missing_keys: list[tuple[int, str, int, str]],
    timeout: int = 30,
) -> dict[tuple[int, str, int], tuple[float | None, str, dict[str, Any]]]:
    output: dict[tuple[int, str, int], tuple[float | None, str, dict[str, Any]]] = {}
    weth_timestamps: list[int] = []
    eth_keys: list[tuple[int, str, int, str, str]] = []
    for chain_id, token_address, timestamp, coin in missing_keys:
        key = (chain_id, token_address, timestamp)
        address = token_address.lower()
        stable_source = _canonical_stable_source(chain_id, address)
        eth_source = _canonical_eth_source(chain_id, address)
        if stable_source:
            output[key] = (1.0, "ok", {"coin": coin, "timestamp": timestamp, "source": stable_source})
        elif eth_source:
            weth_timestamps.append(timestamp)
            eth_keys.append((chain_id, token_address, timestamp, coin, eth_source))
        else:
            output[key] = (None, "missing", {"coin": coin, "timestamp": timestamp})

    if weth_timestamps:
        eth_coin = defillama_coin_id(1, ETHEREUM_WETH)
        response = _defillama_get(
            f"{DEFILLAMA_BASE}/batchHistorical",
            params={"coins": json.dumps({eth_coin: sorted(set(weth_timestamps))}, separators=(",", ":"))},
            timeout=timeout,
        )
        payload = response.json()
        prices = payload.get("coins", {}).get(eth_coin, {}).get("prices") or []
        matched_prices = _match_defillama_samples(weth_timestamps, prices)
        for chain_id, token_address, timestamp, coin, eth_source in eth_keys:
            key = (chain_id, token_address, timestamp)
            matched = matched_prices.get(timestamp)
            if matched is None:
                output[key] = (None, "missing", {"coin": coin, "timestamp": timestamp, "fallback_coin": eth_coin})
                continue
            output[key] = (
                float(matched["price"]),
                "ok",
                {
                    "coin": coin,
                    "timestamp": timestamp,
                    "source": eth_source,
                    "fallback_coin": eth_coin,
                    "matched": matched,
                    "matched_offset_seconds": int(matched["timestamp"]) - timestamp,
                    "maximum_accepted_offset_seconds": DEFILLAMA_SEARCH_WIDTH_SECONDS,
                },
            )
    return output


def _canonical_stable_source(chain_id: int, address: str) -> str | None:
    chain_id = int(chain_id)
    if chain_id == 137 and address in POLYGON_STABLE_ALIASES:
        return "canonical_polygon_stable_fallback"
    if chain_id == 42161 and address in ARBITRUM_STABLE_ALIASES:
        return "canonical_arbitrum_stable_fallback"
    if chain_id == 1 and address in ETHEREUM_STABLE_ALIASES:
        return "canonical_ethereum_stable_fallback"
    if chain_id == 747474 and address in KATANA_USDC_WRAPPERS:
        return "canonical_katana_usdc_wrapper_fallback"
    return None


def _canonical_eth_source(chain_id: int, address: str) -> str | None:
    chain_id = int(chain_id)
    if chain_id == 137 and address == POLYGON_WETH:
        return "canonical_polygon_weth_fallback"
    if chain_id == 747474 and address in KATANA_ETH_WRAPPERS:
        return "canonical_katana_eth_wrapper_fallback"
    return None


def report_amount(raw_value: str | int, decimals: int | None) -> Decimal:
    scale = Decimal(10) ** int(decimals or 18)
    return Decimal(int(raw_value)) / scale


def _selector(signature: str) -> str:
    return "0x" + function_signature_to_4byte_selector(signature).hex()


def _eth_call(chain_id: int, address: str, data: str, block_number: int, timeout: int = 30) -> str | None:
    chain = next(c.key for c in CHAINS.values() if c.chain_id == int(chain_id))
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_call",
        "params": [{"to": Web3.to_checksum_address(address), "data": data}, hex(int(block_number))],
    }
    response = requests.post(get_rpc_url(chain), json=payload, timeout=timeout)
    response.raise_for_status()
    body = response.json()
    result = body.get("result")
    if not isinstance(result, str) or result == "0x":
        return None
    return result


def _call_uint256(chain_id: int, address: str, signature: str, block_number: int, args_hex: str = "") -> int | None:
    try:
        result = _eth_call(chain_id, address, _selector(signature) + args_hex, block_number)
        if not result:
            return None
        return int(abi_decode(["uint256"], bytes.fromhex(result[2:]))[0])
    except Exception:
        return None


def _call_address(chain_id: int, address: str, signature: str, block_number: int, args_hex: str = "") -> str | None:
    try:
        result = _eth_call(chain_id, address, _selector(signature) + args_hex, block_number)
        if not result:
            return None
        decoded = abi_decode(["address"], bytes.fromhex(result[2:]))[0]
        if decoded == ZERO_ADDRESS:
            return None
        return Web3.to_checksum_address(decoded)
    except Exception:
        return None


def _uint_arg(value: int) -> str:
    return int(value).to_bytes(32, byteorder="big").hex()


@lru_cache(maxsize=100_000)
def _curve_pool_from_registry(chain_id: int, lp_token: str, block_number: int) -> str | None:
    if int(chain_id) != 1:
        return None
    lp = Web3.to_checksum_address(lp_token)
    for registry_id in range(10):
        registry = _call_address(chain_id, CURVE_ADDRESS_PROVIDER, "get_address(uint256)", block_number, _uint_arg(registry_id))
        if not registry:
            continue
        pool = _call_address(chain_id, registry, "get_pool_from_lp_token(address)", block_number, lp[2:].rjust(64, "0"))
        if pool:
            return pool
    return None


def _exchange_rate_wrapped_price(
    chain_id: int,
    token_address: str,
    timestamp: int,
    block_number: int,
) -> tuple[float | None, dict[str, Any]]:
    underlying = _call_address(chain_id, token_address, "underlying()", block_number)
    if not underlying:
        return None, {"source": "exchange_rate_wrapper_fallback", "status": "missing_underlying"}
    exchange_rate = _call_uint256(chain_id, token_address, "exchangeRateStored()", block_number)
    if exchange_rate is None:
        return None, {"source": "exchange_rate_wrapper_fallback", "status": "missing_exchange_rate", "underlying": underlying}
    token_decimals = _call_uint256(chain_id, token_address, "decimals()", block_number)
    underlying_decimals = _call_uint256(chain_id, underlying, "decimals()", block_number)
    if token_decimals is None or underlying_decimals is None:
        return None, {"source": "exchange_rate_wrapper_fallback", "status": "missing_decimals", "underlying": underlying}
    underlying_price, underlying_payload = _direct_or_alias_price(chain_id, underlying, timestamp, block_number)
    if underlying_price is None:
        return None, {"source": "exchange_rate_wrapper_fallback", "status": "missing_underlying_price", "underlying": underlying}
    scale = Decimal(10) ** (18 + int(underlying_decimals) - int(token_decimals))
    return (
        float((Decimal(int(exchange_rate)) / scale) * Decimal(str(underlying_price))),
        {
            "source": "exchange_rate_wrapper_fallback",
            "underlying": underlying,
            "exchange_rate": str(exchange_rate),
            "token_decimals": int(token_decimals),
            "underlying_decimals": int(underlying_decimals),
            "underlying_payload": underlying_payload,
        },
    )


def _yearn_v1_vault_price(
    chain_id: int,
    token_address: str,
    timestamp: int,
    block_number: int,
) -> tuple[float | None, dict[str, Any]]:
    """Value a Yearn V1 yToken from its underlying and historical share price."""

    underlying = _call_address(chain_id, token_address, "token()", block_number)
    if not underlying:
        return None, {"source": "yearn_v1_vault_fallback", "status": "missing_underlying"}
    price_per_share = _call_uint256(
        chain_id,
        token_address,
        "getPricePerFullShare()",
        block_number,
    )
    if price_per_share is None:
        return None, {
            "source": "yearn_v1_vault_fallback",
            "status": "missing_price_per_share",
            "underlying": underlying,
        }
    underlying_price, underlying_payload = _direct_or_alias_price(
        chain_id,
        underlying,
        timestamp,
        block_number,
    )
    if underlying_price is None:
        return None, {
            "source": "yearn_v1_vault_fallback",
            "status": "missing_underlying_price",
            "underlying": underlying,
            "underlying_payload": underlying_payload,
        }
    return (
        float((Decimal(price_per_share) / Decimal(10**18)) * Decimal(str(underlying_price))),
        {
            "source": "yearn_v1_vault_fallback",
            "underlying": underlying,
            "price_per_share": str(price_per_share),
            "underlying_payload": underlying_payload,
        },
    )


def _aave_atoken_price(
    chain_id: int,
    token_address: str,
    timestamp: int,
    block_number: int,
) -> tuple[float | None, dict[str, Any]]:
    underlying = _call_address(chain_id, token_address, "UNDERLYING_ASSET_ADDRESS()", block_number)
    if not underlying:
        return None, {"source": "aave_atoken_fallback", "status": "missing_underlying"}
    underlying_price, underlying_payload = _direct_or_alias_price(chain_id, underlying, timestamp, block_number)
    if underlying_price is None:
        return None, {"source": "aave_atoken_fallback", "status": "missing_underlying_price", "underlying": underlying}
    return (
        underlying_price,
        {
            "source": "aave_atoken_fallback",
            "underlying": underlying,
            "underlying_payload": underlying_payload,
        },
    )


@lru_cache(maxsize=200_000)
def _direct_or_alias_price(chain_id: int, token_address: str, timestamp: int, block_number: int) -> tuple[float | None, dict[str, Any]]:
    address = token_address.lower()
    stable_source = _canonical_stable_source(chain_id, address)
    if stable_source:
        return 1.0, {"source": stable_source}
    if int(chain_id) == 1 and address in TOKEN_PRICE_ALIASES:
        base_token, alias_source = TOKEN_PRICE_ALIASES[address]
        price, payload = _direct_or_alias_price(chain_id, base_token, timestamp, block_number)
        return price, {"source": alias_source, "base_token": base_token, "base_payload": payload}
    eth_source = _canonical_eth_source(chain_id, address)
    if eth_source or (int(chain_id) == 1 and address in ETH_EQUIVALENTS):
        price, status, payload = fetch_defillama_price(1, ETHEREUM_WETH, timestamp)
        return (price if status == "ok" else None), {"source": eth_source or "canonical_eth_equivalent_fallback", "payload": payload}
    if int(chain_id) == 1 and address in BTC_EQUIVALENTS:
        price, status, payload = fetch_defillama_price(1, WBTC, timestamp)
        return (price if status == "ok" else None), {"source": "canonical_btc_equivalent_fallback", "payload": payload}
    price, status, payload = fetch_defillama_price(chain_id, token_address, timestamp)
    if status == "ok":
        return price, {"source": "defillama_underlying", "payload": payload}
    if int(chain_id) == 1:
        wrapped_price, wrapped_payload = _exchange_rate_wrapped_price(chain_id, token_address, timestamp, block_number)
        if wrapped_price is not None:
            return wrapped_price, wrapped_payload
        atoken_price, atoken_payload = _aave_atoken_price(chain_id, token_address, timestamp, block_number)
        if atoken_price is not None:
            return atoken_price, atoken_payload
        yearn_v1_price, yearn_v1_payload = _yearn_v1_vault_price(
            chain_id,
            token_address,
            timestamp,
            block_number,
        )
        if yearn_v1_price is not None:
            return yearn_v1_price, yearn_v1_payload
    return None, {"source": "missing_underlying", "payload": payload}


@lru_cache(maxsize=200_000)
def _curve_lp_price(
    chain_id: int,
    token_address: str,
    timestamp: int,
    block_number: int,
    depth: int = 3,
) -> tuple[float | None, dict[str, Any]]:
    """Value a Curve LP from historical reserves, failing closed on gaps."""

    if int(chain_id) not in CURVE_LP_CHAINS or depth < 0:
        return None, {"source": "curve_lp_fallback", "status": "unsupported"}

    token = Web3.to_checksum_address(token_address)
    pool = (
        _call_address(chain_id, token, "minter()", block_number)
        or CURVE_LEGACY_POOLS.get(token_address.lower())
        or _curve_pool_from_registry(chain_id, token, block_number)
        or token
    )
    token_decimals = _call_uint256(chain_id, token, "decimals()", block_number)
    total_supply = _call_uint256(chain_id, token, "totalSupply()", block_number)
    if token_decimals is None or not total_supply:
        return None, {"source": "curve_lp_fallback", "status": "missing_supply", "pool": pool}

    coins: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    for idx in range(8):
        coin = (
            _call_address(chain_id, pool, "coins(uint256)", block_number, _uint_arg(idx))
            or _call_address(chain_id, pool, "coins(int128)", block_number, _uint_arg(idx))
        )
        if not coin:
            break
        balance = _call_uint256(chain_id, pool, "balances(uint256)", block_number, _uint_arg(idx))
        if balance is None:
            balance = _call_uint256(chain_id, pool, "balances(int128)", block_number, _uint_arg(idx))
        if balance is None:
            missing.append({"coin": coin, "balance": None, "reason": "missing_balance"})
            continue
        if balance == 0:
            continue
        decimals = _call_uint256(chain_id, coin, "decimals()", block_number)
        if decimals is None:
            missing.append({"coin": coin, "balance": str(balance), "reason": "missing_decimals"})
            continue
        price, price_payload = _resolve_reserve_price(
            chain_id,
            coin,
            timestamp,
            block_number,
            depth - 1,
        )
        if price is None:
            missing.append(
                {
                    "coin": coin,
                    "balance": str(balance),
                    "decimals": int(decimals),
                    "reason": "missing_price",
                    "price_payload": price_payload,
                }
            )
            continue
        amount = Decimal(int(balance)) / (Decimal(10) ** int(decimals))
        coins.append(
            {
                "coin": coin,
                "balance": str(balance),
                "decimals": int(decimals),
                "price": float(price),
                "value_usd": str(amount * Decimal(str(price))),
                "price_payload": price_payload,
            }
        )

    if missing:
        return None, {
            "source": "curve_lp_fallback",
            "status": "missing_reserve_price",
            "pool": pool,
            "block_number": int(block_number),
            "missing_reserves": missing,
            "priced_coins": coins,
        }
    if not coins:
        return None, {"source": "curve_lp_fallback", "status": "no_valued_reserves", "pool": pool}

    tvl = sum(Decimal(item["value_usd"]) for item in coins)
    supply = Decimal(total_supply) / (Decimal(10) ** int(token_decimals))
    if supply <= 0 or tvl <= 0:
        return None, {"source": "curve_lp_fallback", "status": "zero_valued_pool", "pool": pool}
    return (
        float(tvl / supply),
        {
            "source": "curve_lp_balance_fallback",
            "pool": pool,
            "token": token,
            "total_supply": str(total_supply),
            "token_decimals": int(token_decimals),
            "coins": coins,
        },
    )


def _erc4626_wrapper_price(
    chain_id: int,
    token_address: str,
    timestamp: int,
    block_number: int,
    depth: int = 3,
) -> tuple[float | None, dict[str, Any]]:
    """Value an ERC-4626-style share token from assets per whole share."""

    if depth < 0:
        return None, {"source": "erc4626_wrapper_fallback", "status": "unsupported"}
    token = Web3.to_checksum_address(token_address)
    if _call_address(chain_id, token, "SY()", block_number):
        return None, {
            "source": "erc4626_wrapper_fallback",
            "status": "pendle_principal_token_excluded",
        }
    token_decimals = _call_uint256(chain_id, token, "decimals()", block_number)
    underlying = _call_address(chain_id, token, "asset()", block_number)
    if token_decimals is None or not underlying or underlying.lower() == token_address.lower():
        return None, {"source": "erc4626_wrapper_fallback", "status": "missing_asset"}
    underlying_decimals = _call_uint256(chain_id, underlying, "decimals()", block_number)
    if underlying_decimals is None:
        return None, {
            "source": "erc4626_wrapper_fallback",
            "status": "missing_underlying_decimals",
            "underlying": underlying,
        }
    one_share = 10 ** int(token_decimals)
    assets = _call_uint256(
        chain_id,
        token,
        "convertToAssets(uint256)",
        block_number,
        _uint_arg(one_share),
    )
    rate_method = "convertToAssets(uint256)"
    rate_scale = Decimal(10) ** int(underlying_decimals)
    if assets is None and int(token_decimals) == 18:
        assets = _call_uint256(chain_id, token, "getRate()", block_number)
        rate_method = "getRate()"
        rate_scale = Decimal(10**18)
    if assets is None:
        return None, {
            "source": "erc4626_wrapper_fallback",
            "status": "missing_rate",
            "underlying": underlying,
        }
    underlying_price, underlying_payload = _resolve_reserve_price(
        chain_id,
        underlying,
        timestamp,
        block_number,
        depth - 1,
    )
    if underlying_price is None:
        return None, {
            "source": "erc4626_wrapper_fallback",
            "status": "missing_underlying_price",
            "underlying": underlying,
            "underlying_payload": underlying_payload,
        }
    assets_per_share = Decimal(assets) / rate_scale
    return (
        float(assets_per_share * Decimal(str(underlying_price))),
        {
            "source": "erc4626_wrapper_fallback",
            "underlying": underlying,
            "rate_method": rate_method,
            "assets": str(assets),
            "assets_per_share": format(assets_per_share, "f"),
            "token_decimals": int(token_decimals),
            "underlying_decimals": int(underlying_decimals),
            "underlying_payload": underlying_payload,
        },
    )


def _balancer_pool_supply(
    chain_id: int,
    token: str,
    block_number: int,
    total_supply: int,
    phantom_balance: int | None,
) -> tuple[int | None, str]:
    """Return circulating BPT supply for regular and preminted pools."""

    if phantom_balance is None:
        return total_supply, "totalSupply()"
    actual_supply = _call_uint256(chain_id, token, "getActualSupply()", block_number)
    if actual_supply:
        return actual_supply, "getActualSupply()"
    virtual_supply = _call_uint256(chain_id, token, "getVirtualSupply()", block_number)
    if virtual_supply:
        return virtual_supply, "getVirtualSupply()"
    circulating = total_supply - phantom_balance
    return (circulating if circulating > 0 else None), "totalSupply()-phantomBalance"


def _balancer_bpt_price(
    chain_id: int,
    token_address: str,
    timestamp: int,
    block_number: int,
    depth: int = 3,
) -> tuple[float | None, dict[str, Any]]:
    """Value a Balancer BPT from Vault reserves and circulating supply."""

    if int(chain_id) not in BALANCER_CHAINS or depth < 0:
        return None, {"source": "balancer_bpt_fallback", "status": "unsupported"}
    token = Web3.to_checksum_address(token_address)
    try:
        pool_id = _eth_call(chain_id, token, _selector("getPoolId()"), block_number)
        if not pool_id:
            return None, {"source": "balancer_bpt_fallback", "status": "missing_pool_id"}
        result = _eth_call(
            chain_id,
            BALANCER_VAULT,
            _selector("getPoolTokens(bytes32)") + pool_id[2:].zfill(64),
            block_number,
        )
        if not result:
            return None, {"source": "balancer_bpt_fallback", "status": "missing_pool_tokens"}
        reserves, balances, _ = abi_decode(
            ["address[]", "uint256[]", "uint256"],
            bytes.fromhex(result[2:]),
        )
    except Exception as exc:
        return None, {"source": "balancer_bpt_fallback", "status": "error", "error": str(exc)}

    token_decimals = _call_uint256(chain_id, token, "decimals()", block_number)
    total_supply = _call_uint256(chain_id, token, "totalSupply()", block_number)
    if token_decimals is None or not total_supply:
        return None, {"source": "balancer_bpt_fallback", "status": "missing_supply", "pool_id": pool_id}

    phantom_balance = next(
        (int(balance) for reserve, balance in zip(reserves, balances) if reserve.lower() == token_address.lower()),
        None,
    )
    supply_raw, supply_method = _balancer_pool_supply(
        chain_id,
        token,
        block_number,
        total_supply,
        phantom_balance,
    )
    if not supply_raw:
        return None, {"source": "balancer_bpt_fallback", "status": "missing_circulating_supply", "pool_id": pool_id}

    value = Decimal(0)
    priced_reserves: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    for reserve, balance in zip(reserves, balances):
        if reserve.lower() == token_address.lower():
            priced_reserves.append({"coin": reserve, "balance": str(balance), "skipped": "phantom_bpt"})
            continue
        if balance == 0:
            continue
        price, price_payload = _resolve_reserve_price(
            chain_id,
            reserve,
            timestamp,
            block_number,
            depth - 1,
        )
        if price is None:
            missing.append(
                {
                    "coin": reserve,
                    "balance": str(balance),
                    "reason": "missing_price",
                    "price_payload": price_payload,
                }
            )
            continue
        decimals = _call_uint256(chain_id, reserve, "decimals()", block_number)
        if decimals is None:
            missing.append({"coin": reserve, "balance": str(balance), "reason": "missing_decimals"})
            continue
        amount = Decimal(balance) / (Decimal(10) ** int(decimals))
        reserve_value = amount * Decimal(str(price))
        value += reserve_value
        priced_reserves.append(
            {
                "coin": reserve,
                "balance": str(balance),
                "decimals": int(decimals),
                "price": float(price),
                "value_usd": str(reserve_value),
                "price_payload": price_payload,
            }
        )
    if missing:
        return None, {
            "source": "balancer_bpt_fallback",
            "status": "missing_reserve_price",
            "pool_id": pool_id,
            "missing_reserves": missing,
            "priced_reserves": priced_reserves,
        }
    supply = Decimal(supply_raw) / (Decimal(10) ** int(token_decimals))
    if supply <= 0 or value <= 0:
        return None, {"source": "balancer_bpt_fallback", "status": "zero_valued_pool", "pool_id": pool_id}
    return (
        float(value / supply),
        {
            "source": "balancer_bpt_fallback",
            "pool_id": pool_id,
            "token": token,
            "supply": str(supply_raw),
            "supply_method": supply_method,
            "token_decimals": int(token_decimals),
            "reserves": priced_reserves,
        },
    )


def _resolve_reserve_price(
    chain_id: int,
    token_address: str,
    timestamp: int,
    block_number: int,
    depth: int,
) -> tuple[float | None, dict[str, Any]]:
    """Resolve direct and nested reserve prices with bounded recursion."""

    if depth < 0:
        return None, {"source": "reserve_resolver", "status": "depth_exhausted"}
    price, payload = _direct_or_alias_price(chain_id, token_address, timestamp, block_number)
    if price is not None:
        return price, payload
    attempts: list[dict[str, Any]] = []
    for adapter in (_erc4626_wrapper_price, _curve_lp_price, _balancer_bpt_price):
        try:
            price, payload = adapter(chain_id, token_address, timestamp, block_number, depth)
        except Exception as exc:
            attempts.append({"adapter": adapter.__name__, "error": str(exc)})
            continue
        if price is not None:
            return price, payload
        attempts.append(payload)
    return None, {"source": "reserve_resolver", "status": "unresolved", "attempts": attempts}


def _uniswap_v2_pair_price(
    chain_id: int,
    pair: str,
    token: str,
    base_token: str,
    timestamp: int,
    block_number: int,
) -> tuple[float | None, dict[str, Any]]:
    token0 = _call_address(chain_id, pair, "token0()", block_number)
    token1 = _call_address(chain_id, pair, "token1()", block_number)
    try:
        result = _eth_call(chain_id, pair, _selector("getReserves()"), block_number)
        if not result:
            return None, {"source": "uniswap_v2_pair_fallback", "status": "missing_reserves", "pair": pair}
        reserve0, reserve1, _ = abi_decode(["uint112", "uint112", "uint32"], bytes.fromhex(result[2:]))
    except Exception as exc:
        return None, {"source": "uniswap_v2_pair_fallback", "status": "error", "pair": pair, "error": str(exc)}
    if not token0 or not token1 or not reserve0 or not reserve1:
        return None, {"source": "uniswap_v2_pair_fallback", "status": "empty_reserves", "pair": pair}
    base_price, base_payload = _direct_or_alias_price(chain_id, base_token, timestamp, block_number)
    if base_price is None:
        return None, {"source": "uniswap_v2_pair_fallback", "status": "missing_base_price", "pair": pair}
    if token0.lower() == base_token.lower() and token1.lower() == token.lower():
        ratio = Decimal(int(reserve0)) / Decimal(int(reserve1))
    elif token1.lower() == base_token.lower() and token0.lower() == token.lower():
        ratio = Decimal(int(reserve1)) / Decimal(int(reserve0))
    else:
        return None, {"source": "uniswap_v2_pair_fallback", "status": "pair_mismatch", "pair": pair}
    return (
        float(ratio * Decimal(str(base_price))),
        {
            "source": "uniswap_v2_pair_fallback",
            "pair": pair,
            "base_token": base_token,
            "base_payload": base_payload,
            "reserve0": str(reserve0),
            "reserve1": str(reserve1),
            "token0": token0,
            "token1": token1,
        },
    )


def _curve_get_dy_price(
    chain_id: int,
    pool: str,
    token_index: int,
    base_index: int,
    base_token: str,
    timestamp: int,
    block_number: int,
) -> tuple[float | None, dict[str, Any]]:
    args = _uint_arg(token_index) + _uint_arg(base_index) + _uint_arg(10**18)
    amount = _call_uint256(chain_id, pool, "get_dy(int128,int128,uint256)", block_number, args)
    if amount is None:
        amount = _call_uint256(chain_id, pool, "get_dy(uint256,uint256,uint256)", block_number, args)
    if amount is None:
        return None, {"source": "curve_get_dy_fallback", "status": "missing_get_dy", "pool": pool}
    base_price, base_payload = _direct_or_alias_price(chain_id, base_token, timestamp, block_number)
    if base_price is None:
        return None, {"source": "curve_get_dy_fallback", "status": "missing_base_price", "pool": pool}
    price = (Decimal(int(amount)) / Decimal(10**18)) * Decimal(str(base_price))
    return (
        float(price),
        {
            "source": "curve_get_dy_fallback",
            "pool": pool,
            "amount_out": str(amount),
            "base_token": base_token,
            "base_payload": base_payload,
        },
    )


def _crv_derivative_price(chain_id: int, token_address: str, timestamp: int, block_number: int) -> tuple[float | None, dict[str, Any]]:
    address = token_address.lower()
    if int(chain_id) != 1:
        return None, {"source": "crv_derivative_fallback", "status": "unsupported_chain"}
    if address == YVECRV:
        return _uniswap_v2_pair_price(chain_id, SUSHI_WETH_YVECRV_PAIR, token_address, ETHEREUM_WETH, timestamp, block_number)
    if address == YCRV:
        price, payload = _curve_get_dy_price(chain_id, OLD_CRV_YCRV_POOL, 1, 0, CRV, timestamp, block_number)
        if price is not None:
            return price, payload
        crv_price, crv_payload = _direct_or_alias_price(chain_id, CRV, timestamp, block_number)
        if crv_price is not None:
            return (
                crv_price,
                {
                    "source": "ycrv_crv_par_fallback",
                    "reason": "CRV/yCRV pool unavailable at block",
                    "crv_payload": crv_payload,
                    "pool_payload": payload,
                },
            )
    if address in CRV_BASED_CURVE_LPS:
        virtual_price = _call_uint256(chain_id, token_address, "get_virtual_price()", block_number)
        crv_price, crv_payload = _direct_or_alias_price(chain_id, CRV, timestamp, block_number)
        if virtual_price and crv_price is not None:
            return (
                float((Decimal(int(virtual_price)) / Decimal(10**18)) * Decimal(str(crv_price))),
                {
                    "source": "crv_derivative_curve_lp_fallback",
                    "virtual_price": str(virtual_price),
                    "base_token": CRV,
                    "base_payload": crv_payload,
                },
            )
    return None, {"source": "crv_derivative_fallback", "status": "missing"}


def _row_level_fallback_price(chain_id: int, token_address: str, timestamp: int, block_number: int) -> tuple[float | None, str, dict[str, Any]]:
    canonical = _canonical_defillama_fallback(chain_id, token_address, timestamp)
    if canonical is not None:
        price, status, payload = canonical
        return price, status, payload
    price, payload = _crv_derivative_price(chain_id, token_address, timestamp, block_number)
    if price is not None:
        return price, "ok", payload
    attempts: list[dict[str, Any]] = []
    for adapter in (_erc4626_wrapper_price, _curve_lp_price, _balancer_bpt_price):
        try:
            price, payload = adapter(chain_id, token_address, timestamp, block_number)
        except Exception as exc:
            attempts.append({"adapter": adapter.__name__, "error": str(exc)})
            continue
        if price is not None:
            return price, "ok", payload
        attempts.append(payload)
    return None, "missing", {"source": "row_fallback", "status": "unresolved", "attempts": attempts}


def _safe_row_level_fallback_price(
    chain_id: int,
    token_address: str,
    timestamp: int,
    block_number: int,
) -> tuple[float | None, str, dict[str, Any]]:
    try:
        return _row_level_fallback_price(chain_id, token_address, timestamp, block_number)
    except Exception as exc:
        return None, "missing", {
            "source": "onchain_fallback",
            "status": "error",
            "error": str(exc),
        }


def _existing_price_filter(retry_missing: bool, refresh_onchain_fallbacks: bool) -> str:
    if refresh_onchain_fallbacks:
        return "AND (p.token_address IS NULL OR p.status != 'ok' OR p.raw_json LIKE '%fallback%')"
    if retry_missing:
        return "AND (p.token_address IS NULL OR p.status != 'ok')"
    return "AND p.token_address IS NULL"


def _fetch_price(source: str, chain_id: int, token_address: str, timestamp: int, block_number: int):
    if source == "defillama":
        return fetch_defillama_price(chain_id, token_address, timestamp)
    if source == "yearn-prices":
        return fetch_yearn_price(chain_id, token_address, timestamp)
    raise ValueError(f"unsupported price source {source!r}; expected {sorted(SUPPORTED_SOURCES)}")


def price_unpriced_reports(
    conn,
    limit: int | None = None,
    source: str = DEFAULT_PRICE_SOURCE,
    fallback: str | None = DEFAULT_FALLBACK_PRICE_SOURCE,
    retry_missing: bool = False,
    refresh_onchain_fallbacks: bool = False,
    chain_ids: set[int] | None = None,
    onchain_fallbacks: bool = True,
) -> int:
    if source not in SUPPORTED_SOURCES:
        raise ValueError(f"unsupported price source {source!r}")
    if fallback is not None and fallback not in SUPPORTED_SOURCES:
        raise ValueError(f"unsupported fallback source {fallback!r}")
    chain_filter = ""
    chain_params: list[int] = []
    if chain_ids:
        chain_filter = f"AND r.chain_id IN ({','.join('?' for _ in chain_ids)})"
        chain_params = sorted(chain_ids)
    price_filter = _existing_price_filter(retry_missing, refresh_onchain_fallbacks)
    sql = f"""
    SELECT
        r.chain_id,
        r.asset,
        r.block_timestamp,
        MIN(r.block_number) AS block_number
    FROM strategy_reports r
    LEFT JOIN prices p
      ON p.chain_id = r.chain_id
     AND p.token_address = r.asset
     AND p.timestamp = r.block_timestamp
     AND p.source = ?
    WHERE r.asset IS NOT NULL
      {chain_filter}
      {price_filter}
    GROUP BY r.chain_id, r.asset, r.block_timestamp
    ORDER BY r.block_timestamp
    """
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = conn.execute(sql, (source, *chain_params)).fetchall()
    count = 0
    if source == "defillama":
        count += _price_unpriced_reports_defillama_batched(conn, rows, fallback, onchain_fallbacks=onchain_fallbacks)
        return count
    if source == "yearn-prices":
        count, unresolved_rows = _price_rows_with_yearn_prices(conn, rows)
        if fallback == "defillama":
            count += _price_unpriced_reports_defillama_batched(
                conn,
                unresolved_rows,
                fallback=None,
                onchain_fallbacks=onchain_fallbacks,
            )
        return count

    for row in rows:
        sources = [source]
        if fallback and fallback != source:
            sources.append(fallback)
        for price_source in sources:
            price, status, payload = _fetch_price(
                price_source,
                int(row["chain_id"]),
                row["asset"],
                int(row["block_timestamp"]),
                int(row["block_number"]),
            )
            conn.execute(
                """
                    INSERT INTO prices (
                        chain_id, token_address, timestamp, block_number,
                        source, price_usd, status, raw_json
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(chain_id,token_address,timestamp,source) DO UPDATE SET
                        block_number=excluded.block_number,
                        price_usd=excluded.price_usd,
                        status=excluded.status,
                        raw_json=excluded.raw_json
                """,
                (
                    int(row["chain_id"]),
                    row["asset"],
                    int(row["block_timestamp"]),
                    int(row["block_number"]),
                    price_source,
                    price,
                    status,
                    to_json(payload),
                ),
            )
            count += 1
            if status == "ok":
                break
        if count % 100 == 0:
            conn.commit()
            time.sleep(0.2)
    conn.commit()
    return count


def price_unpriced_volume(
    conn,
    limit: int | None = None,
    source: str = "defillama",
    fallback: str | None = None,
    retry_missing: bool = False,
    refresh_onchain_fallbacks: bool = False,
    chain_ids: set[int] | None = None,
    onchain_fallbacks: bool = True,
) -> int:
    if source not in SUPPORTED_SOURCES:
        raise ValueError(f"unsupported price source {source!r}")
    if fallback is not None and fallback not in SUPPORTED_SOURCES:
        raise ValueError(f"unsupported fallback source {fallback!r}")
    rows = _unpriced_volume_rows(
        conn,
        source,
        limit,
        retry_missing=retry_missing,
        refresh_onchain_fallbacks=refresh_onchain_fallbacks,
        chain_ids=chain_ids,
    )
    count = 0
    if source == "defillama":
        count += _price_unpriced_reports_defillama_batched(conn, rows, fallback, onchain_fallbacks=onchain_fallbacks)
        return count
    if source == "yearn-prices":
        count, _ = _price_rows_with_yearn_prices(conn, rows)
        return count

    for row in rows:
        sources = [source]
        if fallback and fallback != source:
            sources.append(fallback)
        for price_source in sources:
            price, status, payload = _fetch_price(
                price_source,
                int(row["chain_id"]),
                row["asset"],
                int(row["block_timestamp"]),
                int(row["block_number"]),
            )
            conn.execute(
                """
                    INSERT INTO prices (
                        chain_id, token_address, timestamp, block_number,
                        source, price_usd, status, raw_json
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(chain_id,token_address,timestamp,source) DO UPDATE SET
                        block_number=excluded.block_number,
                        price_usd=excluded.price_usd,
                        status=excluded.status,
                        raw_json=excluded.raw_json
                """,
                (
                    int(row["chain_id"]),
                    row["asset"],
                    int(row["block_timestamp"]),
                    int(row["block_number"]),
                    price_source,
                    price,
                    status,
                    to_json(payload),
                ),
            )
            count += 1
            if status == "ok":
                break
        if count % 100 == 0:
            conn.commit()
            time.sleep(0.2)
    conn.commit()
    return count


def _unpriced_volume_rows(
    conn,
    source: str,
    limit: int | None = None,
    retry_missing: bool = False,
    refresh_onchain_fallbacks: bool = False,
    chain_ids: set[int] | None = None,
) -> list[dict[str, int | str]]:
    rows_by_key: dict[tuple[int, str, int], dict[str, int | str]] = {}
    chain_filter = ""
    chain_params: list[int] = []
    if chain_ids:
        chain_filter = f"AND f.chain_id IN ({','.join('?' for _ in chain_ids)})"
        chain_params = sorted(chain_ids)
    price_filter = _existing_price_filter(retry_missing, refresh_onchain_fallbacks)
    for table in ("vault_flows", "strategy_debt_flows"):
        table_rows = conn.execute(
            f"""
            SELECT
                f.chain_id,
                f.asset,
                f.block_timestamp,
                MIN(f.block_number) AS block_number
            FROM {table} f
            LEFT JOIN prices p
              ON p.chain_id = f.chain_id
             AND p.token_address = f.asset
             AND p.timestamp = f.block_timestamp
             AND p.source = ?
            WHERE f.asset IS NOT NULL
              {chain_filter}
              {price_filter}
            GROUP BY f.chain_id, f.asset, f.block_timestamp
            ORDER BY f.block_timestamp
            """,
            (source, *chain_params),
        ).fetchall()
        for row in table_rows:
            key = (int(row["chain_id"]), row["asset"], int(row["block_timestamp"]))
            existing = rows_by_key.get(key)
            if existing is None or int(row["block_number"]) < int(existing["block_number"]):
                rows_by_key[key] = {
                    "chain_id": int(row["chain_id"]),
                    "asset": row["asset"],
                    "block_timestamp": int(row["block_timestamp"]),
                    "block_number": int(row["block_number"]),
                }
    rows = sorted(rows_by_key.values(), key=lambda row: int(row["block_timestamp"]))
    if limit:
        return rows[: int(limit)]
    return rows


def _chunks(rows, size: int):
    for i in range(0, len(rows), size):
        yield rows[i : i + size]


def _price_unpriced_reports_defillama_batched(
    conn,
    rows,
    fallback: str | None,
    onchain_fallbacks: bool = True,
) -> int:
    count = 0
    batches = list(_defillama_coin_batches(rows))
    workers = max(1, int(os.environ.get("YEARN_DATA_PRICE_WORKERS", "8")))
    with (
        ThreadPoolExecutor(max_workers=workers) as fetch_executor,
        ThreadPoolExecutor(max_workers=workers) as fallback_executor,
    ):
        futures = [fetch_executor.submit(_fetch_defillama_batch, batch) for batch in batches]
        for future in as_completed(futures):
            batch, results = future.result()
            fallback_futures = {}
            for row in batch:
                key = (int(row["chain_id"]), row["asset"], int(row["block_timestamp"]))
                if onchain_fallbacks and results.get(key, (None, "missing", {}))[1] != "ok":
                    fallback_future = fallback_executor.submit(
                        _safe_row_level_fallback_price,
                        int(row["chain_id"]),
                        row["asset"],
                        int(row["block_timestamp"]),
                        int(row["block_number"]),
                    )
                    fallback_futures[fallback_future] = row
                    continue
                count += _write_defillama_price_row(
                    conn,
                    row,
                    results,
                    fallback,
                    onchain_fallbacks=onchain_fallbacks,
                )
            conn.commit()
            for fallback_future in as_completed(fallback_futures):
                count += _write_defillama_price_row(
                    conn,
                    fallback_futures[fallback_future],
                    results,
                    fallback,
                    onchain_fallbacks=onchain_fallbacks,
                    fallback_result=fallback_future.result(),
                )
                if count % 100 == 0:
                    conn.commit()
            conn.commit()
    conn.commit()
    return count


def _fetch_defillama_batch(batch):
    requests_ = [
        (int(row["chain_id"]), row["asset"], int(row["block_timestamp"]))
        for row in batch
    ]
    try:
        return batch, fetch_defillama_prices_batch(requests_)
    except Exception:
        results = {}
        for chain_id, token_address, timestamp in requests_:
            price, status, payload = fetch_defillama_price(chain_id, token_address, timestamp)
            results[(chain_id, token_address, timestamp)] = (price, status, payload)
        return batch, results


def _write_defillama_price_row(
    conn,
    row,
    results,
    fallback: str | None,
    onchain_fallbacks: bool = True,
    fallback_result: tuple[float | None, str, dict[str, Any]] | None = None,
) -> int:
    chain_id = int(row["chain_id"])
    token_address = row["asset"]
    timestamp = int(row["block_timestamp"])
    block_number = int(row["block_number"])
    price, status, payload = results.get(
        (chain_id, token_address, timestamp),
        (None, "missing", {"timestamp": timestamp}),
    )
    if status != "ok" and onchain_fallbacks:
        if fallback_result is None:
            fallback_result = _safe_row_level_fallback_price(
                chain_id,
                token_address,
                timestamp,
                block_number,
            )
        fallback_price, fallback_status, fallback_payload = fallback_result
        if fallback_status == "ok":
            price, status, payload = fallback_price, fallback_status, fallback_payload
        else:
            payload = {
                "source": "defillama",
                "status": status,
                "timestamp": timestamp,
                "fallback": fallback_payload,
            }
    conn.execute(
        """
            INSERT INTO prices (
                chain_id, token_address, timestamp, block_number,
                source, price_usd, status, raw_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(chain_id,token_address,timestamp,source) DO UPDATE SET
                block_number=excluded.block_number,
                price_usd=excluded.price_usd,
                status=excluded.status,
                raw_json=excluded.raw_json
        """,
        (
            chain_id,
            token_address,
            timestamp,
            block_number,
            "defillama",
            price,
            status,
            to_json(payload),
        ),
    )
    return 1


def _defillama_coin_batches(rows, max_timestamps: int = 400):
    rows_by_coin: dict[tuple[int, str], list] = defaultdict(list)
    for row in rows:
        rows_by_coin[(int(row["chain_id"]), row["asset"])].append(row)
    for coin_rows in rows_by_coin.values():
        coin_rows.sort(key=lambda row: int(row["block_timestamp"]))
        yield from _chunks(coin_rows, max_timestamps)


def _yearn_prices_batches(
    targets: list[tuple[int, str, int]],
    max_tokens: int | None = None,
) -> list[list[tuple[int, str, int]]]:
    if max_tokens is None:
        max_tokens = int(os.environ.get("YEARN_PRICE_BATCH_TOKENS", "20"))
    if max_tokens < 1 or max_tokens > YEARN_PRICES_MAX_TOKEN_KEYS:
        raise ValueError(
            f"YEARN_PRICE_BATCH_TOKENS must be between 1 and {YEARN_PRICES_MAX_TOKEN_KEYS}"
        )

    timestamps_by_token: dict[tuple[int, str], set[int]] = defaultdict(set)
    for chain_id, token_address, timestamp in targets:
        yearn_prices_token_key(chain_id, token_address)
        timestamps_by_token[(int(chain_id), token_address.lower())].add(
            normalize_yearn_price_timestamp(timestamp)
        )

    segments: list[list[tuple[int, str, int]]] = []
    for (chain_id, token_address), timestamps in sorted(timestamps_by_token.items()):
        sorted_timestamps = sorted(timestamps)
        for chunk in _chunks(sorted_timestamps, YEARN_PRICES_MAX_TIMESTAMPS_PER_TOKEN):
            segments.append([(chain_id, token_address, timestamp) for timestamp in chunk])

    batches: list[list[tuple[int, str, int]]] = []
    current: list[tuple[int, str, int]] = []
    current_tokens: set[tuple[int, str]] = set()
    for segment in segments:
        token = (segment[0][0], segment[0][1])
        if current and (token in current_tokens or len(current_tokens) >= max_tokens):
            batches.append(current)
            current = []
            current_tokens = set()
        current.extend(segment)
        current_tokens.add(token)
    if current:
        batches.append(current)
    return batches


def _price_rows_with_yearn_prices(conn, rows) -> tuple[int, list]:
    if not rows:
        return 0, []

    report_targets = [
        (int(row["chain_id"]), row["asset"].lower(), int(row["block_timestamp"]))
        for row in rows
    ]
    normalized_targets = {
        (chain_id, token_address, normalize_yearn_price_timestamp(timestamp))
        for chain_id, token_address, timestamp in report_targets
    }
    results: dict[tuple[int, str, int], tuple[float | None, str, dict[str, Any]]] = {}
    batches = _yearn_prices_batches(list(normalized_targets))
    batch_workers = max(1, int(os.environ.get("YEARN_DATA_PRICE_WORKERS", "8")))
    with ThreadPoolExecutor(max_workers=batch_workers) as executor:
        futures = [executor.submit(fetch_yearn_prices_batch, batch) for batch in batches]
        for future in as_completed(futures):
            try:
                results.update(future.result())
            except YearnPricesRequestError:
                # Treat a transient batch failure like an omission. The exact
                # request below records the terminal state for each target,
                # after which the configured provider fallback can run.
                continue

    missing_targets = sorted(normalized_targets - results.keys())
    exact_workers = max(1, int(os.environ.get("YEARN_PRICE_EXACT_WORKERS", "5")))
    with ThreadPoolExecutor(max_workers=exact_workers) as executor:
        futures = {
            executor.submit(fetch_yearn_price, chain_id, token_address, timestamp): (
                chain_id,
                token_address,
                timestamp,
            )
            for chain_id, token_address, timestamp in missing_targets
        }
        for future in as_completed(futures):
            results[futures[future]] = future.result()

    insert_rows = []
    unresolved_rows = []
    for row in rows:
        chain_id = int(row["chain_id"])
        token_address = row["asset"]
        report_timestamp = int(row["block_timestamp"])
        normalized_timestamp = normalize_yearn_price_timestamp(report_timestamp)
        price, status, result_payload = results[(chain_id, token_address.lower(), normalized_timestamp)]
        if status != "ok":
            unresolved_rows.append(row)
        payload = dict(result_payload)
        payload["requested_timestamp"] = report_timestamp
        insert_rows.append(
            (
                chain_id,
                token_address,
                report_timestamp,
                int(row["block_number"]),
                "yearn-prices",
                price,
                status,
                to_json(payload),
            )
        )
    conn.executemany(
        """
            INSERT INTO prices (
                chain_id, token_address, timestamp, block_number,
                source, price_usd, status, raw_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(chain_id,token_address,timestamp,source) DO UPDATE SET
                block_number=excluded.block_number,
                price_usd=excluded.price_usd,
                status=excluded.status,
                raw_json=excluded.raw_json
        """,
        insert_rows,
    )
    conn.commit()
    return len(insert_rows), unresolved_rows
