import copy
import json
from pathlib import Path

import pytest

ROOT=Path(__file__).parents[1]
from yearn_data.fee_trace import extract_mint_operand as extract
CASES=json.loads((ROOT/'tests/fixtures/fee-timing/mint-trace-windows.json').read_text())


@pytest.mark.parametrize('case',CASES,ids=lambda c:str(c['id']))
def test_observed_trace_fee_operand_reproduces_receipt_mint(case):
    result=extract(case['ops'],case['minted_shares_raw'])
    assert result['fee_operand_raw']==case['expected_fee_operand_raw']
    assert int(result['fee_operand_raw'])*int(result['supply_before_mint_raw'])//int(result['valuation_assets_at_mint_raw'])==int(case['minted_shares_raw'])


def test_trace_does_not_accept_mismatched_supply_or_ambiguous_mints():
    case=CASES[0];ops=copy.deepcopy(case['ops'])
    ops[3]['b']=hex(int(ops[3]['b'],16)+1)[2:]
    with pytest.raises(ValueError):extract(ops,case['minted_shares_raw'])
    with pytest.raises(ValueError):extract(case['ops']*2,case['minted_shares_raw'])


def test_trace_does_not_accept_mixed_depth_or_different_receipt_mint():
    case=CASES[0];ops=copy.deepcopy(case['ops']);ops[3]['depth']+=1
    with pytest.raises(ValueError):extract(ops,case['minted_shares_raw'])
    with pytest.raises(ValueError):extract(case['ops'],int(case['minted_shares_raw'])+1)
