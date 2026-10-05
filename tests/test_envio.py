"""Offline contract tests for Envio ingestion adapters and cursors."""

from __future__ import annotations

import pytest

from yearn_data import envio
from yearn_data.config import CHAINS, V2_ETH_REGISTRIES, V3_ROLE_MANAGERS
from yearn_data.indexing import insert_strategy_report, normalize_strategy_report
from yearn_data.storage import connect, init_db, seed_chains

STRATEGY = "0x" + "1" * 40
VAULT = "0x" + "2" * 40
ASSET = "0x" + "3" * 40
TX = "0x" + "ab" * 32


def _v3_report_node(*, block: int = 100, log_index: int = 7) -> dict[str, str | int]:
    return {
        "gain": "10",
        "loss": "3",
        "current_debt": "500",
        "protocol_fees": "1",
        "total_fees": "2",
        "total_refunds": "0",
        "strategy": STRATEGY,
        "vaultAddress": VAULT,
        "chainId": 1,
        "blockNumber": block,
        "blockTimestamp": 1_700_000_000,
        "transactionHash": TX,
        "logIndex": log_index,
    }


def _v2_report_node(*, block: int = 100, log_index: int = 7) -> dict[str, str | int]:
    return {
        "gain": "10",
        "loss": "3",
        "debtAdded": "20",
        "debtPaid": "5",
        "totalGain": "100",
        "totalLoss": "30",
        "totalDebt": "500",
        "debtRatio": "9000",
        "strategy": STRATEGY,
        "vaultAddress": VAULT,
        "chainId": 1,
        "blockNumber": block,
        "blockTimestamp": 1_700_000_000,
        "transactionHash": TX,
        "logIndex": log_index,
    }


def _db(tmp_path):
    conn = connect(tmp_path / "envio.sqlite")
    init_db(conn)
    seed_chains(conn, CHAINS)
    return conn


def _seed_vault(conn) -> None:
    conn.execute(
        """
        INSERT INTO vaults (
            chain_id, version, address, source_address, asset, asset_symbol,
            asset_decimals, name, api_version, deployment_block, updated_at
        ) VALUES (1, 'v3', ?, '0xsource', ?, 'USDC', 6, 'vault', '3.0.0', 42, 1)
        """,
        (VAULT, ASSET),
    )
    conn.commit()


def _seed_v2_vault(conn) -> None:
    conn.execute(
        """
        INSERT INTO vaults (
            chain_id, version, address, source_address, asset, asset_symbol,
            asset_decimals, name, api_version, deployment_block, updated_at
        ) VALUES (1, 'v2', ?, '0xsource', ?, 'USDC', 6, 'vault', '0.4.3', 42, 1)
        """,
        (VAULT, ASSET),
    )
    conn.commit()


def test_report_nodes_match_existing_normalizer():
    v3 = _v3_report_node()
    assert envio.normalize_strategy_report(
        1, "v3", VAULT, ASSET, 6, envio._log(v3), int(v3["blockTimestamp"]), envio._v3_report_args(v3)
    ) == normalize_strategy_report(
        1,
        "v3",
        VAULT,
        ASSET,
        6,
        {"transactionHash": TX, "logIndex": 7, "blockNumber": 100},
        1_700_000_000,
        {"strategy": STRATEGY, "gain": 10, "loss": 3, "current_debt": 500, "protocol_fees": 1, "total_fees": 2, "total_refunds": 0},
    )

    v2 = _v2_report_node()
    assert envio.normalize_strategy_report(
        1, "v2", VAULT, ASSET, 6, envio._log(v2), int(v2["blockTimestamp"]), envio._v2_report_args(v2)
    ) == normalize_strategy_report(
        1,
        "v2",
        VAULT,
        ASSET,
        6,
        {"transactionHash": TX, "logIndex": 7, "blockNumber": 100},
        1_700_000_000,
        {"strategy": STRATEGY, "gain": 10, "loss": 3, "debtAdded": 20, "debtPaid": 5, "totalGain": 100, "totalLoss": 30, "totalDebt": 500, "debtRatio": 9000},
    )


