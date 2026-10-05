from types import SimpleNamespace

import pytest
from eth_utils import keccak
from web3.exceptions import ContractLogicError

from yearn_data.fee_state import read_fee_state


def reader(values):
    requests = []
    selectors = {'0x' + keccak(text=k + '()')[:4].hex(): v for k, v in values.items()}
    def call(tx, block_identifier):
        requests.append(block_identifier)
        value = selectors[tx['data']]
        if isinstance(value, Exception):
            raise value
        return value.to_bytes(32, 'big')
    return SimpleNamespace(eth=SimpleNamespace(call=call)), requests


@pytest.mark.parametrize('version,loss,formula,assets', [
    ('0.3.1', '0', 'v2-0.3.1-pre-state', '980'),
    ('0.3.2', '0', 'v2-locked-profit-post-state', '900'),
    ('0.3.2', '10', 'v2-locked-profit-loss-adjusted-post-state', '890'),
])
def test_state_formulas_use_receipt_block_hash(version, loss, formula, assets):
    client, requests = reader(dict(decimals=18, pricePerShare=10**18, totalSupply=1000, totalAssets=1000, lockedProfit=100))
    state = read_fee_state(client, dict(vault_address='0x'+'12'*20, api_version=version, gain_raw='20', loss_raw=loss),
                          dict(blockHash='0x'+'ab'*32), dict(minted_shares_raw='5'))
    assert state['state_formula'] == formula
    assert state.get('pre_valuation_assets_raw', state.get('inversion_assets_raw')) == assets
    assert set(requests) == {'0x'+'ab'*32}


@pytest.mark.parametrize('error,allowed', [(ContractLogicError('reverted'), True), (ConnectionError('offline'), False)])
def test_only_contract_revert_allows_missing_getter_fallback(error, allowed):
    client, _ = reader(dict(decimals=6, pricePerShare=10**6, totalSupply=1000, totalAssets=1000, lockedProfit=error))
    args = (client, dict(vault_address='0x'+'12'*20, api_version='0.2.2', gain_raw='20', loss_raw='0'),
            dict(blockHash='0x'+'ab'*32), dict(minted_shares_raw='5'))
    if allowed:
        assert read_fee_state(*args)['state_unavailable_reason'] == 'locked_profit_call_reverted'
    else:
        with pytest.raises(ConnectionError):
            read_fee_state(*args)


@pytest.mark.parametrize('padding,valid', [(bytes(4064), True), (b'\x01' + bytes(31), False), (bytes(1), False)])
def test_historical_vyper_getter_padding(padding, valid):
    client, _ = reader(dict(decimals=18, pricePerShare=10**18, totalSupply=1000, totalAssets=1000, lockedProfit=100))
    original = client.eth.call
    client.eth.call = lambda *args, **kwargs: original(*args, **kwargs) + padding
    args = (client, dict(vault_address='0x'+'12'*20, api_version='0.3.2', gain_raw='20', loss_raw='0'),
            dict(blockHash='0x'+'ab'*32), dict(minted_shares_raw='5'))
    if valid:
        state = read_fee_state(*args)
        assert state['share_decimals'] == 18
        assert state['post_total_supply_raw'] == '1000'
        assert state['inversion_assets_raw'] == '900'
    else:
        with pytest.raises(ValueError, match='invalid historical state response'):
            read_fee_state(*args)


def test_loss_exhausting_locked_profit_stays_an_estimate():
    from yearn_data.fee_reconstruction import reconstruct_candidate
    from yearn_data.fee_acceptance import assess_candidate
    client,_=reader(dict(decimals=6,pricePerShare=10**6,totalSupply=1000,totalAssets=1000,lockedProfit=0))
    shares={'status':'found','minted_shares_raw':'5'}
    state=read_fee_state(client,dict(vault_address='0x'+'12'*20,api_version='0.3.2',gain_raw='20',loss_raw='10'),
                         dict(blockHash='0x'+'ab'*32),shares)
    assert state['state_formula'] is None
    assert state['state_unavailable_reason']=='loss_with_zero_locked_profit'
    candidate=reconstruct_candidate(shares,state)
    assert candidate['method']=='share-mint-report-block-pps'
    assert assess_candidate(candidate,{})['status']=='conditional'
