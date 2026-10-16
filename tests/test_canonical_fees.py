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


def test_reconstruction_retry_matches_all_older_cases_and_preserves_direct(db):
    identities = seed(db)
    fetch = lambda chain, tx: RECEIPTS[chain, tx]
    index_canonical_fees(db, receipt_fetcher=fetch)
    observed = [tuple(r) for r in db.execute("SELECT * FROM canonical_fee_reports WHERE status='ok'")]
    states = {(c['event']['chain_id'], c['event']['transaction_hash'], c['event']['log_index']): c['reconstruction']
              for c in CASES if c['reconstruction']}
    calls = []
    def state(report, receipt, shares):
        key = (report['chain_id'], report['tx_hash'], report['log_index'])
        calls.append(key)
        return states[key]
    assert index_canonical_fees(db, receipt_fetcher=fetch, state_reader=state,
                                reconstruct_v2=True, retry_unresolved=True) == 8
    assert len(calls) == 6  # Retain candidates even when acceptance is rejected.
    for c in CASES:
        e = c['event']
        if e['amount_source'] != 'derived-contract':
            continue
        row = db.execute('SELECT * FROM canonical_fee_reports WHERE chain_id=? AND tx_hash=? AND report_log_index=?',
                         identities[e['id']]).fetchone()
        candidate = json.loads(row['candidate_json'])
        decision = json.loads(row['decision_json'])
        assert candidate['total_fees_paid_raw'] == EXPECTED[e['id']]['accounting']['totalFeesPaidRaw']
        assert candidate['method'] == EXPECTED[e['id']]['method']
        assert 'quality' not in candidate
        if e['id'] == 84180:
            assert row['status'] == 'ok' and decision['status'] == 'accepted'
        else:
            assert row['status'] == 'unresolved' and row['accounting_json'] is None
            assert decision['status'] == ('rejected' if e['id'] == 82067 else 'conditional')
    for row in observed:
        assert row in [tuple(r) for r in db.execute('SELECT * FROM canonical_fee_reports')]
    assert index_canonical_fees(db, reconstruct_v2=True, receipt_fetcher=fetch, state_reader=state) == 0
    run_canonical_fees(db)


def test_archive_failure_stays_unresolved_without_leaking_endpoint(db):
    case = next(c for c in CASES if c['event']['id'] == 82042)
    seed(db, [case])
    def fail(*args):
        raise RuntimeError('https://private.example/secret')
    index_canonical_fees(db, receipt_fetcher=lambda c,t: RECEIPTS[c,t], state_reader=fail, reconstruct_v2=True)
    row = db.execute('SELECT * FROM canonical_fee_reports').fetchone()
    assert row['status'] == 'unresolved'
    assert row['reason'] == 'historical_fee_state_unavailable_or_invalid'
    assert 'secret' not in row['evidence_json']


def test_later_report_prevents_end_of_block_state_promotion(db):
    case = next(c for c in CASES if c['event']['id'] == 82042)
    event = case['event']
    seed(db, [case])
    receipt = copy.deepcopy(RECEIPTS[event['chain_id'], event['transaction_hash']])
    report_log = copy.deepcopy(next(log for log in receipt['logs'] if int(log['logIndex'], 16) == event['log_index']))
    report_log['logIndex'] = hex(max(int(log['logIndex'], 16) for log in receipt['logs']) + 1)
    receipt['logs'].append(report_log)
    calls = []
    index_canonical_fees(db, receipt_fetcher=lambda *args: receipt, reconstruct_v2=True,
                         state_reader=lambda *args: (calls.append(args), case['reconstruction'])[1])
    row = db.execute('SELECT * FROM canonical_fee_reports').fetchone()
    assert row['status'] == 'unresolved'
    assert row['reason'] == 'later_vault_report_in_receipt'
    assert row['accounting_json'] is None
    assert len(calls) == 1
    assert json.loads(row['candidate_json'])['total_fees_paid_raw'] == EXPECTED[event['id']]['accounting']['totalFeesPaidRaw']
    run = run_canonical_fees(db)
    distributions = [json.loads(r[0]) for r in db.execute(
        "SELECT row_json FROM analysis_outputs WHERE run_id=? AND name='canonical_fee_share_distributions'", (run,))]
    assert distributions
    assert all(r['fee_status'] == 'unresolved' and r['fee_reason'] == 'later_vault_report_in_receipt' for r in distributions)
    summaries = [json.loads(r[0]) for r in db.execute(
        "SELECT row_json FROM analysis_outputs WHERE run_id=? AND name='canonical_fees_by_asset'", (run,))]
    assert summaries[0]['known_fee_subtotal_raw'] == '0'
    assert summaries[0]['unknown_reports'] == 1


