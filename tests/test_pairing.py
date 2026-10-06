"""Consumer views and atomic selection over completed financial results."""
import json

import pytest

from yearn_data.analysis import create_analysis_run, complete_analysis_run, write_output
from yearn_data.cli import main
from yearn_data.fee_valuation import POLICY
from yearn_data.pairing import PairingDataset, PairingStore, pairing_response, select_pairing
from yearn_data.storage import connect, init_db

DAY = 1704067200
CUTOFF = DAY + 32 * 86400
ASSET = '0x' + '11' * 20
VAULT = '0x' + '22' * 20
TOKENIZED = '0x' + '33' * 20


def snapshots(db, *, fallback=None, fee_cutoff=CUTOFF, fee_price='1'):
    earnings = create_analysis_run(db, 'lifetime-yield', {
        'price_source': 'yearn-prices', 'fallback_price_source': fallback, 'before_timestamp': CUTOFF})
    fees = create_analysis_run(db, 'fee-usd', {
        'provider': 'yearn-prices', 'policy': POLICY, 'before_timestamp': fee_cutoff})
    def report(number, timestamp, gain, loss, *, chain=1, price=1):
        row = {'chain_id': chain, 'vault_address': VAULT, 'version': 'v3', 'asset': ASSET,
               'tx_hash': f'0x{number:064x}', 'log_index': 0,
               'block_timestamp': timestamp, 'price_usd': price,
               'valuation_status': 'priced' if price else 'missing_price',
               'gross_gain_usd': gain, 'loss_usd': loss,
               'net_yield_usd': str(int(gain)-int(loss)) if gain is not None and loss is not None else None,
               'raw_gross_gain_usd': gain, 'raw_loss_usd': loss,
               'raw_net_yield_usd': str(int(gain)-int(loss)) if gain is not None and loss is not None else None}
        write_output(db, earnings, 'reports', row)
        return row
    def fee(row, amount, *, tokenized=False):
        event = {key: row[key] for key in ('chain_id','vault_address','asset','tx_hash','log_index','block_timestamp')}
        event.update(asset_decimals=0, contract_family='yearn-v3-tokenized-strategy' if tokenized else 'yearn-v3-allocator',
                     total_fees_paid_usd=amount, price_usd=fee_price if row['price_usd'] else None,
                     accounting_status='accepted' if amount is not None else 'deferred',
                     valuation_status='ok' if amount is not None else 'accounting_unavailable')
        write_output(db, fees, 'fee_usd_events', event)
    first = report(1, DAY, '90', '0');fee(first, '9')
    child = dict(first, tx_hash=f'0x{2:064x}', vault_address=TOKENIZED);fee(child, '10', tokenized=True)
    unresolved = report(3, DAY+86400, '20', '5');fee(unresolved, None)
    last = report(4, DAY+31*86400, '30', '0', chain=10);fee(last, '3')
    unknown = report(5, DAY+86400, None, None, chain=10, price=None);fee(unknown, None)
    for run in (earnings, fees):complete_analysis_run(db, run)
    return earnings, fees


@pytest.fixture
def publication(tmp_path):
    db = connect(tmp_path/'source.sqlite');init_db(db)
    runs = snapshots(db)
    root = tmp_path/'selected'
    yield db, runs, root
    db.close()


def test_nested_fees_and_earnings_independent_of_fee_acceptance(publication):
    db, runs, root = publication
    path = db.execute('PRAGMA database_list').fetchone()[2]
    assert main(['--db', path, 'select-pairing', '--earnings-run-id', str(runs[0]),
                 '--fees-run-id', str(runs[1]), '--out', str(root)]) == 0
    dataset = PairingStore(root).get()
    summary = dataset.view('summary')
    assert summary['totalFeesPaidUsd'] == '22'
    assert summary['grossGainsUsd'] == '140'
    assert summary['lossesUsd'] == '5'
    assert summary['netLifetimeEarningsUsd'] == '135'
    assert [row['totalFeesPaidUsd'] for row in summary['byContractFamily']] == ['12','10']
    assert 'feeCoverage' not in summary and 'diagnostics' not in summary
    assert 'tokenizedStrategyYield' not in summary
    assert dataset.context['diagnostics']['accounting:deferred'] == 2
    status, result = pairing_response(PairingStore(root), '/api/fees?chainId=1')
    assert status == 200 and result['totalFeesPaidUsd'] == '19'
    assert result['netLifetimeEarningsUsd'] == '105'


