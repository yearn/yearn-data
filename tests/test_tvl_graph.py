import json

import pytest

from yearn_data.tvl_api import TvlStore, tvl_response
from test_tvl_api import source, run, DAY, PARENT, CHILD, BRIDGE

HOLDER = '0x'+'44'*20


def router(conn, run_id=1, parent=PARENT, child=CHILD, holder=HOLDER, debt='80', owned='90'):
    row = {'chain_id':1,'parent':parent,'strategy':holder,'child':child,'timestamp':DAY,
           'block_number':100+DAY,'method':'service-registry','debt_usd':debt,'owned_usd':owned,'status':'ok'}
    conn.execute('INSERT INTO tvl_positions VALUES (?,?,?,?,?,?,?)',
                 (run_id,1,parent,holder,child,DAY,json.dumps(row)))
    conn.commit()


def test_router_context_and_category_totals(source):
    conn,path,root=source
    run(conn,1,DAY,[(PARENT,'100'),(CHILD,'500')])
    row=json.loads(conn.execute('SELECT data_json FROM tvl_snapshots WHERE vault=?',(PARENT,)).fetchone()[0])
    row.update(version='v2',category='v2')
    conn.execute('UPDATE tvl_snapshots SET data_json=? WHERE vault=?',(json.dumps(row),PARENT))
    conn.execute("UPDATE tvl_vaults SET active=0,version='v2',api_version='0.4.3' WHERE address=?",(PARENT,))
    router(conn)
    store=TvlStore(path,root)
    status,g=tvl_response(store,'/api/tvl/graph?chainId=1&category=v3&includeRetired=true')
    assert status==200
    assert {n['id'] for n in g['nodes']}=={f'1:{a}' for a in (PARENT,CHILD,HOLDER)}
    assert [(e['kind'],e['tvlUsd'],e['deductedTvlUsd']) for e in g['edges']]==[('registry',80,80),('strategy',80,0)]
    assert (g['totalRawTvlUsd'],g['totalTvlUsd'],g['totalOverlapTvlUsd'])==(500,420,80)
    assert g['snapshotAt']==DAY and g['datasetId']==store.get().id
    _,active=tvl_response(store,'/api/tvl/graph?category=v3&includeRetired=false')
    assert [n['address'] for n in active['nodes']]==[CHILD]
    assert active['edges']==[] and active['totalTvlUsd']==500


def test_shared_holder_does_not_duplicate_parent_debt_or_child_holdings(source):
    conn,path,root=source
    run(conn,1,DAY,[(PARENT,'100'),(CHILD,'500'),(BRIDGE,'300')])
    # BRIDGE is just a second ordinary parent in this fixture.
    row=json.loads(conn.execute('SELECT data_json FROM tvl_snapshots WHERE vault=?',(BRIDGE,)).fetchone()[0])
    row.pop('bridge_target_chain')
    conn.execute('UPDATE tvl_snapshots SET data_json=? WHERE vault=?',(json.dumps(row),BRIDGE))
    router(conn,parent=PARENT,debt='100',owned='200')
    router(conn,parent=BRIDGE,debt='300',owned='200')
    g=TvlStore(path,root).get().graph()
    assert sum(e['tvlUsd'] for e in g['edges'] if e['kind']=='registry')==200
    assert len([e for e in g['edges'] if e['kind']=='registry'])==1
    assert g['totalRawTvlUsd']==900 and g['totalTvlUsd']==700


def test_curation_flow_is_visible_without_changing_headline_policy(source):
    conn,path,root=source
    run(conn,1,DAY,[(PARENT,'100'),(CHILD,'100')])
    row=json.loads(conn.execute('SELECT data_json FROM tvl_snapshots WHERE vault=?',(CHILD,)).fetchone()[0])
    row['category']='curation'
    conn.execute('UPDATE tvl_snapshots SET data_json=? WHERE vault=?',(json.dumps(row),CHILD))
    router(conn)
    g=TvlStore(path,root).get().graph()
    flow=next(e for e in g['edges'] if e['kind']=='registry')
    assert flow['tvlUsd']==80 and flow['deductedTvlUsd']==0
    assert g['totalTvlUsd']==200 and g['totalOverlapTvlUsd']==0


def test_unknowns_are_diagnostics_not_zero_value_nodes_and_dataset_is_pinned(source):
    conn,path,root=source
    run(conn,1,DAY,[(PARENT,'100'),(CHILD,None)])
    store=TvlStore(path,root,refresh_seconds=0)
    old=store.get().id
    run(conn,2,DAY+86400,[(PARENT,'200'),(CHILD,'300')])
    _,g=tvl_response(store,'/api/tvl/graph?datasetId='+old)
    assert g['datasetId']==old and g['totalTvlUsd']==100
    assert [n['address'] for n in g['nodes']]==[PARENT]
    assert g['diagnostics']['unavailableNodes'][0]['nodeId']==f'1:{CHILD}'
    assert g['diagnostics']['status']=='incomplete'


@pytest.mark.parametrize('query',['includeRetired=1','category=unknown','chainId=0','chainId=1&chainId=1','from=1'])
def test_invalid_graph_filters(source,query):
    conn,path,root=source
    run(conn,1,DAY,[(PARENT,'100')])
    assert tvl_response(TvlStore(path,root),'/api/tvl/graph?'+query)[0]==400


