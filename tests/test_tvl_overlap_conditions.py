from decimal import Decimal

import pytest

from yearn_data.tvl import account
from yearn_data.tvl_sources import _position, candidate_edges
from test_tvl import address, snapshot, edge, db


def point(n, value='100', chain=1, asset=None):
    return snapshot(n,value,chain=chain) | {'block_number':10 if chain==1 else 20,'status':'ok',
        'total_assets_raw':'100','total_supply_raw':'100','asset_decimals':0,'price_usd':'1',
        'asset':asset or address(90)}


def test_wrapper_deducts_deployed_shares_not_just_idle_wallet_balance():
    parent, child = point(1,asset=address(2)), point(2)
    class Reader:
        def uint(self,*args):return 0  # All child shares are deployed elsewhere.
    p=_position({'chain_id':1,'parent':address(1),'strategy':address(1),'child':address(2),
                 'method':'vault-asset'},100,Reader(),{(1,address(1)):parent,(1,address(2)):child})
    assert p['status']=='ok' and p['balance_raw']=='0'
    assert p['owned_usd']=='100' and p['ownership_basis']=='total-assets-shares'
    _,_,totals=account([parent,child],[p])
    assert Decimal(totals['external_tvl_usd'])==100


def test_router_registry_destination_is_checked_at_the_collection_block():
    parent,child=point(1),point(2)
    class Reader:
        def debt(self,*args):return 80
        def uint(self,*args):return int(address(3),16)  # Different historical target.
    p=_position({'chain_id':1,'parent':address(1),'strategy':address(9),'child':address(2),
                 'method':'service-registry','vault_getter':'yVault()'},100,Reader(),
                {(1,address(1)):parent,(1,address(2)):child})
    assert p['status']=='unavailable'
    assert account([parent,child],[p])[2]['status']=='incomplete'


def remote_position(*,debt=80,shares=90,mismatch=False,failure=False):
    p,c=point(1),point(2,chain=747474)
    class Source:
        def debt(self,*args):return debt
        def uint(self,contract,sig,block,arg=None):
            assert block==10
            return {'REMOTE_CHAIN_ID()':747474,'REMOTE_COUNTERPART()':int(address(8),16)}[sig]
    class Remote:
        def uint(self,contract,sig,block,arg=None):
            assert block==20
            if failure:raise RuntimeError('archive unavailable')
            return {'REMOTE_COUNTERPART()':int(address(7 if mismatch else 9),16),
                    'vault()':int(address(2),16),'balanceOf(address)':shares}[sig]
    position=_position({'chain_id':1,'parent':address(1),'strategy':address(9),'child':address(2),
                        'child_chain_id':747474,'remote_holder':address(8),'method':'cross-chain-shares'},
                       100,Source(),{(1,address(1)):p,(747474,address(2)):c},{747474:Remote()})
    return p,c,position


@pytest.mark.parametrize('debt,shares,expected',[(80,90,80),(90,80,80),(0,90,0)])
def test_cross_chain_exposure_uses_smaller_dated_debt_and_remote_holding(debt,shares,expected):
    p,c,e=remote_position(debt=debt,shares=shares)
    unrelated=point(2,chain=1)  # Same address on a different chain is distinct.
    rows,positions,totals=account([p,c,unrelated],[e])
    assert Decimal(positions[0]['overlap_usd'])==expected
    by={(r['chain_id'],r['vault']):r for r in rows}
    assert Decimal(by[(747474,address(2))]['external_tvl_usd'])==100-expected
    assert by[(1,address(2))]['external_tvl_usd']=='100'
    assert Decimal(totals['external_tvl_usd'])==300-expected


@pytest.mark.parametrize('failure',['mismatch','failure'])
def test_cross_chain_unverified_remote_state_remains_unknown(failure):
    p,c,e=remote_position(**{failure:True})
    assert e['status']=='unavailable'
    _,positions,totals=account([p,c],[e])
    assert positions[0]['overlap_usd'] is None
    assert totals['external_tvl_usd'] is None and totals['known_external_tvl_usd']=='200'


def test_cross_chain_registry_replaces_unresolved_allocation_candidate(db,monkeypatch):
    db.execute('INSERT INTO tvl_strategies VALUES (1,?,?,?)',(address(1),address(9),'fixture'))
    monkeypatch.setattr('yearn_data.tvl_sources.REGISTRY',{'targets':[],'crossChainStrategies':[
        {'chainId':1,'sourceVaultAddress':address(1),'strategyAddress':address(9),
         'targetChainId':747474,'targetVaultAddress':address(2),'remoteStrategyAddress':address(8)}]})
    vaults=[dict(r) for r in db.execute('SELECT * FROM tvl_vaults')]
    edges=[e for e in candidate_edges(db,vaults) if e['strategy']==address(9)]
    assert len(edges)==1 and edges[0]['child_chain_id']==747474
    assert edges[0]['method']=='cross-chain-shares'
