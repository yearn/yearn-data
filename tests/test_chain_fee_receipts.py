"""Cross-chain receipts with positive mints must not become zero or accepted fees."""
import hashlib,json
from pathlib import Path
import pytest
from test_canonical_fees import seed,db
from yearn_data.fees import index_canonical_fees
from yearn_data.fee_reconstruction import extract_fee_shares

ROOT=Path(__file__).parent/'fixtures/fee-chain-receipts'
MANIFEST=json.loads((ROOT/'manifest.json').read_text())


@pytest.mark.parametrize('name',MANIFEST['files'])
def test_receipt_without_execution_stays_unresolved(db,name):
    payload=(ROOT/name).read_bytes();assert hashlib.sha256(payload).hexdigest()==MANIFEST['files'][name]
    case=json.loads(payload);event=case['event'];seed(db,[case]);requests=[]
    assert extract_fee_shares(case['receipt']['logs'],event['vault_address'],event['log_index'])['status']=='found'
    def unavailable(*args):requests.append(args);raise ValueError('retained provider failure')
    def forbidden(*args):pytest.fail('unexpected state or block acquisition')
    index_canonical_fees(db,report_keys=[(event['chain_id'],event['transaction_hash'],event['log_index'])],
        reconstruct_v2=True,verify_selective=True,verify_execution=True,trace_limit=1,
        receipt_fetcher=lambda *args:case['receipt'],trace_fetcher=unavailable,state_reader=forbidden,block_logs_fetcher=forbidden)
    row=db.execute('SELECT * FROM canonical_fee_reports').fetchone()
    assert row['status']=='unresolved' and row['accounting_json'] is None
    assert row['reason']=='trace_unavailable_or_unrecognized'
    assert len(requests)==1
    assert db.execute('SELECT count(*) FROM fee_receipt_evidence').fetchone()[0]==1
    assert db.execute('SELECT count(*) FROM fee_execution_traces').fetchone()[0]==0
