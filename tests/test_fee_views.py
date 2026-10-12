from types import SimpleNamespace

import pytest
from eth_utils import keccak
from yearn_data.analytics import fee_stack
from yearn_data.analytics_collect import custom_accountant_config
from yearn_data.ybold_fees import VAULT as YBOLD, STAKER, ACCOUNTANT, CODE_HASH
from yearn_data.yvusd_fees import VAULT as YVUSD

LOCKED = '0xaaafea48472f77563961cdb53291dedfb46f9040'
LOAN = '0x' + '1' * 40


def test_own_rates_survive_incomplete_underlying_allocations():
    rows = [{'chain_id': 1, 'vault': YBOLD, 'name': 'Yearn BOLD', 'tvl_usd': '100', 'external_tvl_usd': '10'},
            {'chain_id': 1, 'vault': STAKER, 'name': 'Staked yBOLD', 'tvl_usd': '90', 'external_tvl_usd': '90'}]
    positions = [{'chain_id': 1, 'parent': STAKER, 'strategy': STAKER, 'child': YBOLD, 'debt_usd': '90', 'status': 'ok'},
                 {'chain_id': 1, 'parent': YBOLD, 'strategy': LOAN, 'debt_usd': None, 'status': 'unavailable'}]
    configs = {'vaults': [{'chainId': 1, 'address': YBOLD, 'performanceFee': 0, 'managementFee': 0, 'fixedRateSupported': True},
                         {'chainId': 1, 'address': STAKER, 'performanceFee': 1000, 'managementFee': 0, 'fixedRateSupported': True}]}
    result = fee_stack(rows, positions, configs)
    assert result['chains'][0]['effectivePerfFee'] is None
    entries = {r['vault']['address']: r for r in result['entries']}
    assert entries[STAKER]['performanceFee'] == 1000
    assert entries[STAKER]['vault']['name'] == 'yBOLD'
    assert entries[YBOLD]['performanceFee'] == 0
    assert entries[YBOLD]['vault']['name'] == 'Unstaked yBOLD'
    configs['vaults'][0]['fixedRateSupported'] = False
    assert next(r for r in fee_stack(rows, positions, configs)['entries'] if r['vault']['address'] == YBOLD)['performanceFee'] is None


def test_yvusd_groups_external_capital_and_keeps_underlying_rates():
    rows = [{'chain_id': 1, 'vault': a, 'name': a, 'tvl_usd': str(gross), 'external_tvl_usd': str(external)}
            for a, gross, external in [(YVUSD, 100, 60), (LOCKED, 40, 35), (LOAN, 80, 0)]]
    positions = [{'chain_id': 1, 'parent': parent, 'strategy': child, 'child': child, 'debt_usd': str(debt), 'status': 'ok'}
                 for parent, child, debt in [(LOCKED, YVUSD, 40), (YVUSD, LOAN, 80)]]
    configs = {'vaults': [{'chainId': 1, 'address': a, 'performanceFee': fee, 'managementFee': 0, 'fixedRateSupported': True}
                         for a, fee in [(YVUSD, 0), (LOCKED, 0), (LOAN, 500)]]}
    result = fee_stack(rows, positions, configs)
    entries = {r['vault']['address']: r for r in result['entries']}
    assert LOCKED not in entries
    product = entries[YVUSD]
    assert product['vault']['name'] == 'yvUSD'
    assert product['adjustedTvlUsd'] == 95  # Locked capital owned upstream stays excluded.
    assert product['performanceFee'] == product['managementFee'] == 0
    assert product['feeRateBasis'] == 'product'
    assert len(product['roots']) == 2
    assert product['roots'][0]['children'][0]['perfFee'] == 500
    assert any(c['effectivePerfFee'] > 0 for c in result['chains'])
    rows[1]['external_tvl_usd'] = None
    assert next(r for r in fee_stack(rows, positions, configs)['entries'] if r['vault']['address'] == YVUSD)['adjustedTvlUsd'] is None
    configs['vaults'][1]['performanceFee'] = 100
    assert next(r for r in fee_stack(rows, positions, configs)['entries'] if r['vault']['address'] == YVUSD)['performanceFee'] is None


def reader(monkeypatch, address, code_hash, response):
    import eth_utils
    monkeypatch.setattr(eth_utils, 'keccak', lambda code: bytes.fromhex(code_hash[2:]))
    block = b'1' * 32
    def code(target, block_identifier):
        assert target.lower() == address
        assert block_identifier == block
        return b'code'
    return SimpleNamespace(block={'hash': block}, w3=SimpleNamespace(eth=SimpleNamespace(get_code=code)), call=lambda calls: response)


def test_custom_ybold_accountant_requires_verified_recipient(monkeypatch):
    r = reader(monkeypatch, ACCOUNTANT, CODE_HASH, [(STAKER,)])
    config = {'chainId': 1, 'address': YBOLD, 'accountant': ACCOUNTANT}
    assert custom_accountant_config(r, config)['performanceFee'] == 0
    r.call = lambda calls: [(LOAN,)]
    with pytest.raises(ValueError, match='recipient'):
        custom_accountant_config(r, config)
    with pytest.raises(ValueError, match='accountant'):
        custom_accountant_config(r, config | {'accountant': LOAN})


