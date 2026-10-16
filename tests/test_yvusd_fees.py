import copy
import json
from pathlib import Path

import pytest
from yearn_data.yvusd_fees import accounting, FEE_TOPIC
from yearn_data.fees import normalize_allocator_fees
from yearn_data.fee_valuation import usd_amount

CASES = json.loads((Path(__file__).parent/'fixtures/fees/yvusd.json').read_text())

@pytest.mark.parametrize('case,expected', list(zip(CASES, [(78666,624907,624907),(0,0,7462307),(0,0,25035988)])))
def test_historical_fee_splits(case, expected):
    result = accounting(case['report'], case['evidence'])
    management, performance, bonus = expected
    assert result['management_fee_raw'] == str(management)
    assert result['performance_fee_raw'] == str(performance)
    assert result['locker_bonus_raw'] == str(bonus)
    assert result['total_fees_paid_raw'] == str(management + performance)
    assert result['manager_fee_raw'] == str(management + performance)
    assert result['reported_total_fees_raw'] == str(sum(expected))
    assert usd_amount(result['locker_bonus_raw'], 6, '1') == str(bonus / 1000000)

@pytest.mark.parametrize('mutation', ['accountant','transaction','amount','missing','duplicate','protocol','prior_report'])
def test_rejects_unbound_or_unreconciled_splits(mutation):
    case = copy.deepcopy(CASES[-1]); r,e = case['report'],case['evidence']; receipt=e['receipt']
    fee=next(l for l in receipt['logs'] if l['topics'][0]==FEE_TOPIC)
    if mutation=='accountant': e['accountant']='0x'+'1'*40
    if mutation=='transaction': receipt['transactionHash']='0x'+'1'*64
    if mutation=='amount': fee['topics'][3]='0x'+format(1,'064x')
    if mutation=='missing': receipt['logs'].remove(fee)
    if mutation=='duplicate': receipt['logs'].append(copy.deepcopy(fee))
    if mutation=='protocol': r['protocol_fees_raw']='1'
    if mutation=='prior_report':
        previous=copy.deepcopy(next(l for l in receipt['logs'] if l['logIndex']==r['log_index']))
        previous['logIndex']=r['log_index']-1; receipt['logs'].append(previous)
    with pytest.raises(ValueError): accounting(r,e)

def test_target_cannot_silently_fall_back_to_raw_deductions():
    with pytest.raises(ValueError): normalize_allocator_fees(CASES[0]['report'])

def test_index_and_offline_recompute_preserve_split(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from yearn_data.storage import connect, init_db
    from yearn_data.fees import index_canonical_fees
    from yearn_data.fee_recompute import recompute_canonical_fees
    from yearn_data import fees
    case = CASES[0]; r = case['report']; e = case['evidence']
    with connect(tmp_path/'fees.sqlite') as conn:
        init_db(conn)
        conn.execute('INSERT INTO vaults(chain_id,address,version,api_version,updated_at) VALUES (1,?,?,?,1)',
                     (r['vault_address'], 'v3', r['api_version']))
        fields = [row['name'] for row in conn.execute('PRAGMA table_info(strategy_reports)') if row['name'] in r]
        conn.execute('INSERT INTO strategy_reports ('+','.join(fields)+') VALUES ('+','.join('?' for _ in fields)+')',
                     [r[field] for field in fields])
        calls=[]
        def call(transaction, block_identifier):
            calls.append(block_identifier)
            return bytes.fromhex('00'*12+e['accountant'][2:])
        monkeypatch.setattr(fees, 'web3_for', lambda chain: SimpleNamespace(eth=SimpleNamespace(call=call)))
        assert index_canonical_fees(conn,receipt_fetcher=lambda chain,tx:e['receipt']) == 1
        assert calls == [e['receipt']['blockHash']]
        assert index_canonical_fees(conn) == 0
        before=conn.execute('SELECT accounting_json FROM canonical_fee_reports').fetchone()[0]
        assert json.loads(before)['total_fees_paid_raw']=='703573'
        assert recompute_canonical_fees(conn,'formulas')['accepted']==1
        assert conn.execute('SELECT accounting_json FROM canonical_fee_reports').fetchone()[0]==before

def test_zero_deduction_without_fee_event_is_known_zero():
    from eth_abi import encode
    case=copy.deepcopy(CASES[-1]); r,e=case['report'],case['evidence']
    r['total_fees_raw']='0'
    e['receipt']['logs']=[l for l in e['receipt']['logs'] if l['topics'][0]!=FEE_TOPIC]
    log=next(l for l in e['receipt']['logs'] if l['logIndex']==r['log_index'])
    from eth_abi import decode
    values=list(decode(['uint256']*6,bytes.fromhex(log['data'][2:])))
    values[4]=0;log['data']='0x'+encode(['uint256']*6,values).hex()
    result=accounting(r,e)
    assert result['total_fees_paid_raw']==result['locker_bonus_raw']=='0'


def test_multiple_reports_each_consume_their_own_fee_event():
    case=copy.deepcopy(CASES[-1]); r,e=case['report'],case['evidence']
    fee=copy.deepcopy(next(l for l in e['receipt']['logs'] if l['topics'][0]==FEE_TOPIC))
    report=copy.deepcopy(next(l for l in e['receipt']['logs'] if l['logIndex']==r['log_index']))
    fee['logIndex']-=10;report['logIndex']-=10
    e['receipt']['logs'].extend([fee,report])
    assert accounting(r,e)['locker_bonus_raw']=='25035988'
