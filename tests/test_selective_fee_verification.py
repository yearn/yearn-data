import copy
import json
from pathlib import Path

import pytest

from yearn_data.fee_acceptance import assess_candidate
from yearn_data.fee_trace import SelectiveFeeVerifier, verify_receipt_mint
from yearn_data.storage import connect, init_db

CASE=json.loads((Path(__file__).parent/'fixtures/fee-timing/mint-trace-windows.json').read_text())[1]
VAULT=CASE['vault']; BLOCK=CASE['block_hash']; TX=CASE['transaction_hash']


def evidence():
    trace={'ops':copy.deepcopy(CASE['ops'])}
    # Synthetic complete vault log sequence to test binding and selection mechanics.
    reportlog={'op':'LOG2','topic':'12','data':'0x'+'00'*32,'depth':3,'pc':9999}
    trace['ops'].append(reportlog)
    logs=[{'address':VAULT,'blockHash':BLOCK,'logIndex':6,'topics':['0x'+trace['ops'][5]['topic'],'0x'+'00'*32,'0x'+'00'*12+VAULT[2:]],'data':trace['ops'][5]['data']},
          {'address':VAULT,'blockHash':BLOCK,'logIndex':10,'topics':['0x12','0x'+'00'*32],'data':reportlog['data']}]
    return trace,{'logs':logs,'blockHash':BLOCK},logs


@pytest.fixture
def db(tmp_path):
    c=connect(tmp_path/'test.sqlite');init_db(c)
    yield c
    c.close()


def test_selective_budget_cache_and_unselected_reports(db):
    trace,receipt,logs=evidence()
    scans=[];requests=[]
    def scan(*args):
        scans.append(args)
        return logs
    def fetch(*args):
        requests.append(args)
        return trace
    report={'chain_id':1,'vault_address':VAULT,'tx_hash':TX,'api_version':'0.3.0','log_index':7}
    shares={'mint_log_index':6,'minted_shares_raw':CASE['minted_shares_raw']}
    verifier=SelectiveFeeVerifier(db,1,None,scan,fetch)
    assert verifier.verify({**report,'log_index':10},receipt,shares)['status']=='not_selected'
    result=verifier.verify(report,receipt,shares)
    assert result['status']=='verified'
    assert len(scans)==1 and len(requests)==1
    assert verifier.verify({**report,'tx_hash':'another-tx'},receipt,shares)['reason']=='trace_budget_exhausted'
    # A new run reuses persisted evidence without another trace request.
    again=SelectiveFeeVerifier(db,1,None,scan,fetch)
    assert again.verify(report,receipt,shares)['status']=='verified'
    assert len(requests)==1 and again.attempts==0
    assert len(scans)==1  # Successful block log evidence also survives a new verifier.
    candidate={'method':'execution-fee-mint','total_fees_paid_raw':result['fee_operand_raw']}
    assert assess_candidate(candidate,{'execution_verification':result,'later_vault_report_in_receipt':True})['status']=='accepted'


def test_unsupported_release_and_failed_trace_are_bounded(db):
    trace,receipt,logs=evidence();requests=[]
    def fail(*args):
        requests.append(args)
        raise RuntimeError('https://secret.example/key')
    verifier=SelectiveFeeVerifier(db,1,None,lambda *args:logs,fail)
    report={'chain_id':1,'vault_address':VAULT,'tx_hash':TX,'api_version':'0.4.3','log_index':7}
    shares={'mint_log_index':6,'minted_shares_raw':CASE['minted_shares_raw']}
    assert verifier.verify(report,receipt,shares)['reason']=='unsupported_trace_release'
    report['api_version']='0.3.0'
    for _ in range(2):
        result=verifier.verify(report,receipt,shares)
        assert result['status']=='unavailable' and 'secret' not in json.dumps(result)
    assert len(requests)==1
    assert db.execute('SELECT count(*) FROM fee_execution_traces').fetchone()[0]==0


def test_trace_must_match_complete_receipt_and_target_mint():
    trace,receipt,_=evidence()
    assert verify_receipt_mint(trace,receipt,VAULT,6,CASE['minted_shares_raw'])['fee_operand_raw']==CASE['expected_fee_operand_raw']
    for bad in ({**trace,'overflow':True},{**trace,'error':'reverted'},{'ops':trace['ops'][:-1]}):
        with pytest.raises(ValueError):verify_receipt_mint(bad,receipt,VAULT,6,CASE['minted_shares_raw'])
    with pytest.raises(ValueError):verify_receipt_mint(trace,receipt,VAULT,10,CASE['minted_shares_raw'])


def test_verifier_enforces_budget_and_flag_requirements(db,tmp_path):
    from yearn_data.fees import index_canonical_fees
    from yearn_data.cli import main
    with pytest.raises(ValueError,match='requires V2 reconstruction'):
        index_canonical_fees(db,verify_selective=True)
    with pytest.raises(ValueError,match='positive'):
        index_canonical_fees(db,verify_selective=True,reconstruct_v2=True,trace_limit=0)
    with pytest.raises(ValueError,match='requires --verify-selective'):
        main(['--db',str(tmp_path/'cli.sqlite'),'index-fees','--canonical','--trace-limit','1'])


def test_invalid_cached_trace_is_refetched_within_budget(db):
    trace,receipt,logs=evidence()
    report={'chain_id':1,'vault_address':VAULT,'tx_hash':TX,'api_version':'0.3.0','log_index':7}
    shares={'mint_log_index':6,'minted_shares_raw':CASE['minted_shares_raw']}
    requests=[]
    def fetch(*args):
        requests.append(args)
        return trace
    assert SelectiveFeeVerifier(db,1,None,lambda *a:logs,fetch).verify(report,receipt,shares)['status']=='verified'
    db.execute("UPDATE fee_execution_traces SET trace_json='{}'")
    assert SelectiveFeeVerifier(db,1,None,lambda *a:logs,fetch).verify(report,receipt,shares)['status']=='verified'
    assert len(requests)==2


def test_repeated_mint_amounts_bind_to_distinct_execution_windows():
    # Synthetic second report: same number of shares, different fee/assets.
    first,receipt,logs=evidence()
    second=copy.deepcopy(first)
    ops=second['ops'];mint=int(CASE['minted_shares_raw'])
    fee=int(ops[0]['a'],16)*2;supply=int(ops[0]['b'],16)+mint
    product=fee*supply;assets=product//mint
    assert product//assets==mint
    ops[0].update(a=hex(fee)[2:],b=hex(supply)[2:])
    ops[1].update(a=hex(product)[2:],b=hex(fee)[2:])
    ops[2].update(a=hex(product)[2:],b=hex(assets)[2:])
    ops[3]['b']=hex(supply+mint)[2:]
    second_logs=copy.deepcopy(logs)
    for log in second_logs:log['logIndex']+=10
    combined={'ops':first['ops']+second['ops']}
    combined_receipt={**receipt,'logs':logs+second_logs}
    assert verify_receipt_mint(combined,combined_receipt,VAULT,6,mint)['fee_operand_raw']==CASE['expected_fee_operand_raw']
    assert verify_receipt_mint(combined,combined_receipt,VAULT,16,mint)['fee_operand_raw']==str(fee)
