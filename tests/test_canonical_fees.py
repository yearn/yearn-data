import copy

import json

from pathlib import Path

import re

import pytest

from yearn_data.analysis import run_lifetime_yield

from yearn_data.cli import main

from yearn_data.fees import (
    REPORT_TOPICS, decode_v2_receipt, index_canonical_fees,
    normalize_allocator_fees, normalize_v2_fees, run_canonical_fees,
)

from yearn_data.storage import connect, init_db

ROOT = Path(__file__).parent / 'fixtures' / 'fees'

CASES = json.loads((ROOT/'cases.json').read_text())

EXPECTED = {r['event_id']: r for r in json.loads((ROOT/'expected.json').read_text())}

RECEIPTS = {(r['chain_id'],r['transaction_hash']):r['receipt'] for r in json.loads((ROOT/'receipts.json').read_text())}

DIRECT = [c for c in CASES if c['event']['contract_family']=='yearn-v3-allocator' or c['event']['event_name']=='FeeReport']

def snake(value):
    if isinstance(value,dict):
        return {re.sub(r'(?<!^)(?=[A-Z])','_',k).lower():snake(v) for k,v in value.items()}
    return value

def seed(conn, cases=CASES):
    inventory = {(r['chain_id'],r['address']):r for r in json.loads((ROOT/'inventory.json').read_text())}
    identities = {}
    for c in cases:
        e = c['event']
        if e['contract_family']=='yearn-v3-tokenized-strategy':
            continue  # Do not mix the Tokenized Strategy P&L layer into existing reports.
        index = e['log_index']
        if e['event_name']=='FeeReport':
            logs = RECEIPTS[(e['chain_id'],e['transaction_hash'])]['logs']
            index = min(int(l['logIndex'],16) for l in logs if l['address'].lower()==e['vault_address']
                        and l['topics'][0] in REPORT_TOPICS and int(l['logIndex'],16)>e['log_index'])
        v = inventory[(e['chain_id'],e['vault_address'])]
        version = 'v2' if e['contract_family']=='yearn-v2-vault' else 'v3'
        conn.execute('''INSERT OR IGNORE INTO vaults(chain_id,address,version,asset,asset_decimals,api_version,active,updated_at)
            VALUES (?,?,?,?,?,?,?,1)''',(e['chain_id'],e['vault_address'],version,e['asset_address'],e['asset_decimals'],e['api_version'],not v['is_retired']))
        conn.execute('''INSERT INTO strategy_reports(chain_id,version,vault_address,strategy_address,tx_hash,log_index,
            block_number,block_timestamp,asset,asset_decimals,gain_raw,loss_raw,net_raw,
            protocol_fees_raw,total_fees_raw,total_refunds_raw) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (e['chain_id'],version,e['vault_address'],e['strategy_address'],e['transaction_hash'],index,
             e['block_number'],e['block_timestamp'],e['asset_address'],e['asset_decimals'],e['gross_gain_raw'],e['loss_raw'],
             str(int(e['gross_gain_raw'])-int(e['loss_raw'])),e['protocol_fee_raw'],e['total_fees_paid_raw'],e['total_refunds_raw']))
        identities[e['id']] = (e['chain_id'],e['transaction_hash'],index)
    conn.commit()
    return identities

@pytest.fixture
def db(tmp_path):
    c = connect(tmp_path/'fees.sqlite')
    init_db(c)
    yield c
    c.close()

@pytest.mark.parametrize('case',DIRECT,ids=lambda c:str(c['event']['id']))
def test_direct_amounts_match_pinned_reference(case):
    e,a = case['event'],case['raw_log']['args']
    if e['contract_family']=='yearn-v3-allocator':
        actual = normalize_allocator_fees({'gain_raw':a['gain'],'loss_raw':a['loss'],'total_fees_raw':a['total_fees'],
            'protocol_fees_raw':a['protocol_fees'],'total_refunds_raw':a['total_refunds'],
            'strategy_address':a['strategy'],'vault_address':e['vault_address']})
    else:
        actual = normalize_v2_fees(e['gross_gain_raw'],e['loss_raw'],a['management_fee'],a['performance_fee'],a['strategist_fee'])
    assert actual == snake(EXPECTED[e['id']]['accounting'])

def test_index_reuses_reports_is_idempotent_and_keeps_unresolved(db):
    identities = seed(db)
    before = [tuple(r) for r in db.execute('SELECT * FROM strategy_reports')]
    requests = []
    def receipt(chain,tx):
        requests.append((chain,tx))
        return RECEIPTS[(chain,tx)]
    assert index_canonical_fees(db,receipt_fetcher=receipt) == len(identities)
    assert len(requests)==len(set(requests))
    for c in CASES:
        e = c['event']
        if e['id'] not in identities:
            continue
        row = db.execute('SELECT * FROM canonical_fee_reports WHERE chain_id=? AND tx_hash=? AND report_log_index=?',identities[e['id']]).fetchone()
        if e['id'] in (105890,51671):
            # The old service's gain==0 shortcut is not valid for these releases.
            assert row['status']=='unresolved' and row['reason']=='zero_gain_requires_fee_evidence'
        elif e['amount_source']=='observed-event':
            assert row['status']=='ok'
            expected=snake(EXPECTED[e['id']]['accounting'])
            if e['event_name']=='StrategyReported' and e['contract_family']=='yearn-v2-vault':
                expected['amount_source']='contract-zero-gain'
            assert json.loads(row['accounting_json'])==expected
            assert row['event_log_index']==e['log_index']
        elif e['gross_gain_raw']!='0':
            assert row['status']=='unresolved'
            assert row['accounting_json'] is None
    requests.clear()
    assert index_canonical_fees(db,receipt_fetcher=receipt)==0
    assert requests==[]
    assert [tuple(r) for r in db.execute('SELECT * FROM strategy_reports')]==before

def test_failed_receipt_is_retryable_without_inventing_zero(db):
    c = next(c for c in DIRECT if c['event']['event_name']=='FeeReport')
    seed(db,[c])
    def unavailable(*args):
        raise RuntimeError('https://private-rpc.example/secret-key')
    index_canonical_fees(db,receipt_fetcher=unavailable)
    r=db.execute('SELECT * FROM canonical_fee_reports').fetchone()
    assert r['status']=='unresolved' and r['accounting_json'] is None
    assert 'secret' not in str(tuple(r))
    index_canonical_fees(db,retry_unresolved=True,receipt_fetcher=lambda chain,tx:RECEIPTS[(chain,tx)])
    assert db.execute('SELECT status FROM canonical_fee_reports').fetchone()[0]=='ok'

def test_missing_allocator_fields_and_impossible_protocol_fee_do_not_become_zero(db):
    c = next(c for c in DIRECT if c['event']['contract_family']=='yearn-v3-allocator')
    seed(db,[c])
    db.execute('UPDATE strategy_reports SET total_fees_raw=NULL')
    index_canonical_fees(db)
    assert db.execute('SELECT status FROM canonical_fee_reports').fetchone()[0]=='unresolved'
    db.execute("UPDATE strategy_reports SET total_fees_raw='1',protocol_fees_raw='2'")
    index_canonical_fees(db,retry_unresolved=True)
    assert db.execute('SELECT status FROM canonical_fee_reports').fetchone()[0]=='unresolved'

def test_canonical_exports_include_retired_and_not_indexed_reports_without_usd(db,tmp_path):
    seed(db)
    index_canonical_fees(db,limit=1,receipt_fetcher=lambda chain,tx:RECEIPTS[(chain,tx)])
    run = run_canonical_fees(db)
    rows = [json.loads(r[0]) for r in db.execute("SELECT row_json FROM analysis_outputs WHERE run_id=? AND name='canonical_fee_events'",(run,))]
    assert len(rows)==db.execute('SELECT count(*) FROM strategy_reports').fetchone()[0]
    assert any(r['status']=='not_indexed' and r['total_fees_paid_raw'] is None for r in rows)
    retired = db.execute('SELECT address FROM vaults WHERE active=0').fetchone()[0]
    assert any(r['vault_address']==retired for r in rows)
    assert all('price_usd' not in r for r in rows)
    counts = [json.loads(r[0]) for r in db.execute("SELECT row_json FROM analysis_outputs WHERE run_id=? AND name='canonical_fee_coverage'",(run,))]
    assert sum(r['not_indexed_reports'] for r in counts)==len(rows)-1
    from yearn_data.exports import export_analysis
    assert len(export_analysis(db,'canonical-fees',tmp_path/'export'))==3

def test_fee_projection_does_not_change_lifetime_results(db):
    seed(db)
    def pnl():
        run = run_lifetime_yield(db)
        return [tuple(r) for r in db.execute('SELECT name,row_json FROM analysis_outputs WHERE run_id=? ORDER BY name,row_json',(run,))]
    before = pnl()
    index_canonical_fees(db,receipt_fetcher=lambda chain,tx:RECEIPTS[(chain,tx)])
    assert pnl()==before

def test_decoder_rejects_ambiguous_fee_pairing():
    c = next(c for c in DIRECT if c['event']['event_name']=='FeeReport')
    e = c['event']
    receipt = copy.deepcopy(RECEIPTS[(e['chain_id'],e['transaction_hash'])])
    fee = next(l for l in receipt['logs'] if int(l['logIndex'],16)==e['log_index'])
    receipt['logs'].append(copy.deepcopy(fee))
    with pytest.raises(ValueError,match='ambiguous'):
        decode_v2_receipt(receipt,e['vault_address'])

def test_cli_can_project_and_export_allocator_fees_offline(tmp_path):
    path=tmp_path/'cli.sqlite'
    conn=connect(path);init_db(conn)
    seed(conn,[c for c in DIRECT if c['event']['contract_family']=='yearn-v3-allocator'])
    conn.close()
    assert main(['--db',str(path),'index-fees','--canonical'])==0
    assert main(['--db',str(path),'analyze','canonical-fees'])==0
    assert main(['--db',str(path),'export','canonical-fees','--out',str(tmp_path/'export')])==0
    assert (tmp_path/'export'/'canonical_fee_events.csv').exists()

def test_direct_fee_pairing_keeps_two_reports_in_same_transaction_separate():
    c = next(c for c in DIRECT if c['event']['event_name']=='FeeReport')
    e = c['event']
    original = RECEIPTS[(e['chain_id'],e['transaction_hash'])]
    fee = next(l for l in original['logs'] if int(l['logIndex'],16)==e['log_index'])
    report = next(l for l in original['logs'] if l['address'].lower()==e['vault_address']
                  and l['topics'][0] in REPORT_TOPICS and int(l['logIndex'],16)>e['log_index'])
    logs = []
    for position,template in [(10,fee),(11,report),(20,fee),(21,report)]:
        log=copy.deepcopy(template);log['logIndex']=hex(position);logs.append(log)
    pairs=decode_v2_receipt({'logs':list(reversed(logs))},e['vault_address'])
    assert pairs[11]['fee']['log_index']==10
    assert pairs[21]['fee']['log_index']==20

def test_canonical_projection_is_removed_with_its_source_report(db):
    seed(db,[c for c in DIRECT if c['event']['contract_family']=='yearn-v3-allocator'])
    index_canonical_fees(db)
    init_db(db)  # Schema migration is repeatable and preserves existing projections.
    assert db.execute('SELECT count(*) FROM canonical_fee_reports').fetchone()[0] > 0
    db.execute('DELETE FROM strategy_reports')
    assert db.execute('SELECT count(*) FROM canonical_fee_reports').fetchone()[0]==0

def test_receipt_from_wrong_transaction_remains_unresolved(db):
    c = next(c for c in DIRECT if c['event']['event_name']=='FeeReport')
    seed(db,[c])
    receipt=copy.deepcopy(RECEIPTS[(c['event']['chain_id'],c['event']['transaction_hash'])])
    receipt['transactionHash']='0x'+'00'*32
    index_canonical_fees(db,receipt_fetcher=lambda *args:receipt)
    assert db.execute('SELECT accounting_json FROM canonical_fee_reports').fetchone()[0] is None

def test_unmatched_positive_gain_is_not_inferred_zero(db):
    c = next(c for c in DIRECT if c['event']['event_name']=='FeeReport')
    seed(db,[c])
    receipt=copy.deepcopy(RECEIPTS[(c['event']['chain_id'],c['event']['transaction_hash'])])
    receipt['logs']=[l for l in receipt['logs'] if int(l['logIndex'],16)!=c['event']['log_index']]
    index_canonical_fees(db,receipt_fetcher=lambda *args:receipt)
    row=db.execute('SELECT * FROM canonical_fee_reports').fetchone()
    assert row['status']=='unresolved'
    assert row['accounting_json'] is None
    assert row['reason']=='missing_fee_report'

def test_synthetic_allocator_self_loss_is_observed_without_invented_fees():
    vault='0x'+'12'*20
    amounts=normalize_allocator_fees({'vault_address':vault,'strategy_address':vault,
        'gain_raw':'0','loss_raw':'150','total_fees_raw':'0','protocol_fees_raw':'0','total_refunds_raw':'0'})
    assert amounts['report_kind']=='self-report'
    assert amounts['yield_mechanism']=='self-report-loss'
    assert amounts['loss_raw']=='150' and amounts['gross_gain_raw']=='0'
    assert amounts['total_fees_paid_raw']=='0' and amounts['amount_source']=='observed-event'

def test_complete_filtered_log_pairing_matches_receipt_decoder():
    from yearn_data.fees import decode_v2_report_logs, FEE_TOPIC
    for case in DIRECT:
        e = case['event']
        if e['event_name'] != 'FeeReport':
            continue
        receipt = RECEIPTS[(e['chain_id'], e['transaction_hash'])]
        logs = [log for log in receipt['logs'] if log['address'].lower() == e['vault_address'].lower()
                and log['topics'] and log['topics'][0] in {FEE_TOPIC, *REPORT_TOPICS}]
        assert decode_v2_report_logs(logs, e['vault_address']) == decode_v2_receipt(receipt, e['vault_address'])

def test_filtered_pairing_rejects_mixed_transactions():
    from yearn_data.fees import decode_v2_report_logs, FEE_TOPIC
    e = next(c['event'] for c in DIRECT if c['event']['event_name'] == 'FeeReport')
    receipt = RECEIPTS[(e['chain_id'], e['transaction_hash'])]
    logs = [copy.deepcopy(log) for log in receipt['logs'] if log['address'].lower() == e['vault_address'].lower()
            and log['topics'] and log['topics'][0] in {FEE_TOPIC, *REPORT_TOPICS}]
    for log in logs:
        log['transactionHash'] = '0x' + ('11' if log['topics'][0] == FEE_TOPIC else '22') * 32
    with pytest.raises(ValueError, match='one transaction'):
        decode_v2_report_logs(logs, e['vault_address'])
