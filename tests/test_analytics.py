from types import SimpleNamespace
import json

import pytest
from yearn_data.analytics import (SCHEMA, AnalyticsStore, analytics_response, capital_period,
    comparable, fee_stack, profitability, put_input, DAY, YEAR)
from yearn_data.analytics_collect import rules
from yearn_data.storage import connect

A='0x'+'11'*20;B='0x'+'22'*20;C='0x'+'33'*20

def rows():
    return [{'chain_id':1,'vault':A,'name':'parent','tvl_usd':'100','external_tvl_usd':'100','category':'v3'},
            {'chain_id':1,'vault':B,'name':'child','tvl_usd':'60','external_tvl_usd':'0','category':'v3'}]

def configs():
    return {'vaults':[{'chainId':1,'address':A,'performanceFee':1000,'managementFee':100},
                      {'chainId':1,'address':B,'performanceFee':1000,'managementFee':0}]}

def test_stack_rates_capital_and_unknown():
    positions=[{'chain_id':1,'parent':A,'strategy':C,'child':B,'debt_usd':'60','status':'ok'}]
    result=fee_stack(rows(),positions,configs())
    assert result['chains'][0]['effectivePerfFee']==pytest.approx(1540)
    assert result['chains'][0]['effectiveMgmtFee']==100
    assert result['chains'][0]['root']['children'][0]['viaStrategy']==C
    assert result['totalStackedCapital']==100
    unknown=configs();unknown['vaults'][1]['performanceFee']=None
    assert fee_stack(rows(),positions,unknown)['chains'][0]['effectivePerfFee'] is None
    zero=configs();zero['vaults'][1]['performanceFee']=0
    assert fee_stack(rows(),positions,zero)['chains'][0]['effectivePerfFee']==pytest.approx(1000)


def test_cycle_is_not_complete():
    positions=[{'chain_id':1,'parent':a,'strategy':b,'child':b,'debt_usd':'50','status':'ok'} for a,b in [(A,B),(B,A)]]
    result=fee_stack(rows(),positions,configs())
    assert all(c['effectivePerfFee'] is None for c in result['chains'])
    assert result['availability']['state']=='partial'


def test_membership_unknown_is_neither_missing_nor_included():
    r=rows();r[1]['external_tvl_usd']='60'
    membership={'vaults':[{'chainId':1,'address':A.upper(),'included':False}]}
    result=comparable(r,membership)
    assert result['diff']['missingFromDefillama'][0]['countedTvlUsd']==100
    assert result['includedVaults']==[]
    assert result['availability']['unknownVaults'][0]['vaultAddress']==B
    assert result['availability']['state']=='partial'


def test_capital_time_weighted_and_gaps():
    points=[(i*DAY+DAY-1,100 if i<180 else 300) for i in range(360)]
    assert capital_period(points,0,360*DAY)['average']==200
    assert capital_period(points[:80],0,360*DAY)['eligible'] is False
    assert capital_period([(t,None) for t,v in points],0,360*DAY)['average'] is None


def test_profitability_period_chain_retired_and_no_nested_yield_sum():
    cutoff=YEAR
    history={(1,A):[(i*DAY+DAY-1,100 if i<180 else 300) for i in range(365)]}
    financial=SimpleNamespace(fees=[{'chain_id':1,'vault_address':A,'block_timestamp':100*DAY,
                                   'total_fees_paid_usd':'20','price_usd':1}],
                              earnings=[{'chain_id':1,'vault_address':A,'block_timestamp':100*DAY,
                                         'gross_gain_usd':'100','version':'v3','price_usd':1}],vault_names={})
    result=profitability(financial,rows(),history,configs(),cutoff,1)
    parent=next(v for v in result['vaults'] if v['address']==A)
    assert parent['feeYield']==pytest.approx(20/(100*180/365+300*185/365))
    assert parent['timeWeightedTvlUsd']!=parent['tvlUsd']
    assert parent['trend']=='insufficient_data'
    assert parent['totalGainUsd']==100
    assert parent['feeCapture']==0.2
    assert result['period']['endTimestamp']==cutoff
    assert 'gross contract' in result['capitalBasis']
    unavailable=next(v for v in result['vaults'] if v['address']==B)
    assert unavailable['quadrant'] is None
    assert unavailable['annualizedFeeRevenue'] is None
    assert profitability(financial,rows(),history,configs(),cutoff,10)['vaults']==[]


def test_publications_pinned_filters_and_atomic_selection(tmp_path):
    database=tmp_path/'data.sqlite';conn=connect(database);conn.executescript(SCHEMA)
    identity='a'*64
    meta={'publicationId':identity,'tvlDatasetId':'b'*64}
    data={'meta':meta,'stack':{},'comparison':{},'comparable':{'diff':{'missingFromDefillama':[]}},'profitability':{'None':{},'1':{}}}
    with conn:
        conn.execute('INSERT INTO analytics_publications VALUES (?,?,?)',(identity,1,json.dumps(data)))
        conn.execute('INSERT INTO analytics_selection VALUES (?,?)',('powerglove',identity))
    store=AnalyticsStore(database)
    assert analytics_response(store,'/api/comparison/defillama-comparable?includeVaultBreakdown=false')[0]==200
    assert analytics_response(store,'/api/profitability?chainId=1')[0]==200
    assert analytics_response(store,'/api/profitability?chainId=999')[0]==400
    assert analytics_response(store,'/api/profitability?chainId=1&chainId=1')[0]==400
    assert analytics_response(store,'/api/fees/stack?publicationId=wrong')[0]==400
    assert analytics_response(store,'/api/analytics/publication')[1]==meta
    with conn:conn.execute("DELETE FROM analytics_selection")
    assert analytics_response(store,'/api/fees/stack?publicationId='+identity)[0]==200