def test_pull_window_paginates_a_stable_block_log_cursor(monkeypatch):
    first = _v3_report_node(block=10, log_index=1)
    second = _v3_report_node(block=10, log_index=2)
    calls = []

    def fake_gql(query, variables, timeout=120):
        calls.append(variables)
        return {"StrategyReported": [first, second] if len(calls) == 1 else []}

    monkeypatch.setattr(envio, "_gql", fake_gql)
    assert envio._pull_window("StrategyReported", envio.V3_REPORT_FIELDS, 1, 10, 10, page_size=2) == [first, second]
    assert calls == [{"lb": 9, "li": -1, "n": 2}, {"lb": 10, "li": 2, "n": 2}]


def test_import_reports_sets_a_source_cursor_and_is_idempotent(tmp_path, monkeypatch):
    conn = _db(tmp_path)
    _seed_vault(conn)
    v3 = _v3_report_node()

    monkeypatch.setattr(envio, "_pull_window", lambda *args, **kwargs: [v3])
    monkeypatch.setattr(envio, "_window_blocks", lambda: 1_000_000)
    assert envio.import_reports_from_envio(conn, ["eth"], versions=["v3"], to_block=100) == 1
    row = conn.execute("SELECT asset, asset_decimals, gain_raw, net_raw FROM strategy_reports").fetchone()
    assert tuple(row) == (ASSET, 6, "10", "7")
    assert envio._get_envio_cursor(conn, 1, "StrategyReported") == 100

    monkeypatch.setattr(envio, "_pull_window", lambda *args, **kwargs: [])
    assert envio.import_reports_from_envio(conn, ["eth"], versions=["v3"], to_block=100) == 0







def test_cli_index_fees_keeps_rpc_implementation_when_envio_is_selected(tmp_path, monkeypatch):
    from yearn_data import cli

    conn = _db(tmp_path)
    calls = []
    monkeypatch.setenv("YEARN_DATA_EVENT_SOURCE", "envio")
    monkeypatch.setattr(cli, "open_db", lambda _path: conn)
    monkeypatch.setattr(
        cli,
        "index_v2_fee_mints_from_reports",
        lambda *args, **kwargs: calls.append((args, kwargs)) or 1,
    )

    assert cli.main(
        ["--db", str(tmp_path / "unused.sqlite"), "index-fees", "--chains", "eth"]
    ) == 0
    assert len(calls) == 1

def test_report_import_stops_before_advancing_for_an_unclassified_vault(tmp_path, monkeypatch):
    conn = _db(tmp_path)
    _seed_vault(conn)
    conn.execute("UPDATE vaults SET address=?", ("0x" + "9" * 40,))
    conn.commit()
    monkeypatch.setattr(envio, "_pull_window", lambda *args, **kwargs: [_v3_report_node()])
    monkeypatch.setattr(envio, "_window_blocks", lambda: 1_000_000)

    with pytest.raises(RuntimeError, match="unclassified vault"):
        envio.import_reports_from_envio(conn, ["eth"], versions=["v3"], to_block=100)
    assert conn.execute("SELECT COUNT(*) FROM strategy_reports").fetchone()[0] == 0
    assert envio._get_envio_cursor(conn, 1, "StrategyReported") is None


def _seed_inventory(
    conn,
    *,
    version="v3",
    vault=VAULT,
    asset=ASSET,
    source_kind="role_manager",
    source_address=V3_ROLE_MANAGERS[1],
    action="added",
    block=40,
    log_index=1,
    tx_hash=TX,
):
    conn.execute(
        """
        INSERT INTO vault_inventory_events (
            chain_id, version, vault_address, source_kind, source_address,
            action, asset, tx_hash, log_index, block_number, block_timestamp
        ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1700000000)
        """,
        (
            version,
            vault,
            source_kind,
            source_address,
            action,
            asset,
            tx_hash,
            log_index,
            block,
        ),
    )
    conn.commit()


