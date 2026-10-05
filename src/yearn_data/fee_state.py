"""Historical V2 fee state acquisition, pinned to the receipt block hash."""
from eth_utils import keccak, to_checksum_address
from web3.exceptions import ContractLogicError

from .fees import _hex, unsigned

# Initially limit promotion to releases represented by the frozen older-V2 corpus.
SUPPORTED_RELEASES = {'0.2.2', '0.3.0', '0.3.1', '0.3.2'}
# Pinned releases with the same receipt fee-mint pattern; state formulas remain separately gated.
NO_MINT_RELEASES = SUPPORTED_RELEASES | {'0.3.3', '0.3.5', '0.4.2', '0.4.3'}


def read_fee_state(client, report, receipt, shares):
    block_hash = _hex(receipt['blockHash'])
    address = to_checksum_address(report['vault_address'])

    def call(signature):
        data = '0x' + keccak(text=signature)[:4].hex()
        result = bytes(client.eth.call({'to': address, 'data': data}, block_identifier=block_hash))
        # Some old Vyper vault getters return a word plus zero-filled memory.
        if len(result) < 32 or len(result) % 32 or any(result[32:]):
            raise ValueError('invalid historical state response')
        return int.from_bytes(result[:32], 'big')

    state = {'block_hash': block_hash, 'state_formula': None,
             'share_decimals': call('decimals()'),
             'price_per_share_block_raw': str(call('pricePerShare()'))}
    supply, assets = call('totalSupply()'), call('totalAssets()')
    state.update(post_total_supply_raw=str(supply), post_total_assets_raw=str(assets))
    gain, loss = unsigned(report['gain_raw']), unsigned(report['loss_raw'])
    minted = unsigned(shares['minted_shares_raw'])
    if report['api_version'] == '0.3.1':
        if supply < minted or assets < gain:
            raise ValueError('invalid pre-state reconstruction')
        state.update(state_formula='v2-0.3.1-pre-state', pre_total_supply_raw=str(supply - minted),
                     pre_valuation_assets_raw=str(assets - gain))
    else:
        try:
            locked = call('lockedProfit()')
        except ContractLogicError:
            # Known old releases can lack this getter. Transport failures propagate.
            state['state_unavailable_reason'] = 'locked_profit_call_reverted'
            return state
        if assets < locked:
            raise ValueError('locked profit exceeds assets')
        free = assets - locked
        state.update(locked_profit_raw=str(locked), post_free_funds_raw=str(free))
        if loss and not locked:
            state['state_unavailable_reason'] = 'loss_with_zero_locked_profit'
            return state
        if free < loss:
            raise ValueError('loss exceeds free funds')
        state.update(inversion_assets_raw=str(free - loss),
                     state_formula='v2-locked-profit-loss-adjusted-post-state' if loss
                     else 'v2-locked-profit-post-state')
    return state
