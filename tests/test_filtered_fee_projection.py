"""Direct filtered evidence rebuilds offline and fails closed when its binding changes."""
import json

import pytest
from test_canonical_fees import CASES, RECEIPTS, seed, db
from yearn_data.fees import FEE_TOPIC, REPORT_TOPICS, index_canonical_fees
from yearn_data.fee_log_evidence import digest, retain
from yearn_data.fee_recompute import recompute_canonical_fees


def prepare(conn):
    case=next(c for c in CASES if c['event']['event_name']=='FeeReport')
    seed(conn,[case]);r=conn.execute('SELECT r.*,v.api_version FROM strategy_reports r JOIN vaults v '
        'ON v.chain_id=r.chain_id AND v.address=r.vault_address').fetchone()
    block=r['block_number'];topics=[FEE_TOPIC,*REPORT_TOPICS]
    scope={'chain_id':r['chain_id'],'addresses':[r['vault_address']], 'topics':topics,
           'from_block':block,'to_block':block}
    manifest={'chain_id':r['chain_id'],'sources':[scope]};key=digest(manifest)
    conn.execute('INSERT INTO fee_log_acquisitions VALUES (?,?)',(key,json.dumps(manifest)))
    receipt=RECEIPTS[r['chain_id'],r['tx_hash']]
    logs=[dict(n) for n in receipt['logs'] if n['address'].lower()==r['vault_address'] and n['topics'][0] in topics]
    for n in logs:
        n.update(transactionHash=r['tx_hash'],blockNumber=hex(block),blockHash=receipt['blockHash'])
    retain(conn,r,logs,{'chain_id':r['chain_id'],'vault':r['vault_address'],'topics':topics,
        'from_block':block,'to_block':block,'acquisition_id':key})


def no_rpc(*args):
    pytest.fail('unexpected receipt fetch')


def test_offline_projection_and_rebuild(db):
    prepare(db)
    assert index_canonical_fees(db,filtered_evidence_only=True,receipt_fetcher=no_rpc)==1
    original=db.execute('SELECT accounting_json FROM canonical_fee_reports').fetchone()[0]
    assert db.execute('SELECT count(*) FROM fee_receipt_evidence').fetchone()[0]==0
    for mode in ('policy','formulas'):
        assert recompute_canonical_fees(db,mode)['accepted']==1
        assert db.execute('SELECT accounting_json FROM canonical_fee_reports').fetchone()[0]==original
    db.execute('DELETE FROM canonical_fee_reports')
    assert index_canonical_fees(db,filtered_evidence_only=True,receipt_fetcher=no_rpc)==1
    assert db.execute('SELECT accounting_json FROM canonical_fee_reports').fetchone()[0]==original


@pytest.mark.parametrize('mutation',[
    "UPDATE strategy_reports SET asset_decimals=7",
    "UPDATE strategy_reports SET gain_raw='1'",
    "UPDATE fee_filtered_log_evidence SET payload_sha256='wrong'",
    'DELETE FROM fee_log_acquisitions',
])
def test_invalid_evidence_never_triggers_rpc_or_acceptance(db,mutation):
    prepare(db)
    index_canonical_fees(db,filtered_evidence_only=True,receipt_fetcher=no_rpc)
    db.execute(mutation)
    assert recompute_canonical_fees(db,'formulas')['unavailable']==1
    db.execute('DELETE FROM canonical_fee_reports')
    index_canonical_fees(db,filtered_evidence_only=True,receipt_fetcher=no_rpc)
    row=db.execute('SELECT status,reason,accounting_json FROM canonical_fee_reports').fetchone()
    assert tuple(row)==('unresolved','invalid_filtered_log_evidence',None)


@pytest.mark.parametrize('damage',['missing_topic','missing_fee','wrong_transaction','wrong_block'])
def test_structurally_invalid_filtered_logs_are_rejected(db,damage):
    from yearn_data.fee_log_evidence import evaluate
    prepare(db)
    r=db.execute('SELECT r.*,v.api_version FROM strategy_reports r JOIN vaults v '
        'ON v.chain_id=r.chain_id AND v.address=r.vault_address').fetchone()
    payload=json.loads(db.execute('SELECT payload_json FROM fee_filtered_log_evidence').fetchone()[0])
    if damage=='missing_topic':
        payload['coverage']['topics']=[FEE_TOPIC]
    elif damage=='missing_fee':
        payload['logs']=[n for n in payload['logs'] if n['topics'][0]!=FEE_TOPIC]
    elif damage=='wrong_transaction':
        payload['logs'][0]['transactionHash']='0x'+'00'*32
    else:
        payload['logs'][0]['blockNumber']='0x1'
    with pytest.raises(ValueError):
        evaluate(r,payload)


@pytest.mark.parametrize('version,gain,accepted', [('0.4.6',0,True),('0.4.5',0,True),('0.3.5',0,True),('0.3.3',0,False),('unknown',0,False),('0.4.6',1,False)])
def test_zero_gain_rule_requires_supported_release_and_matching_report(db,version,gain,accepted):
    from yearn_data.fee_log_evidence import binding, evaluate
    prepare(db)
    r=dict(db.execute('SELECT r.*,v.api_version FROM strategy_reports r JOIN vaults v '
        'ON v.chain_id=r.chain_id AND v.address=r.vault_address').fetchone())
    payload=json.loads(db.execute('SELECT payload_json FROM fee_filtered_log_evidence').fetchone()[0])
    r.update(api_version=version,gain_raw=str(gain))
    payload['report_inputs']=binding(r)
    payload['logs']=[n for n in payload['logs'] if n['topics'][0]!=FEE_TOPIC]
    for n in payload['logs']:
        if int(n['logIndex'],16)==r['log_index']:
            n['data']='0x'+format(gain,'064x')+n['data'][66:]
    if accepted:
        amounts,index=evaluate(r,payload)
        assert amounts['total_fees_paid_raw']=='0' and amounts['amount_source']=='contract-zero-gain'
        assert index is None
    else:
        with pytest.raises(ValueError): evaluate(r,payload)
