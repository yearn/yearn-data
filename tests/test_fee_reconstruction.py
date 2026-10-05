"""Historical characterization plus failure cases for the pure reconstruction core."""
import copy
import json
from pathlib import Path

import pytest

from yearn_data.fee_reconstruction import extract_fee_shares, fee_interval, reconstruct_candidate

ROOT = Path(__file__).parent / 'fixtures' / 'fees'
CASES = [c for c in json.loads((ROOT / 'cases.json').read_text()) if c['reconstruction']]
EXPECTED = {r['event_id']: r for r in json.loads((ROOT / 'expected.json').read_text())}
RECEIPTS = {(r['chain_id'], r['transaction_hash']): r['receipt']
            for r in json.loads((ROOT / 'receipts.json').read_text())}


def evidence_for(case):
    e = case['event']
    return extract_fee_shares(RECEIPTS[e['chain_id'], e['transaction_hash']]['logs'],
                              e['vault_address'], e['log_index'])


@pytest.mark.parametrize('case', CASES, ids=lambda c: str(c['event']['id']))
def test_receipts_and_candidates_match_historical_reference(case):
    e, state = case['event'], case['reconstruction']
    evidence = evidence_for(case)
    assert evidence['minted_shares_raw'] == state['minted_shares_raw']
    assert evidence['transferred_shares_raw'] == state['transferred_shares_raw']
    assert evidence['mint_log_index'] == state['mint_log_index']
    result = reconstruct_candidate(evidence, state)
    if e['amount_source'] == 'derived-contract':
        expected = EXPECTED[e['id']]
        assert result['total_fees_paid_raw'] == expected['accounting']['totalFeesPaidRaw']
        assert result['method'] == expected['method']
        interval = expected['interval']
        assert result['interval'] == (None if interval is None else {
            'lower_raw': interval['lowerRaw'], 'upper_raw': interval['upperRaw'], 'width_raw': interval['widthRaw']})
    else:
        # Independently validate reconstruction against direct FeeReport ground truth.
        assert result['precision'] == 'singleton'
        assert result['total_fees_paid_raw'] == EXPECTED[e['id']]['accounting']['totalFeesPaidRaw']


def test_ambiguous_or_mismatched_transfers_cannot_be_promoted():
    for status in ('ambiguous-mints', 'transfer-mismatch', 'invalid-transfer'):
        with pytest.raises(ValueError, match='unresolved'):
            reconstruct_candidate({'status': status}, {})


def test_corrupt_transfer_is_not_no_mint():
    case = CASES[0]
    e = case['event']
    logs = copy.deepcopy(RECEIPTS[e['chain_id'], e['transaction_hash']]['logs'])
    index = evidence_for(case)['mint_log_index']
    next(log for log in logs if int(log['logIndex'], 16) == index)['data'] = '0x'
    with pytest.raises(ValueError, match='invalid fee share transfer'):
        extract_fee_shares(logs, e['vault_address'], e['log_index'])


@pytest.mark.parametrize('mint,supply,assets', [(0, 1, 1), (2, 1, 1), (1, 2, 0), (-1, 2, 3), (1, 1, 1)])
def test_invalid_or_impossible_post_state(mint, supply, assets):
    with pytest.raises(ValueError):
        fee_interval(mint, supply, assets)


def test_unknown_formula_does_not_silently_fall_back_to_pps():
    case = CASES[0]
    with pytest.raises(ValueError, match='unsupported state formula'):
        reconstruct_candidate(evidence_for(case), {**case['reconstruction'], 'state_formula': 'unknown'})


def test_missing_pps_is_not_zero():
    case = CASES[0]
    with pytest.raises(ValueError):
        reconstruct_candidate(evidence_for(case), {**case['reconstruction'], 'price_per_share_block_raw': None})


def test_share_windows_do_not_reuse_previous_report_mints():
    from yearn_data.fees import REPORT_TOPICS
    case = CASES[0]
    event = case['event']
    original = RECEIPTS[event['chain_id'], event['transaction_hash']]['logs']
    upper = event['log_index']
    window = [copy.deepcopy(log) for log in original if log['address'].lower() == event['vault_address']
              and int(log['logIndex'], 16) <= upper]
    report = next(log for log in window if int(log['logIndex'], 16) == upper and log['topics'][0] in REPORT_TOPICS)
    offset = max(int(log['logIndex'], 16) for log in original) + 100
    second = copy.deepcopy(window)
    for log in second:
        log['logIndex'] = hex(int(log['logIndex'], 16) + offset)
    combined = list(reversed(window + second))
    first_evidence = extract_fee_shares(combined, event['vault_address'], upper)
    second_evidence = extract_fee_shares(combined, event['vault_address'], upper + offset)
    assert first_evidence['candidate_mint_count'] == second_evidence['candidate_mint_count'] == 1
    assert second_evidence['previous_report_log_index'] == upper
    assert second_evidence['mint_log_index'] == first_evidence['mint_log_index'] + offset
    assert second_evidence['minted_shares_raw'] == first_evidence['minted_shares_raw']
    # A second report with no new mint must not inherit the first report's fee.
    report = copy.deepcopy(report)
    report['logIndex'] = hex(upper + offset)
    empty = extract_fee_shares(window + [report], event['vault_address'], upper + offset)
    assert empty['status'] == 'no-mint'
    assert empty['recipient_transfers'] == []