def test_reconstruction_upgrade_rechecks_derived_rows_without_replacing_observed(db):
    case = next(c for c in CASES if c['event']['id'] == 82067)
    seed(db, [case])
    event = case['event']
    db.execute('''INSERT INTO canonical_fee_reports
        (chain_id,tx_hash,report_log_index,contract_family,api_version,method_version,status,accounting_json,evidence_json)
        VALUES (?,?,?,'yearn-v2-vault','0.3.2','canonical-fees-2','ok',?,'{}')''',
        (event['chain_id'],event['transaction_hash'],event['log_index'],json.dumps({'amount_source':'derived-contract'})))
    assert index_canonical_fees(db, chains=['eth'], reconstruct_v2=True,
                                receipt_fetcher=lambda c,t: RECEIPTS[c,t], state_reader=lambda *args: case['reconstruction']) == 1
    row = db.execute('SELECT * FROM canonical_fee_reports').fetchone()
    assert row['status'] == 'unresolved'
    assert row['reason'] == 'later_vault_report_in_receipt'


def test_candidate_exports_never_inflate_accepted_totals(db):
    cases = [c for c in CASES if c['event']['id'] in (82042, 82067, 84180)]
    seed(db, cases)
    states = {c['event']['log_index']: c['reconstruction'] for c in cases}
    index_canonical_fees(db, reconstruct_v2=True, receipt_fetcher=lambda c,t: RECEIPTS[c,t],
                         state_reader=lambda r,*args: states[r['log_index']])
    run = run_canonical_fees(db)
    def outputs(name):
        return [json.loads(r[0]) for r in db.execute('SELECT row_json FROM analysis_outputs WHERE run_id=? AND name=?',(run,name))]
    rows = outputs('canonical_fee_events')
    assert sorted(r['acceptance_status'] for r in rows) == ['accepted','conditional','rejected']
    for r in rows:
        assert r['candidate_fee_raw'] is not None
        if r['acceptance_status'] != 'accepted':
            assert r['total_fees_paid_raw'] is None
            assert r['state_scope'] == 'end-of-block'
            assert r['calculation_precision'] == 'singleton'
    totals = outputs('canonical_fees_by_asset')
    assert sum(int(r['known_fee_subtotal_raw']) for r in totals) == 0
    assert sum(r['unknown_reports'] for r in totals) == 2


def test_init_migrates_legacy_derived_amount_without_losing_evidence(db):
    case = next(c for c in CASES if c['event']['id'] == 82042)
    seed(db, [case])
    e = case['event']
    legacy = {'reconstruction': {'method':'contract-state-singleton','quality':'exact-state',
              'total_fees_paid_raw':'65525','interval':{'lower_raw':'65525','upper_raw':'65525','width_raw':'0'}},
              'historical_state':case['reconstruction']}
    db.execute('''INSERT INTO canonical_fee_reports
        (chain_id,tx_hash,report_log_index,contract_family,method_version,status,accounting_json,evidence_json)
        VALUES (?,?,?,'yearn-v2-vault','canonical-fees-2','ok',?,?)''',
        (e['chain_id'],e['transaction_hash'],e['log_index'],
         json.dumps({'amount_source':'derived-contract','total_fees_paid_raw':'65525'}),json.dumps(legacy)))
    init_db(db)
    row = db.execute('SELECT * FROM canonical_fee_reports').fetchone()
    assert row['accounting_json'] is None and row['status'] == 'unresolved'
    assert json.loads(row['candidate_json'])['precision'] == 'singleton'
    assert json.loads(row['decision_json'])['status'] == 'conditional'
    assert json.loads(row['evidence_json'])['historical_state'] == case['reconstruction']
    before = tuple(row)
    init_db(db)
    assert tuple(db.execute('SELECT * FROM canonical_fee_reports').fetchone()) == before


