import copy
import gzip
import json
import os
from pathlib import Path
import sys

import pytest
from test_canonical_fees import db, seed
from yearn_data.fee_reconstruction import extract_fee_shares
from yearn_data.fee_trace import verify_receipt_mint, TRACE_VERSION
from yearn_data.fees import index_canonical_fees
from yearn_data.fee_recompute import recompute_canonical_fees
sys.path.insert(0, str(Path(__file__).parents[1]/'scripts'))
from replay_fantom_prestate import charged_gas, compact_trace, correct_sender_prestate, creation_frames, replay

CASES = [json.load(gzip.open(p, 'rt')) for p in sorted((Path(__file__).parent/'fixtures/fee-fantom-replay').glob('*.gz'))]


def test_opera_gas_rule_and_unknown_refund_fail_closed():
    assert charged_gas(1453636,120144,1197292,'istanbul')==1209306
    with pytest.raises(ValueError,match='capped'):
        charged_gas(100000,20000,40000,'istanbul')


def test_observed_capped_refund_must_match_local_receipt():
    assert charged_gas(5000000,4480637,259682,'istanbul',364200)==603226
    assert charged_gas(3440994,1770631,1336291,'london',340500)==1506926
    with pytest.raises(ValueError,match='differs'):
        charged_gas(5000000,4480637,259682,'istanbul',100)


def test_constructor_frames_bind_storage_to_returned_address():
    parent='0x'+'11'*20; child='0x'+'22'*20
    def step(op,depth,stack):return dict(op=op,depth=depth,stack=stack,pc=0)
    steps=[step('CREATE',1,['0','0','0']),step('SLOAD',2,['0']),
           step('STOP',2,[]),step('STOP',1,[child])]
    assert creation_frames(steps)=={1:child}
    assert compact_trace(steps,parent,[parent],{}, {'logs':[]})[parent]['ops']==[]
    bad=copy.deepcopy(steps);bad[-1]['stack']=['0']
    with pytest.raises(ValueError,match='failed'):
        creation_frames(bad)
    with pytest.raises(ValueError,match='prestate is not empty'):
        compact_trace(steps,parent,[parent],{child:{'nonce':1}}, {'logs':[]})
    with pytest.raises(ValueError,match='missing accessed storage'):
        compact_trace([step('SLOAD',1,['0'])],parent,[parent],{}, {'logs':[]})


@pytest.mark.parametrize('op',['BLOCKHASH','CREATE2','SELFDESTRUCT'])
def test_unimplemented_replay_paths_remain_rejected(op):
    with pytest.raises(ValueError,match='unsupported replay opcode'):
        compact_trace([dict(op=op,depth=1,stack=[],pc=0)],'0x01',[],{}, {'logs':[]})


def test_sender_correction_requires_bound_parent_and_untouched_account():
    case=next(c for c in CASES if 'sender_prestate_proof' in c)
    prestate=case['prestate'];tx=case['tx'];block=case['block'];proof=case['sender_prestate_proof']
    corrected,evidence=correct_sender_prestate(prestate,tx,block,proof)
    assert corrected[tx['from']]['balance']!=prestate[tx['from']]['balance']
    assert evidence['prior_transactions']==3
    invalid=copy.deepcopy(proof);invalid['balance']['request']['params'][1]['blockHash']='0xwrong'
    with pytest.raises(ValueError,match='binding'):
        correct_sender_prestate(prestate,tx,block,invalid)
    invalid=copy.deepcopy(proof);invalid['traces']['response']['result'][0]['result']['calls'][0]['to']=tx['from']
    with pytest.raises(ValueError,match='sender touched'):
        correct_sender_prestate(prestate,tx,block,invalid)
    invalid=copy.deepcopy(proof);invalid['nonce']['response']['result']='0x0'
    with pytest.raises(ValueError,match='untouched EOA'):
        correct_sender_prestate(prestate,tx,block,invalid)


@pytest.mark.parametrize('case',CASES,ids=lambda c:str(c['event']['id']))
def test_replay_provenance_canonical_recompute_and_rebuild(db,case):
    seed(db,[case]);e=case['event'];key=(250,e['transaction_hash'],e['log_index'])
    db.execute('INSERT INTO fee_receipt_evidence VALUES (?,?,?,?,?)',(250,key[1],e['block_number'],e['block_hash'],json.dumps(case['receipt'])))
    db.execute('INSERT INTO fee_execution_traces VALUES (?,?,?,?,?,?)',(250,key[1],e['vault_address'],e['block_hash'],TRACE_VERSION,json.dumps(case['trace'])))
    def forbidden(*args):raise AssertionError('offline replay attempted RPC')
    opts=dict(report_keys=[key],reconstruct_v2=True,verify_selective=True,verify_execution=True,
              receipt_fetcher=forbidden,state_reader=forbidden,block_logs_fetcher=forbidden,trace_fetcher=forbidden)
    def value():return db.execute('SELECT accounting_json FROM canonical_fee_reports').fetchone()[0]
    index_canonical_fees(db,**opts);before=value()
    assert db.execute('SELECT status FROM canonical_fee_reports').fetchone()[0] == 'ok'
    # The captured service estimate for 160913 differs by three wei from the
    # assessed fee operand in its verified execution trace.
    expected = '4422615320863889915' if e['id'] == 160913 else e['total_fees_paid_raw']
    assert json.loads(before)['total_fees_paid_raw'] == expected
    evidence=json.loads(db.execute('SELECT evidence_json FROM canonical_fee_reports').fetchone()[0])
    assert 'local-prestate-replay' in json.dumps(evidence)
    for mode in ('policy','formulas'):
        assert recompute_canonical_fees(db,mode,report_keys=[key])['accepted'] == 1
        assert value()==before
    db.execute('DELETE FROM canonical_fee_reports');index_canonical_fees(db,**opts);assert value()==before
    shares=extract_fee_shares(case['receipt']['logs'],e['vault_address'],e['log_index'])
    trace=copy.deepcopy(case['trace']);trace['local_replay']['opera_gas_used_equal']=False
    with pytest.raises(ValueError,match='unverified'):
        verify_receipt_mint(trace,case['receipt'],e['vault_address'],shares['mint_log_index'],shares['minted_shares_raw'])


@pytest.mark.skipif(not os.environ.get('RUN_LOCAL_EVM_REPLAY'),reason='explicit localhost Anvil integration check')
@pytest.mark.parametrize('case',CASES,ids=lambda c:str(c['event']['id']))
def test_actual_local_replay_from_frozen_native_prestate(case):
    e=case['event'];v=e['vault_address'];trace=replay(case['prestate'],case['tx'],case['block'],case['receipt'],[v],case['trace']['local_replay']['fork'],case.get('sender_prestate_proof'))[v]
    assert trace['ops']==case['trace']['ops']
