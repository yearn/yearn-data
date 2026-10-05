"""Real zero-gain execution operands, scoped acquisition and offline evidence replay."""
import hashlib
import json
from pathlib import Path

import pytest
from test_canonical_fees import seed,db
from yearn_data.fees import index_canonical_fees
from yearn_data.fee_recompute import recompute_canonical_fees

ROOT=Path(__file__).parent/'fixtures/fee-zero-execution'
MANIFEST=json.loads((ROOT/'manifest.json').read_text())
CASES=[json.loads((ROOT/name).read_text()) for name in MANIFEST['files']]


def forbidden(*args):pytest.fail('unexpected state, receipt, block-log or trace acquisition')


@pytest.mark.parametrize('case',CASES,ids=lambda c:c['event']['api_version'])
def test_real_zero_gain_execution_and_offline_rebuild(db,case):
    assert all(hashlib.sha256((ROOT/n).read_bytes()).hexdigest()==sha for n,sha in MANIFEST['files'].items())
    seed(db,[case]);calls=[]
    def trace(*args):calls.append(args);return case['trace']
    options=dict(reconstruct_v2=True,verify_selective=True,trace_limit=1,state_reader=forbidden,block_logs_fetcher=forbidden)
    index_canonical_fees(db,receipt_fetcher=lambda *a:case['receipt'],trace_fetcher=trace,**options)
    row=db.execute('SELECT * FROM canonical_fee_reports').fetchone()
    assert row['status']=='ok'
    assert json.loads(row['accounting_json'])['total_fees_paid_raw']==case['expected']['fee_operand_raw']
    assert len(calls)==1 and 'historical_state' not in json.loads(row['evidence_json'])
    before=row['accounting_json']
    for mode in ('policy','formulas'):assert recompute_canonical_fees(db,mode)['accepted']==1
    db.execute('DELETE FROM canonical_fee_reports')
    index_canonical_fees(db,receipt_fetcher=forbidden,trace_fetcher=forbidden,**options)
    assert db.execute('SELECT accounting_json FROM canonical_fee_reports').fetchone()[0]==before


@pytest.mark.parametrize('mutation',[
    'DELETE FROM fee_execution_traces','DELETE FROM fee_receipt_evidence',
    "UPDATE strategy_reports SET asset_decimals=7",
    "UPDATE fee_execution_traces SET trace_json='{\"ops\":[]}'",
])
def test_execution_recomputation_rejects_missing_changed_evidence(db,mutation):
    case=CASES[0];seed(db,[case])
    index_canonical_fees(db,reconstruct_v2=True,verify_selective=True,trace_limit=1,
        receipt_fetcher=lambda *a:case['receipt'],trace_fetcher=lambda *a:case['trace'],
        state_reader=forbidden,block_logs_fetcher=forbidden)
    db.execute(mutation)
    assert recompute_canonical_fees(db,'formulas')['unavailable']==1
    assert db.execute('SELECT accounting_json FROM canonical_fee_reports').fetchone()[0] is None