def test_retries_reuse_bound_receipt_and_state_until_explicit_refresh(db):
    case=next(c for c in CASES if c['event']['id']==82042);seed(db,[case])
    calls={'receipt':0,'state':0}
    def receipt(chain,tx):calls['receipt']+=1;return RECEIPTS[chain,tx]
    def state(*args):calls['state']+=1;return case['reconstruction']
    kwargs=dict(reconstruct_v2=True,retry_unresolved=True,receipt_fetcher=receipt,state_reader=state)
    index_canonical_fees(db,**kwargs)
    index_canonical_fees(db,**kwargs)
    assert calls=={'receipt':1,'state':1}
    index_canonical_fees(db,refresh_evidence=True,**kwargs)
    assert calls=={'receipt':2,'state':2}
    # A damaged cached getter input is refetched without refetching the receipt.
    row=db.execute('SELECT evidence_json FROM canonical_fee_reports').fetchone()
    evidence=json.loads(row[0]);evidence['historical_state']['pre_total_supply_raw']=None
    db.execute('UPDATE canonical_fee_reports SET evidence_json=?',(json.dumps(evidence),))
    index_canonical_fees(db,**kwargs)
    assert calls=={'receipt':2,'state':3}


def test_offline_recomputation_restores_formula_but_policy_only_preserves_candidate(db,monkeypatch):
    from yearn_data.fee_recompute import recompute_canonical_fees
    import yearn_data.fees as fees
    case=next(c for c in CASES if c['event']['id']==82042);seed(db,[case])
    index_canonical_fees(db,reconstruct_v2=True,receipt_fetcher=lambda c,t:RECEIPTS[c,t],state_reader=lambda *a:case['reconstruction'])
    row=db.execute('SELECT candidate_json FROM canonical_fee_reports').fetchone()
    candidate=json.loads(row[0]);candidate['total_fees_paid_raw']='123'
    db.execute('UPDATE canonical_fee_reports SET candidate_json=?',(json.dumps(candidate),))
    def no_network(*args):raise AssertionError('offline command attempted network')
    monkeypatch.setattr(fees,'web3_for',no_network)
    assert recompute_canonical_fees(db,'policy')['conditional']==1
    assert json.loads(db.execute('SELECT candidate_json FROM canonical_fee_reports').fetchone()[0])['total_fees_paid_raw']=='123'
    assert recompute_canonical_fees(db,'formulas',chains=['eth'],limit=1)['conditional']==1
    assert json.loads(db.execute('SELECT candidate_json FROM canonical_fee_reports').fetchone()[0])['total_fees_paid_raw']=='65525'
    db.execute("UPDATE strategy_reports SET gain_raw='999'")
    assert recompute_canonical_fees(db,'formulas')['unavailable']==1
    row=db.execute('SELECT accounting_json,reason FROM canonical_fee_reports').fetchone()
    assert row['accounting_json'] is None and row['reason']=='cached_fee_inputs_missing_or_changed'


