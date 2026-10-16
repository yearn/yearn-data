"""Supported bounded acquisition uses retained inventory and durable ranges."""

import json
from types import SimpleNamespace

from hexbytes import HexBytes
import pytest
from web3 import Web3

from yearn_data import catchup, cli
from yearn_data.history_sources import HistoricalSource, inventory_targets
from yearn_data.indexing import normalize_strategy_report
from yearn_data.storage import connect, init_db

VAULT = '0x' + '41' * 20
NEW = '0x' + '42' * 20
ASSET = '0x' + '43' * 20
HASH = '0x' + 'ab' * 32


def seed(conn, address=VAULT, management='yearn'):
    conn.execute("""INSERT INTO vaults(chain_id,version,address,asset,asset_decimals,
        api_version,management,active,updated_at) VALUES(10,'v2',?,?,6,'0.4.6',?,0,0)""",
        (address, ASSET, management))
    conn.commit()


@pytest.fixture
def db(tmp_path):
    conn = connect(tmp_path/'catchup.sqlite')
    init_db(conn)
    seed(conn)
    yield conn
    conn.close()


class Source(HistoricalSource):
    def __init__(self):
        w3 = SimpleNamespace(eth=SimpleNamespace(chain_id=10, block_number=100,
            get_block=lambda n: dict(number=90 if n == 'finalized' else n,
                                     hash=HexBytes(HASH), timestamp=1000)))
        super().__init__('op', w3)
        self.calls = []
        self.fail = None

    def reports(self, vault, lo, hi, source):
        self.calls.append((vault['address'], lo, hi))
        if self.fail == vault['address']:
            raise RuntimeError('provider unavailable')
        if not lo <= 25 <= hi:
            return []
        return [normalize_strategy_report(10, 'v2', vault['address'], ASSET, 6,
            dict(transactionHash='0x'+vault['address'][2:4]*32, logIndex=0, blockNumber=25),
            1000, dict(strategy=ASSET, gain=12, loss=0, debtAdded=0, debtPaid=0,
                       totalGain=12, totalLoss=0, totalDebt=100, debtRatio=100))]

    def inventory(self, target, lo, hi, source):
        if target != next(inventory_targets('op')) or not lo <= 20 <= hi:
            return []
        return [dict(chain_id=10, version='v2', vault_address=NEW,
                     source_kind=target.kind, action='added', source_address=target.address,
                     asset=ASSET, tx_hash='0x'+'33'*32, log_index=0, block_number=20,
                     block_timestamp=900, decoded_json=json.dumps(dict(
                         token=ASSET, vault=NEW, api_version='0.4.6', vault_id=0)))]


def test_discovery_unions_retained_inactive_vaults_and_resumes(db, monkeypatch):
    seed(db, '0x'+'44'*20, management='external')
    monkeypatch.setattr(catchup, 'vault_metadata_from_kong', lambda *args: {
        Web3.to_checksum_address(NEW): dict(asset=ASSET, asset_decimals=6, api_version='0.4.6')})
    client = Source()
    result = catchup.catch_up(db, 'op', 10, 30, discover=True, chunk_size=10, client=client)
    assert result['vaults'] == 2
    assert {a for a, _, _ in client.calls} == {VAULT, NEW}
    assert db.execute('SELECT COUNT(*) FROM strategy_reports').fetchone()[0] == 2
    assert db.execute('SELECT COUNT(*) FROM events_raw').fetchone()[0] == 2
    assert result['pin']['finality'] == 'finalized'
    assert db.execute('SELECT active FROM vaults WHERE address=?', (VAULT,)).fetchone()[0] == 0
    client.calls.clear()
    repeated = catchup.catch_up(db, 'op', 10, 30, discover=True, chunk_size=7, client=client)
    assert repeated['completed_ranges'] == repeated['inserted_rows'] == 0
    assert client.calls == []