def test_bounds_months_vault_identity_and_unavailable_values(publication):
    db, runs, _ = publication
    dataset = PairingDataset(db.execute('PRAGMA database_list').fetchone()[2], *runs)
    january = dataset.view('history', until=DAY+31*86400)
    assert [row['period'] for row in january['buckets']] == ['2024-01']
    assert january['buckets'][0]['totalFeesPaidUsd'] == '19'
    february = dataset.view('summary', since=DAY+31*86400, until=CUTOFF, chains=(10,))
    assert february['totalFeesPaidUsd'] == '3'
    unknown = dataset.view('summary', since=DAY+86400, until=DAY+2*86400, chains=(10,))
    assert unknown['totalFeesPaidUsd'] is None and unknown['netLifetimeEarningsUsd'] is None
    vaults = dataset.view('vaults')['vaults']
    assert {(v['chainId'],v['address']) for v in vaults} == {(1,VAULT),(1,TOKENIZED),(10,VAULT)}
    assert all(v['name'] is None for v in vaults)
    assert all('tvlUsd' not in v for v in vaults)


def test_published_zero_fees_do_not_require_prices_or_known_components(publication):
    db, runs, _ = publication
    db.execute("""UPDATE analysis_outputs SET row_json=json_set(row_json,
        '$.total_fees_paid_usd','0','$.price_usd',NULL,'$.accounting_status','accepted',
        '$.valuation_status','zero_fee') WHERE run_id=? AND name='fee_usd_events'""", (runs[1],))
    db.commit()
    summary = PairingDataset(db.execute('PRAGMA database_list').fetchone()[2], *runs).view('summary')
    assert summary['totalFeesPaidUsd'] == '0'
    assert summary['protocolFeesUsd'] is None
    assert summary['netLifetimeEarningsUsd'] == '135'


@pytest.mark.parametrize('problem', ['cutoff','fallback','price','unfinished','cohort','duplicate'])
def test_failed_selection_retains_previous_dataset(publication, problem):
    db, runs, root = publication
    path = db.execute('PRAGMA database_list').fetchone()[2]
    selected = select_pairing(path, *runs, root)
    previous = (root/'current.json').read_bytes()
    other = snapshots(db, fallback='defillama' if problem=='fallback' else None,
                      fee_cutoff=CUTOFF+86400 if problem=='cutoff' else CUTOFF,
                      fee_price='2' if problem=='price' else '1')
    if problem=='unfinished':db.execute("UPDATE analysis_runs SET status='running' WHERE id=?", (other[0],))
    if problem=='cohort':db.execute("DELETE FROM analysis_outputs WHERE run_id=? AND name='reports' AND json_extract(row_json,'$.chain_id')=10", (other[0],))
    if problem=='duplicate':
        row = db.execute("SELECT row_json FROM analysis_outputs WHERE run_id=? AND name='fee_usd_events' LIMIT 1", (other[1],)).fetchone()[0]
        write_output(db, other[1], 'fee_usd_events', json.loads(row))
    db.commit()
    with pytest.raises(ValueError):select_pairing(path, *other, root)
    assert (root/'current.json').read_bytes() == previous
    assert PairingStore(root).get().id == selected['datasetId']


def test_pin_survives_refresh_and_invalid_requests_are_rejected(publication):
    db, runs, root = publication
    path = db.execute('PRAGMA database_list').fetchone()[2]
    first = select_pairing(path, *runs, root)
    second = select_pairing(path, *snapshots(db), root)
    store = PairingStore(root)
    status, result = pairing_response(store, '/api/fees/history?datasetId='+first['datasetId'])
    assert status == 200 and result['datasetId'] == first['datasetId']
    assert store.get().id == second['datasetId']
    for query in ('chainId=999','chainId=1.5','since=abc','since=20&until=10','interval=weekly','since=1&since=2','x=1'):
        assert pairing_response(store, '/api/fees?'+query)[0] == 400
    assert pairing_response(store, '/api/fees/stack')[0] == 404


def test_published_output_changes_are_detected(publication):
    db, runs, root = publication
    path = db.execute('PRAGMA database_list').fetchone()[2]
    select_pairing(path, *runs, root)
    db.execute("UPDATE analysis_outputs SET row_json=json_set(row_json,'$.total_fees_paid_usd','999') WHERE run_id=? AND name='fee_usd_events'", (runs[1],))
    db.commit()
    with pytest.raises(ValueError, match='changed since publication'):PairingStore(root).get()


def test_interrupted_activation_keeps_previous_dataset(publication, monkeypatch):
    from pathlib import Path

    db, runs, root = publication
    path = db.execute('PRAGMA database_list').fetchone()[2]
    first = select_pairing(path, *runs, root)
    next_runs = snapshots(db)
    replace = Path.replace

    def interrupt_activation(source, target):
        if Path(target) == root/'current.json':
            raise OSError('interrupted activation')
        return replace(source, target)

    monkeypatch.setattr(Path, 'replace', interrupt_activation)
    with pytest.raises(OSError, match='interrupted activation'):
        select_pairing(path, *next_runs, root)
    assert PairingStore(root).get().id == first['datasetId']