def test_offline_formula_refresh_keeps_execution_fee_and_updates_only_baseline(db):
    from yearn_data.fee_recompute import recompute_canonical_fees
    case=next(c for c in CASES if c['event']['id']==82042);seed(db,[case])
    index_canonical_fees(db,reconstruct_v2=True,receipt_fetcher=lambda c,t:RECEIPTS[c,t],state_reader=lambda *a:case['reconstruction'])
    row=db.execute('SELECT * FROM canonical_fee_reports').fetchone();evidence=json.loads(row['evidence_json'])
    evidence['execution_verification']={'status':'verified','fee_operand_raw':'65524'}
    candidate={'method':'execution-fee-mint','total_fees_paid_raw':'65524','baseline_candidate':{'total_fees_paid_raw':'123'}}
    db.execute('UPDATE canonical_fee_reports SET candidate_json=?,evidence_json=?',(json.dumps(candidate),json.dumps(evidence)))
    assert recompute_canonical_fees(db,'formulas')['accepted']==1
    row=db.execute('SELECT * FROM canonical_fee_reports').fetchone()
    assert json.loads(row['candidate_json'])['baseline_candidate']['total_fees_paid_raw']=='65525'
    assert json.loads(row['accounting_json'])['total_fees_paid_raw']=='65524'


def test_projection_rebuild_reuses_state_and_detects_changed_release(db):
    from yearn_data.fee_recompute import recompute_canonical_fees
    case=next(c for c in CASES if c['event']['id']==82042)
    seed(db,[case])
    index_canonical_fees(db,reconstruct_v2=True,
        receipt_fetcher=lambda c,t:RECEIPTS[c,t],state_reader=lambda *a:case['reconstruction'])
    before=tuple(db.execute('SELECT * FROM canonical_fee_reports').fetchone())
    db.execute('DELETE FROM canonical_fee_reports')
    def no_rpc(*args):
        raise AssertionError('projection rebuild attempted RPC')
    assert index_canonical_fees(db,reconstruct_v2=True,receipt_fetcher=no_rpc,state_reader=no_rpc)==1
    assert tuple(db.execute('SELECT * FROM canonical_fee_reports').fetchone())==before
    db.execute("UPDATE vaults SET api_version='0.3.0'")
    assert recompute_canonical_fees(db,'policy')['unavailable']==1
    row=db.execute('SELECT accounting_json,reason FROM canonical_fee_reports').fetchone()
    assert row['accounting_json'] is None
    assert row['reason']=='cached_fee_inputs_missing_or_changed'


def test_synthetic_allocator_self_loss_is_observed_without_invented_fees():
    vault='0x'+'12'*20
    amounts=normalize_allocator_fees({'vault_address':vault,'strategy_address':vault,
        'gain_raw':'0','loss_raw':'150','total_fees_raw':'0','protocol_fees_raw':'0','total_refunds_raw':'0'})
    assert amounts['report_kind']=='self-report'
    assert amounts['yield_mechanism']=='self-report-loss'
    assert amounts['loss_raw']=='150' and amounts['gross_gain_raw']=='0'
    assert amounts['total_fees_paid_raw']=='0' and amounts['amount_source']=='observed-event'




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


def test_old_zero_gain_with_actual_fee_mint_is_not_accepted_zero(db):
    case=next(c for c in CASES if c['event']['id']==105890)
    seed(db,[case])
    index_canonical_fees(db,receipt_fetcher=lambda chain,tx:RECEIPTS[chain,tx])
    r=db.execute('SELECT * FROM canonical_fee_reports').fetchone()
    assert r['accounting_json'] is None and r['reason']=='zero_gain_requires_fee_evidence'
    from yearn_data.fee_reconstruction import extract_fee_shares
    e=case['event']
    shares=extract_fee_shares(RECEIPTS[e['chain_id'],e['transaction_hash']]['logs'],e['vault_address'],e['log_index'])
    assert shares['status']=='found' and int(shares['minted_shares_raw'])>0
    # Simulate the previous implementation's persisted zero and verify migration.
    db.execute("UPDATE canonical_fee_reports SET status='ok',reason=NULL,accounting_json=?,method_version='canonical-fees-5'",
        (json.dumps(normalize_v2_fees(0,0,0,0,0)),))
    db.commit();init_db(db)
    r=db.execute('SELECT * FROM canonical_fee_reports').fetchone()
    assert r['accounting_json'] is None and r['reason']=='zero_gain_requires_fee_evidence'


