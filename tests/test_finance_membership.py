import pytest
from yearn_data.finance_membership import capped_debt

def test_converter_cap_normalizes_child_units():
    route=dict(parentAsset='usdc',childAsset='usds',parentDecimals=6,childDecimals=18)
    assert capped_debt(100_000_000,200*10**18,1,4,route,'usdc','usds')==50_000_000
    assert capped_debt(10_000_000,200*10**18,1,4,route,'usdc','usds')==10_000_000
    with pytest.raises(ValueError,match='assets changed'):
        capped_debt(100,100,1,2,route,'dai','usds')

def test_router_zero_supply_and_invalid_shares():
    assert capped_debt(100,100,0,0,{},'asset','asset')==0
    with pytest.raises(ValueError,match='ownership'):
        capped_debt(100,100,2,1,{},'asset','asset')


def test_wrapper_and_unavailable_reads_are_distinct():
    from yearn_data.finance_membership import contributions
    parent='0x'+'11'*20; child='0x'+'22'*20; token='0x'+'33'*20
    class Reader:
        def call(self,calls):
            return [(child,) if a==parent else (token,) for a,*_ in calls]
    active={parent:{'asset':{'address':child}},child:{'asset':{'address':token}}}
    result,edges=contributions(Reader(),10,active,{parent:(50,),child:(100,)},{parent:[],child:None},{'cross':{},'routes':{}})
    assert result[parent]==0
    assert result[child] is None
    assert edges[0]['method']=='vault-asset'
