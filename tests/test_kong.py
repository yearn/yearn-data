"""Offline tests for Yearn Kong vault metadata ingestion."""

from __future__ import annotations

from yearn_data import kong

VAULT = "0x" + "2" * 40
ASSET = "0x" + "3" * 40


class _Response:
    def raise_for_status(self):
        return None

    def json(self):
        return {
            "data": {
                "vaults": [
                    {
                        "address": VAULT,
                        "name": "Test vault",
                        "symbol": "yvTEST",
                        "apiVersion": "3.0.0",
                        "asset": {"address": ASSET, "symbol": "USDC", "decimals": 6},
                    }
                ]
            }
        }


def test_vault_metadata_from_kong_uses_underlying_asset_decimals(monkeypatch):
    calls = []
    monkeypatch.setattr(
        kong.requests,
        "post",
        lambda url, json, timeout: calls.append((url, json, timeout)) or _Response(),
    )

    metadata = kong.vault_metadata_from_kong(1, [VAULT])
    assert metadata[VAULT]["asset"] == ASSET
    assert metadata[VAULT]["asset_symbol"] == "USDC"
    assert metadata[VAULT]["asset_decimals"] == 6
    assert calls[0][1]["variables"] == {"chainId": 1, "addresses": [VAULT]}
