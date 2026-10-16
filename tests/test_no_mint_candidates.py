"""Real positive-gain no-mint receipts and zero-share mint counterexamples."""
import hashlib
import json
from pathlib import Path
import pytest
from test_canonical_fees import seed,db
from yearn_data.fees import index_canonical_fees

ROOT=Path(__file__).parent/'fixtures/fee-no-mint'
MANIFEST=json.loads((ROOT/'manifest.json').read_text())
CASES=[json.loads((ROOT/name).read_text()) for name in MANIFEST['files']]


@pytest.mark.parametrize('case',CASES,ids=lambda c:str(c['event']['id']))
def test_real_no_mint_acceptance_and_rounding_guard(db,case):
    assert all(hashlib.sha256((ROOT/n).read_bytes()).hexdigest()==sha for n,sha in MANIFEST['files'].items())
    seed(db,[case])
    def no_state(*args):pytest.fail('receipt-only case requested historical state')
    index_canonical_fees(db,reconstruct_v2=True,receipt_fetcher=lambda *a:case['receipt'],state_reader=no_state)
    r=db.execute('SELECT * FROM canonical_fee_reports').fetchone()
    if case['expected_share_status']=='no-mint':
        assert r['status']=='ok' and json.loads(r['accounting_json'])['total_fees_paid_raw']=='0'
        assert json.loads(r['candidate_json'])['method']=='receipt-no-mint-zero'
    else:
        assert r['status']=='unresolved' and r['accounting_json'] is None
        assert json.loads(r['evidence_json'])['fee_shares']['status']=='zero-share-mint'