def test_envio_discovery_reuses_cached_metadata_and_preserves_rpc_facts(tmp_path, monkeypatch):
    conn = _db(tmp_path)
    _seed_vault(conn)
    _seed_inventory(conn)
    monkeypatch.setattr(envio, "_import_v3_vault_inventory", lambda *args: 0)
    monkeypatch.setattr(envio, "vault_metadata_from_kong", lambda *args: (_ for _ in ()).throw(AssertionError("unexpected Kong query")))

    assert envio.discover_from_envio(conn, ["eth"], skip_v2=True, to_block=100) == 1
    row = conn.execute("SELECT source_address, deployment_block, asset, management FROM vaults WHERE address=?", (VAULT,)).fetchone()
    assert tuple(row) == ("0xsource", 42, ASSET, "yearn")


def test_envio_discovery_resolves_missing_asset_decimals_from_kong(tmp_path, monkeypatch):
    conn = _db(tmp_path)
    _seed_inventory(conn)
    monkeypatch.setattr(envio, "_import_v3_vault_inventory", lambda *args: 0)
    monkeypatch.setattr(
        envio,
        "vault_metadata_from_kong",
        lambda chain_id, addresses: {
            VAULT: {
                "asset": ASSET,
                "asset_symbol": "USDC",
                "asset_decimals": 6,
                "name": "vault",
                "api_version": "3.0.0",
            }
        },
    )

    assert envio.discover_from_envio(conn, ["eth"], skip_v2=True, to_block=100) == 1
    row = conn.execute("SELECT asset, asset_symbol, asset_decimals FROM vaults WHERE address=?", (VAULT,)).fetchone()
    assert tuple(row) == (ASSET, "USDC", 6)


def test_v3_inventory_keeps_each_classification_source(tmp_path, monkeypatch):
    conn = _db(tmp_path)
    nodes_by_entity = {
        "V3RegistryNewEndorsedVault": [{
            "registryAddress": "0x" + "5" * 40,
            "vault": VAULT,
            "asset": ASSET,
            "releaseVersion": "3",
            "vaultType": "0",
            "chainId": 1,
            "blockNumber": 40,
            "blockTimestamp": 1_700_000_000,
            "transactionHash": TX,
            "logIndex": 1,
        }],
        "V3VaultFactoryNewVault": [{
            "factoryAddress": "0x" + "6" * 40,
            "vault_address": VAULT,
            "asset": ASSET,
            "chainId": 1,
            "blockNumber": 41,
            "blockTimestamp": 1_700_000_001,
            "transactionHash": "0x" + "bc" * 32,
            "logIndex": 2,
        }],
        "V3RoleManagerAddedNewVault": [{
            "roleManagerAddress": "0x" + "7" * 40,
            "vault": VAULT,
            "debtAllocator": "0x" + "8" * 40,
            "category": "0",
            "chainId": 1,
            "blockNumber": 42,
            "blockTimestamp": 1_700_000_002,
            "transactionHash": "0x" + "cd" * 32,
            "logIndex": 3,
        }],
        "V3RoleManagerRemovedVault": [{
            "roleManagerAddress": "0x" + "7" * 40,
            "vault": VAULT,
            "chainId": 1,
            "blockNumber": 43,
            "blockTimestamp": 1_700_000_003,
            "transactionHash": "0x" + "de" * 32,
            "logIndex": 4,
        }],
    }
    monkeypatch.setattr(
        envio,
        "_pull_window",
        lambda entity, *args, **kwargs: nodes_by_entity[entity],
    )

    assert envio._import_v3_vault_inventory(conn, "eth", 100, None) == 4
    rows = conn.execute(
        "SELECT source_kind, action FROM vault_inventory_events ORDER BY block_number"
    ).fetchall()
    assert [tuple(row) for row in rows] == [
        ("registry", "added"),
        ("factory", "added"),
        ("role_manager", "added"),
        ("role_manager", "removed"),
    ]


def test_v3_membership_replays_add_remove_and_readd_at_selected_block(tmp_path):
    conn = _db(tmp_path)
    removed_vault = "0x" + "4" * 40
    later_vault = "0x" + "5" * 40
    _seed_inventory(conn, vault=VAULT, block=10, log_index=1, tx_hash="0x" + "10" * 32)
    _seed_inventory(conn, vault=removed_vault, block=11, log_index=1, tx_hash="0x" + "11" * 32)
    _seed_inventory(
        conn,
        vault=VAULT,
        action="removed",
        asset=None,
        block=12,
        log_index=1,
        tx_hash="0x" + "12" * 32,
    )
    _seed_inventory(
        conn,
        vault=removed_vault,
        action="removed",
        asset=None,
        block=13,
        log_index=1,
        tx_hash="0x" + "13" * 32,
    )
    _seed_inventory(conn, vault=VAULT, block=14, log_index=1, tx_hash="0x" + "14" * 32)
    _seed_inventory(conn, vault=later_vault, block=101, log_index=1, tx_hash="0x" + "15" * 32)

    assert envio._active_v3_role_manager_addresses(
        conn, 1, V3_ROLE_MANAGERS[1], 100
    ) == {VAULT}