def test_yvusd_reads_actual_fee_config_separately_from_locker_bonus(monkeypatch):
    code_hash = '0x237ca0cef5a652adde023f9a80e5dbd8535e4a36cfd216bd42bfa7af31e8ebf1'
    r = reader(monkeypatch, LOCKED, code_hash, [(YVUSD,), (25, 50, 1000)])
    config = {'chainId': 1, 'address': YVUSD, 'accountant': LOCKED}
    amounts = custom_accountant_config(r, config)
    assert (amounts['managementFee'], amounts['performanceFee'], amounts['lockerBonus']) == (25, 50, 1000)
    r.call = lambda calls: [(YVUSD,), None]
    with pytest.raises(ValueError, match='configuration'):
        custom_accountant_config(r, config)
    r = reader(monkeypatch, LOCKED, '0x' + '0' * 64, [])
    with pytest.raises(ValueError, match='implementation'):
        custom_accountant_config(r, config)


@pytest.mark.parametrize('positions', [
    [],
    [{'chain_id': 1, 'parent': LOAN, 'strategy': '0x' + '2' * 40,
      'child': '0x' + '2' * 40, 'debt_usd': '100', 'status': 'ok'}],
])
def test_standalone_vaults_have_direct_deposit_entries_without_a_nested_stack(positions):
    rows = [{'chain_id': 1, 'vault': LOAN, 'name': 'Curve DOLA-sUSDe Factory yVault',
             'tvl_usd': '100', 'external_tvl_usd': '100'}]
    configs = {'vaults': [{'chainId': 1, 'address': LOAN, 'performanceFee': 1000,
                           'managementFee': 0, 'fixedRateSupported': True}]}
    result = fee_stack(rows, positions, configs)
    assert result['chains'] == []  # Legacy stack statistics still describe nesting.
    assert result['totalStackedCapital'] == 0
    assert len(result['entries']) == 1
    entry = result['entries'][0]
    assert entry['vault']['address'] == LOAN
    assert entry['performanceFee'] == pytest.approx(1000)
    assert entry['managementFee'] == 0
    assert entry['adjustedTvlUsd'] == 100
    assert entry['roots'][0]['children'] == []
    configs['vaults'][0]['performanceFee'] = None
    assert fee_stack(rows, positions, configs)['entries'][0]['performanceFee'] is None


def test_adding_standalone_entries_preserves_nested_stacks_and_counts_each_vault_once():
    from copy import deepcopy
    rows = [{'chain_id': 1, 'vault': a, 'name': a, 'tvl_usd': str(gross),
             'external_tvl_usd': str(external)}
            for a, gross, external in [(YVUSD, 100, 100), (LOAN, 60, 0)]]
    configs = {'vaults': [{'chainId': 1, 'address': a, 'performanceFee': fee, 'managementFee': 0}
                         for a, fee in [(YVUSD, 0), (LOAN, 500)]]}
    positions = [{'chain_id': 1, 'parent': YVUSD, 'strategy': LOAN, 'child': LOAN,
                  'debt_usd': '60', 'status': 'ok'}]
    previous = fee_stack(rows, positions, configs)
    standalone = '0x' + '3' * 40
    extended_rows = deepcopy(rows) + [{'chain_id': 1, 'vault': standalone, 'name': 'Standalone',
                                     'tvl_usd': '40', 'external_tvl_usd': '40'}]
    extended_configs = deepcopy(configs)
    extended_configs['vaults'].append({'chainId': 1, 'address': standalone,
                                     'performanceFee': 1000, 'managementFee': 200})
    result = fee_stack(extended_rows, positions, extended_configs)
    for field in ('chains', 'maxDepth', 'maxEffectivePerfFee', 'avgEffectivePerfFee',
                  'totalStackedCapital', 'availability'):
        assert result[field] == previous[field]
    entries = {r['vault']['address']: r for r in result['entries']}
    assert len(entries) == len(result['entries']) == 3
    assert entries[standalone]['performanceFee'] == pytest.approx(1000)
    assert entries[standalone]['managementFee'] == 200
    assert entries[LOAN]['adjustedTvlUsd'] == 0  # Upstream-owned capital remains deducted.
    assert entries[YVUSD]['performanceFee'] == 0
    assert entries[YVUSD]['roots'][0]['children'][0]['perfFee'] == 500


@pytest.mark.parametrize('tvl', [None, '0'])
def test_standalone_entries_keep_existing_tvl_eligibility(tvl):
    rows = [{'chain_id': 1, 'vault': LOAN, 'name': 'Standalone',
             'tvl_usd': tvl, 'external_tvl_usd': tvl}]
    configs = {'vaults': [{'chainId': 1, 'address': LOAN, 'performanceFee': 1000, 'managementFee': 0}]}
    assert fee_stack(rows, [], configs)['entries'] == []