def test_input_zero_stored_and_bad_adapter_fails(tmp_path):
    conn=connect(tmp_path/'data.sqlite');conn.executescript(SCHEMA)
    identity=put_input(conn,'fee-config',configs(),observed_at=1)
    assert identity==put_input(conn,'fee-config',configs(),observed_at=1)
    with pytest.raises(ValueError,match='unsupported finance adapter'):rules('garbage','garbage')


def test_failed_retry_preserves_known_zero_and_membership(tmp_path):
    from yearn_data.analytics import selected_input
    conn=connect(tmp_path/'data.sqlite');conn.executescript(SCHEMA)
    put_input(conn,'fee-config',{'vaults':[{'chainId':1,'address':A,'performanceFee':0,'managementFee':0,'observedAt':1}]},observed_at=1)
    put_input(conn,'fee-config',{'vaults':[{'chainId':1,'address':A,'performanceFee':None,'managementFee':None}]},status='partial',observed_at=2)
    latest=selected_input(conn,'fee-config')['body']['vaults'][0]
    assert latest['performanceFee']==0
    assert latest['retainedObservedAt']==1
    put_input(conn,'membership',{'vaults':[{'chainId':1,'address':A,'included':True}]},observed_at=1)
    put_input(conn,'membership',{'vaults':[{'chainId':1,'address':A,'included':None}]},status='partial',observed_at=2)
    assert selected_input(conn,'membership')['body']['vaults'][0]['included'] is True


def test_stack_unsupported_configuration_is_not_fixed_rate():
    c=configs();c['vaults'][0]['fixedRateSupported']=False
    result=fee_stack(rows(),[{'chain_id':1,'parent':A,'strategy':B,'child':B,'debt_usd':'60','status':'ok'}],c)
    assert result['chains'][0]['effectivePerfFee'] is None


def test_prepare_freezes_inputs_and_retains_old_selection(tmp_path):
    from yearn_data.analytics import prepare
    db=tmp_path/'data.sqlite';conn=connect(db);conn.executescript(SCHEMA)
    put_input(conn,'fee-config',configs(),observed_at=1)
    put_input(conn,'membership',{'vaults':[{'chainId':1,'address':A,'included':True}]},observed_at=1)
    financial=SimpleNamespace(id='f'*64,cutoff=YEAR,fees=[],earnings=[],vault_names={})
    tvl=SimpleNamespace(id='b'*64,as_of=lambda:YEAR-1,frames=lambda dates:[(YEAR-1,rows(),[],{})],
                        dates=lambda:[i*DAY+DAY-1 for i in range(365)],history_prepared=lambda:False,
                        history_rows=lambda dates,chain:[(t,tuple(rows())) for t in dates],
                        summary=lambda:{'retiredVaultTvl':0,'overlapExcluded':60,'vaultBridgeExcluded':0})
    old=prepare(db,financial,tvl)
    store=AnalyticsStore(db)
    assert store.get()['meta']['publicationId']==old
    put_input(conn,'membership',{'vaults':[{'chainId':1,'address':A,'included':False}]},observed_at=2)
    new=prepare(db,financial,tvl)
    assert new!=old
    assert store.get(old)['comparable']['includedVaults'][0]['vaultAddress']==A
    assert store.get(new)['comparable']['diff']['missingFromDefillama'][0]['vaultAddress']==A
    assert prepare(db,financial,tvl)==new
    assert analytics_response(store,'/api/comparison?datasetId='+tvl.id)[0]==200


def test_quadrants_use_displayed_capital_and_zero_yield_is_low():
    points={};current=[];fee_rows=[]
    for i,(tvl,fee) in enumerate([(20000,0),(30000,50),(200000,0),(300000,500),(0,0)]):
        address='0x'+f'{i+1:040x}'
        current.append({'chain_id':1,'vault':address,'name':address,'category':'v3','tvl_usd':str(tvl)})
        points[(1,address)]=[(d*DAY+DAY-1,max(tvl,100)) for d in range(365)]
        fee_rows.append({'chain_id':1,'vault_address':address,'block_timestamp':100*DAY,'total_fees_paid_usd':str(fee)})
    financial=SimpleNamespace(id='f'*64,fees=fee_rows,earnings=[],vault_names={})
    result=profitability(financial,current,points,{},YEAR)
    assert result['quadrantThresholds']['cohortSize']==4
    assert all(len(vaults)==1 for vaults in result['quadrants'].values())
    assert result['vaults'][-1]['quadrant'] is None


def test_no_fee_observations_do_not_become_known_zero_yield():
    f=SimpleNamespace(fees=[],earnings=[],vault_names={})
    history={(1,A):[(i*DAY+DAY-1,100000) for i in range(365)]}
    current=[dict(rows()[0],tvl_usd='100000')]
    result=profitability(f,current,history,configs(),YEAR)['vaults'][0]
    assert result['totalFeeRevenue'] is None
    assert result['feeYield'] is None
    assert result['quadrant'] is None
    assert result['availability']['fees']['state']=='unavailable'