def test_envio_discovery_marks_removed_v3_vaults_inactive(tmp_path, monkeypatch):
    conn = _db(tmp_path)
    removed_vault = "0x" + "4" * 40
    _seed_vault(conn)
    conn.execute(
        """
        INSERT INTO vaults (
            chain_id, version, address, source_address, asset, asset_symbol,
            asset_decimals, name, api_version, deployment_block, updated_at
        ) VALUES (1, 'v3', ?, '0xsource', ?, 'USDC', 6, 'removed', '3.0.0', 43, 1)
        """,
        (removed_vault, ASSET),
    )
    conn.commit()
    _seed_inventory(conn, vault=VAULT, block=40, log_index=1, tx_hash="0x" + "20" * 32)
    _seed_inventory(
        conn,
        vault=removed_vault,
        block=41,
        log_index=1,
        tx_hash="0x" + "21" * 32,
    )
    _seed_inventory(
        conn,
        vault=removed_vault,
        asset=None,
        action="removed",
        block=42,
        log_index=1,
        tx_hash="0x" + "22" * 32,
    )
    monkeypatch.setattr(envio, "_import_v3_vault_inventory", lambda *args: 0)
    monkeypatch.setattr(
        envio,
        "vault_metadata_from_kong",
        lambda *args: (_ for _ in ()).throw(AssertionError("unexpected Kong query")),
    )

    assert envio.discover_from_envio(conn, ["eth"], skip_v2=True, to_block=100) == 1
    rows = conn.execute("SELECT address, active FROM vaults ORDER BY address").fetchall()
    assert {row["address"]: row["active"] for row in rows} == {
        VAULT: 1,
        removed_vault: 0,
    }


def test_envio_discovery_excludes_experimental_v2_by_default(tmp_path, monkeypatch):
    conn = _db(tmp_path)
    registry_vault = "0x" + "4" * 40
    registry2_vault = "0x" + "5" * 40
    experimental_vault = "0x" + "6" * 40
    for index, (vault, source_kind) in enumerate(
        (
            (registry_vault, "registry"),
            (registry2_vault, "registry2"),
            (experimental_vault, "registry_experimental"),
        ),
        start=1,
    ):
        _seed_inventory(
            conn,
            version="v2",
            vault=vault,
            source_kind=source_kind,
            source_address=V2_ETH_REGISTRIES[0],
            block=20 + index,
            log_index=index,
            tx_hash="0x" + f"{index:02x}" * 32,
        )
    _seed_inventory(
        conn,
        version="v2",
        vault=registry_vault,
        source_kind="registry",
        source_address="0x" + "7" * 40,
        block=10,
        log_index=10,
        tx_hash="0x" + "10" * 32,
    )
    monkeypatch.setattr(envio, "_import_v3_vault_inventory", lambda *args: 0)
    monkeypatch.setattr(envio, "_import_v2_vault_inventory", lambda *args: 0)
    monkeypatch.setattr(
        envio,
        "vault_metadata_from_kong",
        lambda _chain_id, addresses: {
            address: {
                "asset": ASSET,
                "asset_symbol": "USDC",
                "asset_decimals": 6,
                "name": "vault",
                "api_version": "0.4.3",
            }
            for address in addresses
        },
    )

    assert envio.discover_from_envio(conn, ["eth"], to_block=100) == 2
    assert conn.execute("SELECT COUNT(*) FROM vaults WHERE version='v2' AND active=1").fetchone()[0] == 2
    production_row = conn.execute(
        "SELECT source_address, deployment_block FROM vaults WHERE address=?",
        (registry_vault,),
    ).fetchone()
    assert tuple(production_row) == (V2_ETH_REGISTRIES[0], None)

    assert envio.discover_from_envio(
        conn, ["eth"], include_experimental_v2=True, to_block=100
    ) == 3
    assert conn.execute("SELECT COUNT(*) FROM vaults WHERE version='v2' AND active=1").fetchone()[0] == 3


