import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from eth_abi import encode
from eth_utils import keccak
from yearn_data.ybold_fees import accounting, ACCOUNTANT, STAKER
from yearn_data.fees import normalize_allocator_fees

CASES = json.loads((Path(__file__).parent/'fixtures/fees/ybold.json').read_text())

@pytest.mark.parametrize('case', CASES)
def test_historical_distributions_are_not_revenue(case):
    r = case['report']; a = accounting(r, case['evidence'])
    assert a['total_fees_paid_raw'] == a['manager_fee_raw'] == '0'
    assert a['staker_distribution_raw'] == a['reported_total_fees_raw'] == r['total_fees_raw']
    assert a['gross_gain_raw'] == r['gain_raw']
    assert a['loss_raw'] == r['loss_raw']

@pytest.mark.parametrize('field', ['accountant', 'recipient', 'accountant_code_hash', 'block_hash'])
def test_unknown_configuration_is_unresolved(field):
    c = copy.deepcopy(CASES[-1]); c['evidence'][field] = '0x' + '1'*64
    with pytest.raises(ValueError): accounting(c['report'], c['evidence'])

@pytest.mark.parametrize('mutation', ['transaction','block','status','missing','duplicate','amount','strategy'])
def test_receipt_binding(mutation):
    c = copy.deepcopy(CASES[-1]); r,e = c['report'],c['evidence']; receipt=e['receipt']
    if mutation=='transaction': receipt['transactionHash']='0x'+'1'*64
    if mutation=='block': receipt['blockNumber']=0
    if mutation=='status': receipt['status']=0
    if mutation=='missing': receipt['logs']=[]
    if mutation=='duplicate': receipt['logs'].append(receipt['logs'][0])
    if mutation=='amount': r['total_fees_raw']='1'
    if mutation=='strategy': r['strategy_address']='0x'+'1'*40
    with pytest.raises(ValueError): accounting(r,e)

def test_retains_real_protocol_fees():
    c = copy.deepcopy(CASES[-1]); r,e = c['report'],c['evidence']
    r['protocol_fees_raw']='100'
    log=next(l for l in e['receipt']['logs'] if l['logIndex']==r['log_index'])
    from eth_abi import decode
    values=list(decode(['uint256']*6,bytes.fromhex(log['data'][2:])));values[3]=100
    log['data']='0x'+encode(['uint256']*6,values).hex()
    a=accounting(r,e)
    assert a['total_fees_paid_raw']==a['protocol_fee_raw']=='100'
    assert int(a['staker_distribution_raw'])+100==int(r['total_fees_raw'])

def test_no_unverified_fallback():
    with pytest.raises(ValueError): normalize_allocator_fees(CASES[-1]['report'])

def test_index_and_offline_replay(tmp_path, monkeypatch):
    from yearn_data.storage import connect, init_db
    from yearn_data.fees import index_canonical_fees
    from yearn_data.fee_recompute import recompute_canonical_fees
    from yearn_data import fees, ybold_fees
    c=CASES[-1];r,e=c['report'],c['evidence']
    calls=[]
    # Exercise pinned collection with mocked code hash, independently tested against the fixture.
    def call(tx, block_identifier):
        calls.append(block_identifier)
        address=ACCOUNTANT if tx['data']==keccak(text='accountant()')[:4] else STAKER
        return bytes.fromhex('00'*12+address[2:])
    client=SimpleNamespace(eth=SimpleNamespace(call=call,get_code=lambda *a,**k:b'code'))
    real_keccak=ybold_fees.keccak
    monkeypatch.setattr(ybold_fees,'keccak',lambda value=None,**kw: bytes.fromhex(ybold_fees.CODE_HASH[2:]) if value==b'code' else real_keccak(value,**kw))
    monkeypatch.setattr(fees,'web3_for',lambda chain:client)
    with connect(tmp_path/'test.sqlite') as conn:
        init_db(conn)
        conn.execute('INSERT INTO vaults(chain_id,address,version,updated_at) VALUES (1,?,?,1)',(r['vault_address'],'v3'))
        fields=[x['name'] for x in conn.execute('PRAGMA table_info(strategy_reports)') if x['name'] in r]
        conn.execute('INSERT INTO strategy_reports ('+','.join(fields)+') VALUES ('+','.join('?' for _ in fields)+')',[r[k] for k in fields])
        assert index_canonical_fees(conn,receipt_fetcher=lambda *a:e['receipt'])==1
        assert calls==[e['block_hash'],e['block_hash']]
        before=conn.execute('SELECT accounting_json FROM canonical_fee_reports').fetchone()[0]
        assert json.loads(before)['staker_distribution_raw']==r['total_fees_raw']
        assert index_canonical_fees(conn)==0
        conn.execute("UPDATE canonical_fee_reports SET method_version='canonical-fees-7'")
        assert index_canonical_fees(conn)==1
        assert calls==[e['block_hash'],e['block_hash']]  # Reuse retained evidence offline.
        assert recompute_canonical_fees(conn,'formulas')['accepted']==1
        assert conn.execute('SELECT accounting_json FROM canonical_fee_reports').fetchone()[0]==before

def test_valuation_and_api_keep_distributions_separate():
    from yearn_data.fee_valuation import value_event, target_for
    from yearn_data.pairing import _fees
    r=CASES[-1]['report']
    event={k:r[k] for k in ('chain_id','vault_address','tx_hash','log_index','block_timestamp','asset','asset_decimals')}
    event.update(accounting=accounting(r,CASES[-1]['evidence']),accounting_status='accepted')
    row=value_event(event,{target_for(event):{'status':'ok','price_usd':'1','evidence_json':'{}','fetched_at':1}})
    api=_fees([row])
    assert api['totalFeesPaidUsd']=='0'
    assert api['stakerDistributionUsd']==api['reportedTotalFeesUsd']
    assert api['stakerDistributionUsd']!='0'
    assert row['gross_gain_raw']==r['gain_raw']
