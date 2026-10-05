"""Frozen observed supplement; distinct from the original source-parity corpus."""
import json
from pathlib import Path

import pytest

from yearn_data.fee_acceptance import assess_candidate, normalize_candidate
from yearn_data.fee_reconstruction import reconstruct_candidate
from yearn_data.fee_trace import extract_mint_operand

CASES=json.loads((Path(__file__).parent/'fixtures/fee-timing/wider-sample.json').read_text())['cases']


@pytest.mark.parametrize('case',CASES,ids=lambda c:str(c['source_reconstruction_id']))
def test_wider_observed_inputs_reproduce_candidates_and_decisions(case):
    evidence=case['evidence'];expected=case['candidate']
    baseline=normalize_candidate(reconstruct_candidate(evidence['fee_shares'],evidence['historical_state']))
    assert baseline==expected.get('baseline_candidate',expected)
    if 'mint_trace_window' in case:
        verified=extract_mint_operand(case['mint_trace_window'],evidence['fee_shares']['minted_shares_raw'])
        assert verified['fee_operand_raw']==expected['total_fees_paid_raw']
        assert verified['fee_operand_raw']==evidence['execution_verification']['fee_operand_raw']
    assert assess_candidate(expected,evidence)==case['decision']
