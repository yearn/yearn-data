"""Real transaction regressions for the remaining bounded execution cases."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from yearn_data.fee_trace import verify_receipt_mint
from yearn_data.fee_reconstruction import extract_fee_shares


def verify_case(case):
    event, receipt = case['event'], case['receipt']
    shares = extract_fee_shares(receipt['logs'], event['vault_address'], event['log_index'])
    return verify_receipt_mint(case['trace'], receipt, event['vault_address'],
                              shares['mint_log_index'], shares['minted_shares_raw'])


ROOT = Path(__file__).parent / 'fixtures/fee-unresolved-execution'
CASES = sorted(p for folder in (ROOT, ROOT.parent / 'fee-cohort-pilots')
               for p in folder.glob('*.json') if p.name != 'manifest.json')


@pytest.mark.parametrize('path', CASES, ids=lambda p: p.stem)
def test_real_operand_and_receipt_binding(path):
    case = json.loads(path.read_text())
    result = verify_case(case)
    assert result['fee_operand_raw'] == case['expected_operand_raw']
    damaged = deepcopy(case)
    damaged['trace']['overflow'] = True
    with pytest.raises(ValueError, match='incomplete'):
        verify_case(damaged)


@pytest.mark.parametrize('event_id', [92520, 95226])
def test_zero_share_operand_is_positive_and_damaged_arithmetic_rejected(event_id):
    case = json.loads((ROOT / f'{event_id}.json').read_text())
    result = verify_case(case)
    assert result['minted_shares_raw'] == '0'
    assert result['fee_operand_raw'] == '1'
    for op in case['trace']['ops']:
        if op['op'] == 'MUL' and op['pc'] == result['mul_pc']:
            op['a'] = '2'
    with pytest.raises(ValueError, match='inconsistent'):
        verify_case(case)

from test_canonical_fees import seed, db
from yearn_data.fees import index_canonical_fees
from yearn_data.fee_recompute import recompute_canonical_fees


def forbidden(*args):
    pytest.fail('unexpected acquisition')


@pytest.mark.parametrize('path', CASES, ids=lambda p: p.stem)
def test_explicit_execution_projection_and_rebuild(db, path):
    case = json.loads(path.read_text());event = case['event'];seed(db, [case])
    keys = [(event['chain_id'], event['transaction_hash'], event['log_index'])]
    options = dict(report_keys=keys, reconstruct_v2=True, verify_selective=True,
                   verify_execution=True, trace_limit=1, state_reader=forbidden, block_logs_fetcher=forbidden)
    index_canonical_fees(db, receipt_fetcher=lambda *a: case['receipt'], trace_fetcher=lambda *a: case['trace'], **options)
    row = db.execute('SELECT * FROM canonical_fee_reports').fetchone()
    assert row['status'] == 'ok'
    assert json.loads(row['accounting_json'])['total_fees_paid_raw'] == case['expected_operand_raw']
    evidence = json.loads(row['evidence_json'])
    assert evidence['fee_amount_basis'] == 'assessed_asset_fee_operand'
    if event['id'] in (92520, 95226):
        assert evidence['issued_fee_shares_raw'] == '0'
    for mode in ('policy', 'formulas'):
        assert recompute_canonical_fees(db, mode)['accepted'] == 1
    db.execute('DELETE FROM canonical_fee_reports')
    index_canonical_fees(db, receipt_fetcher=forbidden, trace_fetcher=forbidden, **options)
    assert db.execute('SELECT accounting_json FROM canonical_fee_reports').fetchone()[0] == row['accounting_json']
    db.execute('DELETE FROM fee_execution_traces')
    assert recompute_canonical_fees(db, 'formulas')['unavailable'] == 1


def test_explicit_execution_requires_bounded_selection(db):
    with pytest.raises(ValueError, match='report keys'):
        index_canonical_fees(db, reconstruct_v2=True, verify_selective=True, verify_execution=True)
