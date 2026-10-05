"""Yearn Kong vault metadata client.

Kong's vault snapshots include the underlying ERC-20 address, symbol, and
``asset.decimals`` needed to normalize Envio's raw report values. This avoids
per-vault RPC metadata calls during Envio discovery.
"""

from __future__ import annotations

from typing import Any, Iterable

import requests
from web3 import Web3

from .config import get_kong_graphql_url

KONG_METADATA_BATCH_SIZE = 100
KONG_REQUEST_TIMEOUT = 30

_VAULT_METADATA_QUERY = """
query VaultMetadata($chainId: Int!, $addresses: [String!]) {
  vaults(chainId: $chainId, addresses: $addresses, yearn: true) {
    address
    name
    symbol
    decimals
    apiVersion
    asset { address symbol decimals }
  }
}
"""


def _chunks(items: list[str], size: int) -> Iterable[list[str]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def vault_metadata_from_kong(chain_id: int, addresses: list[str]) -> dict[str, dict[str, Any]]:
    """Return current Yearn vault asset metadata keyed by checksum address.

    Missing addresses are deliberately absent. The caller must retain a null
    asset/decimal rather than silently inventing a decimal scale.
    """
    metadata: dict[str, dict[str, Any]] = {}
    for batch in _chunks([Web3.to_checksum_address(address) for address in addresses], KONG_METADATA_BATCH_SIZE):
        response = requests.post(
            get_kong_graphql_url(),
            json={"query": _VAULT_METADATA_QUERY, "variables": {"chainId": int(chain_id), "addresses": batch}},
            timeout=KONG_REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        body = response.json()
        if body.get("errors"):
            raise RuntimeError(f"Kong GraphQL errors: {body['errors']}")
        for vault in body.get("data", {}).get("vaults") or []:
            asset = vault.get("asset") or {}
            if not asset.get("address") or asset.get("decimals") is None:
                continue
            address = Web3.to_checksum_address(vault["address"])
            metadata[address] = {
                "asset": Web3.to_checksum_address(asset["address"]),
                "asset_symbol": asset.get("symbol"),
                "asset_decimals": int(asset["decimals"]),
                "share_decimals": int(vault["decimals"]) if vault.get("decimals") is not None else None,
                "name": vault.get("name"),
                "api_version": vault.get("apiVersion"),
            }
    return metadata