def test_old_zero_gain_requires_and_accepts_no_mint_receipt(db):
    case=next(c for c in CASES if c['event']['id']==51671)
    seed(db,[case])
    index_canonical_fees(db,reconstruct_v2=True,receipt_fetcher=lambda chain,tx:RECEIPTS[chain,tx])
    r=db.execute('SELECT * FROM canonical_fee_reports').fetchone()
    assert r['status']=='ok' and json.loads(r['accounting_json'])['total_fees_paid_raw']=='0'
    assert json.loads(r['candidate_json'])['method']=='receipt-no-mint-zero'


def test_report_key_scope_preserves_unselected_reports_and_accepts_033_no_mint(db):
    identities=seed(db)
    key=identities[51671]
    db.execute("UPDATE vaults SET api_version='0.3.3' WHERE chain_id=? AND address=(SELECT vault_address FROM strategy_reports WHERE chain_id=? AND tx_hash=? AND log_index=?)",(key[0],*key))
    def no_state(*args):pytest.fail('no-mint receipt requested state')
    assert index_canonical_fees(db,report_keys=[key],reconstruct_v2=True,
        receipt_fetcher=lambda chain,tx:RECEIPTS[chain,tx],state_reader=no_state)==1
    assert db.execute('SELECT count(*) FROM canonical_fee_reports').fetchone()[0]==1
    row=db.execute('SELECT * FROM canonical_fee_reports').fetchone()
    assert row['status']=='ok' and json.loads(row['candidate_json'])['method']=='receipt-no-mint-zero'
    from yearn_data.fee_recompute import recompute_canonical_fees
    assert recompute_canonical_fees(db,'formulas',report_keys=[key])['accepted']==1
    assert index_canonical_fees(db,report_keys=[])==0
    assert recompute_canonical_fees(db,'policy',report_keys=[])['processed']==0


def test_zero_share_fee_mint_does_not_establish_zero_asset_fee():
    from yearn_data.fee_reconstruction import extract_fee_shares
    case=next(c for c in CASES if c['event']['id']==105890)['event']
    logs=copy.deepcopy(RECEIPTS[case['chain_id'],case['transaction_hash']]['logs'])
    original=extract_fee_shares(logs,case['vault_address'],case['log_index'])
    for n in logs:
        if int(n['logIndex'],16)==original['mint_log_index']:
            n['data']='0x'+'00'*32
    assert extract_fee_shares(logs,case['vault_address'],case['log_index'])['status']=='zero-share-mint'


def test_cli_explicit_execution_keeps_report_scope(db, tmp_path, monkeypatch):
    import yearn_data.cli as cli
    keys = [[1, '0x' + 'AB' * 32, 4]]
    selector = tmp_path / 'keys.json'; selector.write_text(json.dumps(keys))
    captured = {}
    def index(conn, chains, **options):
        captured.update(options)
        return 0
    monkeypatch.setattr(cli, 'index_canonical_fees', index)
    database = db.execute('PRAGMA database_list').fetchone()['file']
    assert cli.main(['--db', database, 'index-fees', '--canonical', '--reconstruct-v2',
                     '--verify-selective', '--verify-execution', '--report-keys', str(selector),
                     '--trace-limit', '2']) == 0
    assert captured['report_keys'] == [(1, keys[0][1].lower(), 4)]
    assert captured['verify_execution'] is True and captured['trace_limit'] == 2
    selector.write_text('[[1,"invalid",4]]')
    with pytest.raises(ValueError, match='report keys'):
        cli.main(['--db', database, 'index-fees', '--canonical', '--report-keys', str(selector)])