def test_failure_keeps_only_completed_ranges_then_resumes(db):
    seed(db, NEW)
    client = Source()
    client.fail = NEW
    with pytest.raises(RuntimeError, match='provider unavailable'):
        catchup.catch_up(db, 'op', 10, 30, chunk_size=10, client=client)
    assert db.execute('SELECT DISTINCT target FROM history_coverage').fetchall()[0][0] == VAULT
    assert db.execute('SELECT COUNT(*) FROM strategy_reports').fetchone()[0] == 1
    client.fail = None
    client.calls.clear()
    result = catchup.catch_up(db, 'op', 10, 30, chunk_size=7, client=client)
    assert {a for a, _, _ in client.calls} == {NEW}
    assert result['vaults'] == 2
    assert db.execute('SELECT COUNT(*) FROM strategy_reports').fetchone()[0] == 2


def test_explicit_confirmations_are_recorded_and_reused_hash_is_checked(db):
    client = Source()
    result = catchup.catch_up(db, 'op', 10, 30, confirmations=20, client=client)
    assert result['pin']['finality'] == 'confirmations:20'
    db.execute("UPDATE history_coverage SET end_hash='changed'")
    db.commit()
    client.calls.clear()
    with pytest.raises(ValueError, match='hash changed'):
        catchup.catch_up(db, 'op', 10, 30, client=client)
    assert client.calls == []


@pytest.mark.parametrize('options', [dict(from_block=-1, to_block=30),
    dict(from_block=30, to_block=20), dict(from_block=0,to_block=30,chunk_size=0),
    dict(from_block=0,to_block=30,confirmations=0), dict(from_block=0,to_block=30,experimental=True)])
def test_invalid_scope_fails_before_client_creation(db, monkeypatch, options):
    monkeypatch.setattr(catchup, 'web3_for', lambda *args: pytest.fail('unexpected RPC'))
    with pytest.raises(ValueError):
        catchup.catch_up(db, 'op', **options)


def test_cli_catchup_only_calls_acquisition(tmp_path, monkeypatch, capsys):
    captured = {}
    def acquire(conn, chain, first, last, **kwargs):
        captured.update(chain=chain, first=first, last=last, **kwargs)
        return dict(status='complete', pin=dict(finality='confirmations:1000'))
    monkeypatch.setattr(catchup, 'catch_up', acquire)
    monkeypatch.setattr(cli, 'price_unpriced_reports', lambda *args, **kwargs: pytest.fail('unexpected pricing'))
    assert cli.main(['--db', str(tmp_path/'test.sqlite'), 'catch-up', '--chain', 'kat',
        '--from-block', '1', '--to-block', '2', '--confirmations', '1000', '--discover']) == 0
    assert captured == dict(chain='kat', first=1, last=2, source='auto', confirmations=1000,
                            chunk_size=50000, discover=True, experimental=False)
    assert json.loads(capsys.readouterr().out)['pin']['finality'] == 'confirmations:1000'


def test_existing_reports_deduplicate_and_conflicts_leave_no_coverage(db):
    from yearn_data.history_sources import write_rows
    client = Source()
    vault = dict(db.execute("SELECT * FROM vaults WHERE address=?", (VAULT,)).fetchone())
    report = client.reports(vault, 10, 30, "rpc")[0]
    with db:
        assert write_rows(db, "strategy_reports", [report]) == 1
    result = catchup.catch_up(db, "op", 10, 30, client=client)
    assert result["inserted_rows"] == 0
    db.execute("DELETE FROM history_coverage")
    db.execute("UPDATE strategy_reports SET gain_raw='13'")
    db.commit()
    with pytest.raises(ValueError, match="conflicting observation"):
        catchup.catch_up(db, "op", 10, 30, client=client)
    assert db.execute("SELECT COUNT(*) FROM history_coverage").fetchone()[0] == 0
    assert db.execute("SELECT gain_raw FROM strategy_reports").fetchone()[0] == '13'