def test_cross_chain_graph_and_chain_summary_account_before_filtering(source):
    conn,path,root=source
    run(conn,1,DAY,[(PARENT,'100')])
    remote={'chain_id':10,'vault':CHILD,'timestamp':DAY,'block_number':50,'version':'v3','category':'v3',
            'tvl_usd':'200','status':'ok','name':'Remote vault','asset_units':'200'}
    conn.execute('INSERT INTO tvl_snapshots VALUES (1,10,?,?,50,?)',(CHILD,DAY,json.dumps(remote)))
    p={'chain_id':1,'parent':PARENT,'strategy':HOLDER,'child':CHILD,'child_chain_id':10,
       'remote_holder':'0x'+'55'*20,'timestamp':DAY,'block_number':100+DAY,'child_block_number':50,
       'method':'cross-chain-shares','debt_usd':'80','owned_usd':'90','status':'ok'}
    conn.execute('INSERT INTO tvl_positions VALUES (1,1,?,?,?,?,?)',(PARENT,HOLDER,CHILD,DAY,json.dumps(p)))
    conn.commit()
    dataset=TvlStore(path,root).get()
    assert dataset.summary()['totalTvl']==220
    assert dataset.summary(10)['totalTvl']==120
    g=dataset.graph(10)
    assert g['totalTvlUsd']==120 and g['totalOverlapTvlUsd']==80
    assert {n['chainId'] for n in g['nodes']}=={1,10}
    assert next(e for e in g['edges'] if e['kind']=='registry')['targetNodeId']==f'10:{CHILD}'
    # A later selected receiver observation at another block cannot reuse old ownership.
    remote['block_number']=51
    conn.execute('INSERT INTO tvl_runs VALUES (2,2,2,?,?)',('complete',json.dumps({'from':DAY,'to':DAY})))
    conn.execute('INSERT INTO tvl_snapshots VALUES (2,10,?,?,51,?)',(CHILD,DAY,json.dumps(remote)))
    conn.commit()
    revised=TvlStore(path,root).get()
    _,_,positions,diagnostics=revised.frames((DAY,))[0]
    assert positions[0]['reason']=='selected_child_block_mismatch'
    assert diagnostics['status']=='incomplete' and revised.summary(10)['overlapExcluded']==0


def test_graph_does_not_replace_unpriced_vault_with_synthetic_priced_strategy(source):
    conn,path,root=source
    run(conn,1,DAY,[(PARENT,'100'),(CHILD,None)],[(CHILD,'80')])
    g=TvlStore(path,root).get().graph()
    assert [n['address'] for n in g['nodes']]==[PARENT]
    assert g['edges']==[]


def test_stale_debt_cannot_make_graph_flows_exceed_parent_assets(source):
    conn,path,root=source
    run(conn,1,DAY,[(PARENT,'100'),(CHILD,'500')])
    router(conn,debt='80',owned='80')
    other='0x'+'66'*20
    p={'chain_id':1,'parent':PARENT,'strategy':other,'child':other,'timestamp':DAY,
       'block_number':100+DAY,'method':'strategy-allocation','debt_usd':'920','status':'ok'}
    conn.execute('INSERT INTO tvl_positions VALUES (1,1,?,?,?,?,?)',(PARENT,other,other,DAY,json.dumps(p)))
    conn.commit()
    g=TvlStore(path,root).get().graph()
    outgoing=[e for e in g['edges'] if e['sourceNodeId']==f'1:{PARENT}']
    assert sum(e['tvlUsd'] for e in outgoing)==100
    assert sum(e['recordedDebtUsd'] for e in outgoing)==1000
    assert next(e for e in g['edges'] if e['kind']=='registry')['tvlUsd']==80
    assert g['totalTvlUsd']==520


def test_verified_zero_cross_chain_debt_needs_no_remote_block(source):
    conn,path,root=source
    run(conn,1,DAY,[(PARENT,'100')])
    remote={'chain_id':10,'vault':CHILD,'timestamp':DAY,'block_number':50,'version':'v3',
            'tvl_usd':'200','status':'ok','name':'Remote vault','asset_units':'200'}
    conn.execute('INSERT INTO tvl_snapshots VALUES (1,10,?,?,50,?)',(CHILD,DAY,json.dumps(remote)))
    p={'chain_id':1,'parent':PARENT,'strategy':HOLDER,'child':CHILD,'child_chain_id':10,
       'timestamp':DAY,'block_number':100+DAY,'method':'cross-chain-shares',
       'debt_usd':'0','owned_usd':'0','status':'ok'}
    conn.execute('INSERT INTO tvl_positions VALUES (1,1,?,?,?,?,?)',(PARENT,HOLDER,CHILD,DAY,json.dumps(p)))
    conn.commit()
    _,_,positions,diagnostics=TvlStore(path,root).get().frames((DAY,))[0]
    assert positions[0]['status']=='ok' and positions[0]['overlap_usd']=='0'
    assert diagnostics['status']=='complete' and diagnostics['external_tvl_usd']=='300'
