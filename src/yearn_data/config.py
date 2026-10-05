"""Configuration for chains, registries, role managers, and environment loading."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
import os

from dotenv import load_dotenv


DEFAULT_ENV_PATHS = (
    Path(".env"),
)


@dataclass(frozen=True)
class ChainConfig:
    key: str
    chain_id: int
    name: str
    rpc_env: str
    defillama_slug: str
    explorer_chain_id: int | None = None


CHAINS: dict[str, ChainConfig] = {
    "eth": ChainConfig("eth", 1, "Ethereum", "ETH_RPC_URL", "ethereum", 1),
    "polygon": ChainConfig("polygon", 137, "Polygon", "POLYGON_RPC_URL", "polygon", 137),
    "base": ChainConfig("base", 8453, "Base", "BASE_RPC_URL", "base", 8453),
    "arb": ChainConfig("arb", 42161, "Arbitrum", "ARB_RPC_URL", "arbitrum", 42161),
    "op": ChainConfig("op", 10, "Optimism", "OPTIMISM_RPC_URL", "optimism", 10),
    "ftm": ChainConfig("ftm", 250, "Fantom", "FANTOM_RPC_URL", "fantom", 250),
    "kat": ChainConfig("kat", 747474, "Katana", "KAT_RPC_URL", "katana", None),
}

# Optimism and Fantom are explicitly selectable for historical RPC work;
# keep default Envio jobs on chains with verified endpoint coverage.
DEFAULT_CHAINS = ("eth", "polygon", "base", "arb", "kat")

CHAIN_ALIASES = {
    "ethereum": "eth",
    "mainnet": "eth",
    "matic": "polygon",
    "arbitrum": "arb",
    "katana": "kat",
    "optimism": "op",
    "fantom": "ftm",
}

V3_ROLE_MANAGERS = {
    1: "0xb3bd6B2E61753C311EFbCF0111f75D29706D9a41",
    137: "0x2C4b68B2e3f03B3BD8804EB02fA22CD387E78B83",
    8453: "0xea3481244024E2321cc13AcAa80df1050f1fD456",
    42161: "0x3BF72024420bdc4D7cA6a8b6211829476D6685b1",
    747474: "0x2297d2486070655c3a162b02c64248A2f9dBC9a4",
}

V2_ETH_REGISTRIES = (
    "0xaF1f5e1c19cB68B30aAD73846eFfDf78a5863319",
    "0x50c1a2eA0a861A967D9d0FFE2AE4012c2E053804",
)


# Historical registry sources: Kong config/abis.yaml, yearn/2/registry{,2}.
# Keep the original Ethereum tuple for the legacy RPC discovery adapter.
V2_REGISTRIES_BY_CHAIN = {
    1: (*V2_ETH_REGISTRIES, "0xE15461B18EE31b7379019Dc523231C57d1Cbc18c"),
    10: ("0x1ba4eB0F44AB82541E56669e18972b0d6037dfE0", "0x79286Dd38C9017E5423073bAc11F53357Fc5C128"),
    250: ("0x727fe1759430df13655ddb0731dE0D0FDE929b04",),
    8453: ("0xF3885eDe00171997BFadAa98E01E167B53a78Ec5",),
    42161: ("0x3199437193625DCcD6F9C9e98BDf93582200Eb1f",),
}


def load_environment(extra_paths: Iterable[str | Path] = ()) -> None:
    """Load dotenv files without overriding already exported variables."""
    for path in [*extra_paths, *DEFAULT_ENV_PATHS]:
        p = Path(path).expanduser()
        if p.exists():
            load_dotenv(p, override=False)


def normalize_chain_key(chain: str) -> str:
    key = CHAIN_ALIASES.get(chain, chain)
    if key not in CHAINS:
        raise ValueError(f"unknown chain {chain!r}; expected one of {sorted(CHAINS)}")
    return key


def get_rpc_url(chain: str) -> str:
    cfg = CHAINS[normalize_chain_key(chain)]
    value = os.environ.get(cfg.rpc_env)
    if not value:
        raise ValueError(f"missing RPC for {cfg.key}; set {cfg.rpc_env}")
    return value

def get_rpc_urls(chain: str) -> list[str]:
    """RPC endpoints for a chain; comma-separated values are tried in order."""
    return [url.strip() for url in get_rpc_url(chain).split(",") if url.strip()]


KONG_GRAPHQL_DEFAULT_URL = "https://kong.yearn.fi/api/gql"


def get_envio_graphql_url() -> str:
    """Envio Hasura GraphQL endpoint used when event source is ``envio``."""
    value = os.environ.get("YEARN_ENVIO_GRAPHQL_URL")
    if not value:
        raise ValueError("missing Envio endpoint; set YEARN_ENVIO_GRAPHQL_URL")
    return value


def get_kong_graphql_url() -> str:
    """Yearn Kong GraphQL endpoint for vault and underlying-token metadata."""
    return os.environ.get("YEARN_KONG_GRAPHQL_URL", KONG_GRAPHQL_DEFAULT_URL)


def get_event_source() -> str:
    """Configured indexing backend: Envio by default, legacy RPC on request."""
    value = os.environ.get("YEARN_DATA_EVENT_SOURCE", "envio").strip().lower()
    if value not in {"envio", "rpc"}:
        raise ValueError(f"YEARN_DATA_EVENT_SOURCE must be 'rpc' or 'envio', got {value!r}")
    return value


def get_etherscan_api_key() -> str | None:
    return os.environ.get("ETHERSCAN_API_KEY")
