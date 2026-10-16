"""Pure V2 reconstruction primitives, characterized against fees-service main.

Inputs must come from complete receipts and historical report-block state.
This module does not fetch state or promote candidates into canonical storage.
"""
from eth_utils import keccak

from .fees import REPORT_TOPICS, _hex, _number, unsigned

TRANSFER_TOPIC = '0x' + keccak(text='Transfer(address,address,uint256)').hex()
ZERO = '0x' + '0' * 40


def extract_fee_shares(logs, vault_address, report_log_index):
    vault = vault_address.lower()
    prior = sorted((log for log in logs if log['address'].lower() == vault
                    and _number(log['logIndex']) < report_log_index),
                   key=lambda log: _number(log['logIndex']))
    previous = max((_number(log['logIndex']) for log in prior
                    if log['topics'] and _hex(log['topics'][0]) in REPORT_TOPICS), default=-1)
    transfers = []
    for log in prior:
        if (_number(log['logIndex']) <= previous or not log['topics']
                or _hex(log['topics'][0]) != TRANSFER_TOPIC):
            continue
        topics = log['topics']
        data = bytes.fromhex(_hex(log['data'])[2:])
        if len(topics) != 3 or len(data) != 32:
            raise ValueError('invalid fee share transfer')
        addresses = []
        for topic in topics[1:]:
            word = bytes.fromhex(_hex(topic)[2:])
            if len(word) != 32 or any(word[:12]):
                raise ValueError('invalid transfer address')
            addresses.append('0x' + word[-20:].hex())
        transfers.append({'log_index': _number(log['logIndex']), 'from': addresses[0],
                          'to': addresses[1], 'shares_raw': str(int.from_bytes(data, 'big'))})
    mints = [t for t in transfers if t['from'] == ZERO and t['to'] == vault and int(t['shares_raw']) > 0]
    evidence = {'previous_report_log_index': previous if previous >= 0 else None,
                'candidate_mint_count': len(mints), 'mint_log_index': None,
                'minted_shares_raw': None, 'transferred_shares_raw': None, 'recipient_transfers': []}
    # A positive asset fee can round down to zero shares. The emitted zero
    # Transfer is evidence of an attempted mint, not proof that no fee existed.
    zero_mints = [t for t in transfers if t['from'] == ZERO and t['to'] == vault and int(t['shares_raw']) == 0]
    if zero_mints:
        if len(zero_mints) == 1 and not mints:
            evidence.update(mint_log_index=zero_mints[0]['log_index'], minted_shares_raw='0')
        return {**evidence, 'status': 'zero-share-mint'}
    if not mints:
        return {**evidence, 'status': 'no-mint'}
    if len(mints) != 1:
        return {**evidence, 'status': 'ambiguous-mints'}
    mint = mints[0]
    recipients = [t for t in transfers if t['log_index'] > mint['log_index']
                  and t['from'] == vault and t['to'] != ZERO]
    transferred = sum(int(t['shares_raw']) for t in recipients)
    return {**evidence, 'status': 'found' if transferred == int(mint['shares_raw']) else 'transfer-mismatch',
            'mint_log_index': mint['log_index'], 'minted_shares_raw': mint['shares_raw'],
            'transferred_shares_raw': str(transferred), 'recipient_transfers': recipients}


def fee_interval(minted_shares, supply, assets, *, pre_state=False):
    minted, supply, assets = map(unsigned, (minted_shares, supply, assets))
    if not minted or not supply or not assets or (not pre_state and supply < minted):
        raise ValueError('invalid fee inversion state')
    lower = (minted * assets + supply - 1) // supply
    upper = ((minted + 1) * assets - 1) // (supply if pre_state else supply + 1)
    if upper < lower:
        raise ValueError('fee mint has no valid asset interval')
    return {'lower_raw': str(lower), 'upper_raw': str(upper), 'width_raw': str(upper - lower)}


def reconstruct_candidate(evidence, state):
    """Select a candidate from verified mint evidence and recorded state inputs.

    A caller must validate receipt identity, completeness, report amounts and
    release support before using this result. Unknown/malformed state raises;
    a PPS fallback requires an explicitly absent state formula.
    """
    if evidence['status'] == 'no-mint':
        return {'total_fees_paid_raw': '0', 'method': 'receipt-no-mint-zero',
                'calculation_version': 'v2-fee-calculation-1', 'precision': 'receipt-zero', 'interval': None}
    if evidence['status'] != 'found':
        raise ValueError('fee share evidence is unresolved')
    minted = unsigned(evidence['minted_shares_raw'])
    formula = state.get('state_formula')
    if formula == 'v2-0.3.1-pre-state':
        interval = fee_interval(minted, state['pre_total_supply_raw'],
                                state['pre_valuation_assets_raw'], pre_state=True)
    elif formula in ('v2-locked-profit-post-state', 'v2-locked-profit-loss-adjusted-post-state'):
        interval = fee_interval(minted, state['post_total_supply_raw'], state['inversion_assets_raw'])
    elif formula is None:
        decimals = unsigned(state['share_decimals'])
        if decimals > 255:
            raise ValueError('invalid share decimals')
        amount = minted * unsigned(state['price_per_share_block_raw']) // 10 ** decimals
        return {'total_fees_paid_raw': str(amount), 'method': 'share-mint-report-block-pps',
                'calculation_version': 'v2-fee-calculation-1', 'precision': 'pps-estimate', 'interval': None}
    else:
        raise ValueError('unsupported state formula')
    exact = interval['width_raw'] == '0'
    return {'total_fees_paid_raw': interval['lower_raw'], 'interval': interval,
            'method': 'contract-state-singleton' if exact else 'contract-state-lower-bound',
            'calculation_version': 'v2-fee-calculation-1', 'precision': 'singleton' if exact else 'bounded-interval'}