def test_cli_routes_experimental_v2_option_to_envio_discovery(tmp_path, monkeypatch):
    from yearn_data import cli

    conn = _db(tmp_path)
    captured = {}
    monkeypatch.setenv("YEARN_DATA_EVENT_SOURCE", "envio")
    monkeypatch.setattr(cli, "open_db", lambda _path: conn)

    def fake_discover(*args, **kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(cli, "discover_from_envio", fake_discover)
    assert cli.main(
        [
            "--db",
            str(tmp_path / "unused.sqlite"),
            "discover",
            "--chains",
            "eth",
            "--include-experimental-v2",
        ]
    ) == 0
    assert captured["include_experimental_v2"] is True



def test_cli_combined_lifetime_yield_uses_envio(tmp_path, monkeypatch):
    from yearn_data import cli

    conn = _db(tmp_path)
    calls = []
    monkeypatch.setenv("YEARN_DATA_EVENT_SOURCE", "envio")
    monkeypatch.setattr(cli, "open_db", lambda _path: conn)
    monkeypatch.setattr(
        cli,
        "discover_from_envio",
        lambda *args, **kwargs: calls.append(("discover_envio", kwargs)) or 1,
    )
    monkeypatch.setattr(
        cli,
        "import_reports_from_envio",
        lambda *args, **kwargs: calls.append(("reports_envio", kwargs)) or 2,
    )
    monkeypatch.setattr(
        cli,
        "discover",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unexpected RPC discovery")),
    )
    monkeypatch.setattr(
        cli,
        "index_all_reports",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unexpected RPC report indexing")),
    )
    monkeypatch.setattr(
        cli,
        "price_unpriced_reports",
        lambda *args, **kwargs: calls.append(("price", kwargs)) or 3,
    )
    monkeypatch.setattr(
        cli,
        "_run_analysis",
        lambda *args, **kwargs: calls.append(("analyze", kwargs)) or 4,
    )
    monkeypatch.setattr(cli, "export_analysis", lambda *args: [])

    assert cli.main(
        [
            "--db",
            str(tmp_path / "unused.sqlite"),
            "run",
            "lifetime-yield",
            "--chains",
            "eth",
            "--to-block",
            "100",
        ]
    ) == 0
    assert [name for name, _ in calls] == [
        "discover_envio",
        "reports_envio",
        "price",
        "analyze",
    ]
    assert calls[0][1]["to_block"] == 100
    assert calls[1][1]["to_block"] == 100
    assert calls[2][1]["source"] == "defillama"
    assert calls[2][1]["fallback"] is None


def test_cli_combined_volume_keeps_rpc_path_when_envio_is_selected(tmp_path, monkeypatch):
    from yearn_data import cli

    conn = _db(tmp_path)
    calls = []
    monkeypatch.setenv("YEARN_DATA_EVENT_SOURCE", "envio")
    monkeypatch.setattr(cli, "open_db", lambda _path: conn)
    monkeypatch.setattr(cli, "discover", lambda *args, **kwargs: calls.append("discover_rpc") or 1)
    monkeypatch.setattr(cli, "index_all_volume", lambda *args, **kwargs: calls.append("volume_rpc") or 2)
    monkeypatch.setattr(cli, "price_unpriced_volume", lambda *args, **kwargs: calls.append("price_volume") or 3)
    monkeypatch.setattr(cli, "_run_analysis", lambda *args, **kwargs: calls.append("analyze") or 4)
    monkeypatch.setattr(cli, "export_analysis", lambda *args: [])
    monkeypatch.setattr(
        cli,
        "discover_from_envio",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unexpected Envio discovery")),
    )

    assert cli.main(
        [
            "--db",
            str(tmp_path / "unused.sqlite"),
            "run",
            "vault-volume",
            "--chains",
            "eth",
            "--to-block",
            "100",
        ]
    ) == 0
    assert calls == ["discover_rpc", "volume_rpc", "price_volume", "analyze"]


def test_historical_import_recovers_inactive_pre_registration_report_without_cursor_reset(tmp_path, monkeypatch):
    conn = _db(tmp_path)
    _seed_vault(conn)
    conn.execute("UPDATE vaults SET active=0, deployment_block=200")
    conn.commit()
    envio._set_envio_cursor(conn, 1, "StrategyReported", 500)
    calls = []
    monkeypatch.setattr(envio, "_pull_window", lambda *a, **kw: calls.append((a, kw)) or [_v3_report_node(block=100)])
    assert envio.import_reports_from_envio(conn, ['eth'], versions=['v3'],
        from_block=90, to_block=110, include_inactive=True, vault_addresses=[VAULT]) == 1
    assert calls[0][0][3:5] == (90, 110)
    assert calls[0][1]['address_values'] == [VAULT]
    assert envio._get_envio_cursor(conn, 1, 'StrategyReported') == 500
    assert conn.execute('SELECT block_number FROM strategy_reports').fetchone()[0] == 100
    assert conn.execute('SELECT active FROM vaults').fetchone()[0] == 0
    envio.import_reports_from_envio(conn, ['eth'], versions=['v3'],
        from_block=90, to_block=110, include_inactive=True, vault_addresses=[VAULT])
    assert conn.execute('SELECT COUNT(*) FROM strategy_reports').fetchone()[0] == 1


def test_historical_import_requires_bounds_and_cannot_advance_global_cursor_for_subset(tmp_path):
    conn = _db(tmp_path)
    with pytest.raises(ValueError, match='explicit to_block'):
        envio.import_reports_from_envio(conn, ['eth'], from_block=0)
    with pytest.raises(ValueError, match='preserve global cursors'):
        envio.import_reports_from_envio(conn, ['eth'], vault_addresses=[VAULT], to_block=100)


def test_historical_discovery_retains_removed_vault_without_reactivating(tmp_path, monkeypatch):
    conn = _db(tmp_path)
    _seed_vault(conn)
    _seed_inventory(conn, block=40)
    _seed_inventory(conn, block=50, action='removed', log_index=2)
    monkeypatch.setattr(envio, '_import_v3_vault_inventory', lambda *a: 0)
    assert envio.discover_from_envio(conn, ['eth'], skip_v2=True, include_retired=True, to_block=100) == 1
    assert conn.execute('SELECT active FROM vaults').fetchone()[0] == 0
    assert envio._vault_asset_map(conn, 1) == {}
    assert VAULT.lower() in envio._vault_asset_map(conn, 1, include_inactive=True)


def test_v2_only_chain_uses_own_registry_and_does_not_require_role_manager(tmp_path, monkeypatch):
    from yearn_data.config import V2_REGISTRIES_BY_CHAIN
    conn = _db(tmp_path)
    _seed_v2_vault(conn)
    _seed_inventory(conn, version='v2', source_kind='registry', source_address=V2_REGISTRIES_BY_CHAIN[250][0])
    conn.execute('UPDATE vaults SET chain_id=250')
    conn.execute('UPDATE vault_inventory_events SET chain_id=250')
    conn.commit()
    monkeypatch.setattr(envio, '_import_v3_vault_inventory', lambda *a: 0)
    monkeypatch.setattr(envio, '_import_v2_vault_inventory', lambda *a: 0)
    assert envio.discover_from_envio(conn, ['ftm'], to_block=100) == 1
    assert conn.execute('SELECT active FROM vaults').fetchone()[0] == 1


def test_inventory_distinguishes_creation_from_registration(tmp_path):
    conn = _db(tmp_path)
    _seed_inventory(conn, source_kind='registry', block=80)
    facts = envio._inventory_facts(conn, 1, 'v3')
    assert facts[VAULT.lower()]['registration_block'] == 80
    assert facts[VAULT.lower()]['deployment_block'] is None
    _seed_inventory(conn, source_kind='factory', block=40, log_index=2)
    assert envio._inventory_facts(conn, 1, 'v3')[VAULT.lower()]['deployment_block'] == 40


def test_rpc_only_historical_chains_are_explicitly_selectable_not_default():
    from yearn_data.cli import _chains
    assert _chains(['optimism', 'fantom']) == ['op', 'ftm']
    assert 'op' not in _chains(None) and 'ftm' not in _chains(None)
    assert CHAINS['op'].rpc_env == 'OPTIMISM_RPC_URL'
    assert CHAINS['ftm'].rpc_env == 'FANTOM_RPC_URL'


def test_cli_historical_discovery_keeps_retired_members_inactive(tmp_path, monkeypatch):
    from yearn_data import cli

    conn = _db(tmp_path)
    _seed_vault(conn)
    _seed_inventory(conn, block=40)
    _seed_inventory(conn, block=50, action="removed", log_index=2)
    monkeypatch.setenv("YEARN_DATA_EVENT_SOURCE", "envio")
    monkeypatch.setattr(cli, "open_db", lambda _path: conn)
    monkeypatch.setattr(envio, "_import_v3_vault_inventory", lambda *args: 0)

    assert cli.main([
        "discover", "--chains", "eth", "--skip-v2",
        "--include-retired", "--to-block", "100",
    ]) == 0

    assert conn.execute("SELECT active FROM vaults WHERE address=?", (VAULT,)).fetchone()[0] == 0
    assert VAULT.lower() in envio._vault_asset_map(conn, 1, include_inactive=True)


def test_cli_historical_import_persists_reports_without_moving_forward_cursor(tmp_path, monkeypatch):
    from yearn_data import cli

    conn = _db(tmp_path)
    _seed_vault(conn)
    conn.execute("UPDATE vaults SET active=0, deployment_block=200")
    envio._set_envio_cursor(conn, 1, "StrategyReported", 500)
    monkeypatch.setenv("YEARN_DATA_EVENT_SOURCE", "envio")
    monkeypatch.setattr(envio, "_pull_window", lambda *args, **kwargs: [_v3_report_node()])

    assert cli.main([
        "--db", str(tmp_path / "envio.sqlite"), "index-events",
        "--chains", "eth", "--versions", "v3", "--include-inactive",
        "--from-block", "90", "--to-block", "110", "--vaults", VAULT,
    ]) == 0

    assert conn.execute("SELECT COUNT(*) FROM strategy_reports").fetchone()[0] == 1
    assert envio._get_envio_cursor(conn, 1, "StrategyReported") == 500
    assert conn.execute("SELECT active FROM vaults").fetchone()[0] == 0


@pytest.mark.parametrize("arguments", [
    ["discover", "--include-retired"],
    ["index-events", "--include-inactive"],
    ["index-events", "--from-block", "0", "--to-block", "100"],
    ["index-events", "--vaults", VAULT],
])
def test_cli_rejects_envio_history_flags_before_rpc_work(tmp_path, monkeypatch, arguments):
    from yearn_data import cli

    def unexpected_rpc(*args, **kwargs):
        pytest.fail("unsupported history flags must not trigger RPC acquisition")

    monkeypatch.setenv("YEARN_DATA_EVENT_SOURCE", "rpc")
    monkeypatch.setattr(cli, "discover", unexpected_rpc)
    monkeypatch.setattr(cli, "index_all_reports", unexpected_rpc)
    with pytest.raises(ValueError, match="requires? Envio"):
        cli.main(["--db", str(tmp_path / "history.sqlite"), *arguments])


@pytest.mark.parametrize(("from_block", "to_block"), [(-1, 100), (101, 100)])
def test_invalid_historical_bounds_stop_before_acquisition(tmp_path, monkeypatch, from_block, to_block):
    def unexpected_query(*args, **kwargs):
        pytest.fail("invalid bounds must not query Envio")

    conn = _db(tmp_path)
    monkeypatch.setattr(envio, "_resolve_to_block", unexpected_query)
    with pytest.raises(ValueError, match="explicit to_block"):
        envio.import_reports_from_envio(conn, ["eth"], from_block=from_block, to_block=to_block)


def test_unknown_historical_vault_stops_before_report_query(tmp_path, monkeypatch):
    conn = _db(tmp_path)
    _seed_vault(conn)
    envio._set_envio_cursor(conn, 1, "StrategyReported", 500)

    def unexpected_query(*args, **kwargs):
        pytest.fail("an unknown vault must not broaden the report query")

    monkeypatch.setattr(envio, "_pull_window", unexpected_query)
    with pytest.raises(ValueError, match="unclassified or excluded vaults"):
        envio.import_reports_from_envio(
            conn, ["eth"], from_block=0, to_block=100,
            vault_addresses=["0x" + "9" * 40],
        )
    assert envio._get_envio_cursor(conn, 1, "StrategyReported") == 500


def test_failed_historical_window_can_be_replayed_after_reopening_database(tmp_path, monkeypatch):
    conn = _db(tmp_path)
    _seed_vault(conn)
    conn.execute("UPDATE vaults SET active=0")
    envio._set_envio_cursor(conn, 1, "StrategyReported", 500)
    monkeypatch.setattr(envio, "_window_blocks", lambda: 10)

    def fetch_window(entity, fields, chain, lo, hi, **kwargs):
        if lo == 100:
            raise RuntimeError("injected acquisition failure")
        return [_v3_report_node(block=90, log_index=1)]

    monkeypatch.setattr(envio, "_pull_window", fetch_window)
    arguments = dict(versions=["v3"], from_block=90, to_block=109, include_inactive=True)
    with pytest.raises(RuntimeError, match="injected acquisition failure"):
        envio.import_reports_from_envio(conn, ["eth"], **arguments)
    conn.close()

    conn = connect(tmp_path / "envio.sqlite")
    assert conn.execute("SELECT block_number FROM strategy_reports").fetchone()[0] == 90
    assert envio._get_envio_cursor(conn, 1, "StrategyReported") == 500
    monkeypatch.setattr(envio, "_pull_window", lambda entity, fields, chain, lo, hi, **kwargs: [
        _v3_report_node(block=lo, log_index=lo),
    ] if lo == 100 else [_v3_report_node(block=90, log_index=1)])

    envio.import_reports_from_envio(conn, ["eth"], **arguments)
    assert [row[0] for row in conn.execute(
        "SELECT block_number FROM strategy_reports ORDER BY block_number"
    )] == [90, 100]
    assert envio._get_envio_cursor(conn, 1, "StrategyReported") == 500
    conn.close()


@pytest.mark.parametrize("stored_case", ["lower", "checksum"])
def test_pull_window_matches_both_address_forms(monkeypatch, stored_case):
    from web3 import Web3
    address = Web3.to_checksum_address("0xabcdefabcdefabcdefabcdefabcdefabcdefabcd")
    node = {**_v3_report_node(), "vaultAddress": address if stored_case == "checksum" else address.lower()}
    def query(_query, variables):
        assert set(variables["addresses"]) == {address, address.lower()}
        return {"StrategyReported": [node]}
    monkeypatch.setattr(envio, "_gql", query)
    assert envio._pull_window("StrategyReported", envio.V3_REPORT_FIELDS, 1, 100, 100,
        address_field="vaultAddress", address_values=[address]) == [node]


@pytest.mark.parametrize("response", [{}, {"StrategyReported": None}, {"StrategyReported": {}}])
def test_missing_entity_cannot_advance_cursor(tmp_path, monkeypatch, response):
    conn = _db(tmp_path)
    _seed_vault(conn)
    monkeypatch.setattr(envio, "_gql", lambda *args: response)
    with pytest.raises(ValueError, match="missing Envio entity"):
        envio.import_reports_from_envio(conn, ["eth"], versions=["v3"], to_block=100)
    assert envio._get_envio_cursor(conn, 1, "StrategyReported") is None
    assert conn.execute("SELECT COUNT(*) FROM strategy_reports").fetchone()[0] == 0


@pytest.mark.parametrize("change", [{"blockNumber": 101}, {"chainId": 10}, {"vaultAddress": ASSET}])
def test_pull_window_rejects_wrong_scope(monkeypatch, change):
    monkeypatch.setattr(envio, "_gql", lambda *args: {"StrategyReported": [{**_v3_report_node(), **change}]})
    with pytest.raises(ValueError, match="mismatch"):
        envio._pull_window("StrategyReported", envio.V3_REPORT_FIELDS, 1, 100, 100,
            address_field="vaultAddress", address_values=[VAULT])
