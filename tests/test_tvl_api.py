import json
from pathlib import Path
import sqlite3

import pytest

from yearn_data.storage import connect, init_db
from yearn_data.tvl import SCHEMA
from yearn_data.tvl_api import TvlStore, publish_tvl, tvl_response

DAY = 1704067199  # Sunday UTC close, preceding Monday January 1.
PARENT = '0x'+'11'*20
CHILD = '0x'+'22'*20
BRIDGE = '0x'+'33'*20


def run(conn, number, timestamp, rows, edges=(), status='complete'):
    conn.execute('INSERT INTO tvl_runs VALUES (?,?,?,?,?)', (number,number,number,status,json.dumps({'from':timestamp,'to':timestamp})))
    for address, amount in rows:
        row = {'chain_id':1,'vault':address,'timestamp':timestamp,'block_number':100+timestamp,
               'version':'v3','category':'v3','asset':'0x'+'aa'*20,'asset_decimals':0,
               'total_assets_raw':'10','total_supply_raw':'10','asset_units':'10',
               'price_usd':str(int(amount)//10) if amount is not None else None,
               'tvl_usd':amount,'status':'ok' if amount is not None else 'unavailable','name':'Vault'}
        if address == BRIDGE:
            row['bridge_target_chain'] = 10
        conn.execute('INSERT INTO tvl_snapshots VALUES (?,?,?,?,?,?)', (number,1,address,timestamp,100+timestamp,json.dumps(row)))
    for child, owned in edges:
        row = {'chain_id':1,'parent':PARENT,'child':child,'strategy':child,'timestamp':timestamp,
               'block_number':100+timestamp,'status':'ok','method':'direct-strategy','debt_usd':owned,
               'owned_usd':owned,'debt_raw':owned,'ownership_ratio':'0.5'}
        conn.execute('INSERT INTO tvl_positions VALUES (?,?,?,?,?,?,?)', (number,1,PARENT,child,child,timestamp,json.dumps(row)))
    conn.commit()


@pytest.fixture
def source(tmp_path):
    path = tmp_path/'yearn.sqlite'
    conn = connect(path); init_db(conn); conn.executescript(SCHEMA)
    for address, active in ((PARENT,1),(CHILD,1),(BRIDGE,0)):
        conn.execute("INSERT INTO tvl_vaults (chain_id,version,address,active,updated_at) VALUES (1,'v3',?,?,1)", (address,active))
    conn.execute("INSERT INTO tvl_vaults (chain_id,version,address,active,updated_at) VALUES (10,'v3',?,1,1)",(CHILD,))
    conn.commit()
    yield conn, path, tmp_path/'publication'
    conn.close()


def test_accumulated_history_repair_and_failed_retry_preserve_known_data(source):
    conn,path,root = source
    run(conn,1,DAY,[(PARENT,'100'),(CHILD,'100')],[(CHILD,'50')])
    run(conn,2,DAY+7*86400,[(PARENT,'200'),(CHILD,'200')],[(CHILD,'100')])
    # A high-ID old repair adds to history without erasing the newer date.
    run(conn,3,DAY,[(CHILD,'120')])
    run(conn,4,DAY,[(PARENT,None)],status='incomplete')
    run(conn,5,DAY+14*86400,[(PARENT,'999')],status='running')
    store = TvlStore(path,root)
    summary = store.get().summary()
    assert summary['totalTvl'] == 300
    history = store.get().history()
    assert history['range']['to'] == DAY+7*86400
    assert len(history['chart']) == 2
    # Parent 100 + repaired child 120 - recorded overlap 50.
    assert history['chart'][0]['Ethereum'] == 170
    assert history['chart'][1]['Ethereum'] == 300


def test_auto_refresh_and_pinned_publication(source):
    conn,path,root = source
    run(conn,1,DAY,[(PARENT,'100')])
    store = TvlStore(path,root,refresh_seconds=0)
    old = store.get().id
    run(conn,2,DAY+86400,[(PARENT,'200')])
    assert store.get().id != old
    assert store.get().summary()['totalTvl'] == 200
    assert store.get(old).summary()['totalTvl'] == 100
    status, result = tvl_response(store,'/api/tvl/history/runs/latest?datasetId='+old)
    assert status == 200 and result['datasetId'] == old
    assert result['chart'][0]['Ethereum'] == 100


def test_failed_publication_retains_last_selection(source, monkeypatch):
    conn,path,root = source
    run(conn,1,DAY,[(PARENT,'100')])
    store = TvlStore(path,root,refresh_seconds=0)
    previous = (root/'current.json').read_bytes()
    replace = Path.replace
    def fail(source,target):
        if Path(target) == root/'current.json':
            raise OSError('interrupted publication')
        return replace(source,target)
    monkeypatch.setattr(Path,'replace',fail)
    assert store.get().summary()['totalTvl'] == 100
    assert (root/'current.json').read_bytes() == previous
    assert store.last_error == 'interrupted publication'


def test_weekly_stock_values_and_constant_price_timeframe(source):
    conn,path,root = source
    run(conn,1,DAY,[(PARENT,'100'),(CHILD,'100')],[(CHILD,'50')])
    run(conn,2,DAY+86400,[(PARENT,'200'),(CHILD,'200')],[(CHILD,'100')])
    run(conn,3,DAY+2*86400,[(PARENT,'300'),(CHILD,'300')],[(CHILD,'150')])
    dataset = TvlStore(path,root).get()
    # Weekly TVL is the last stock observation, not a sum of daily values.
    assert [r['Ethereum'] for r in dataset.history()['chart']] == [150,450]
    fixed = dataset.history(start=DAY+86400,constant=True)
    assert len(fixed['chart']) == 1
    assert fixed['actualChart'][0]['Ethereum'] == 450
    assert fixed['constantPriceChart'][0]['Ethereum'] == 300
    assert all(r['timestamp'] == DAY+86400 for r in fixed['references'])
    assert dataset.history(start=DAY+3*86400)['chart'] == []


def test_current_bridge_policy_not_retroactive_and_chain_filter_consistent(source):
    conn,path,root = source
    run(conn,1,DAY,[(PARENT,'100'),(BRIDGE,'50')])
    run(conn,2,DAY+86400,[(PARENT,'100'),(BRIDGE,'50')])
    dataset = TvlStore(path,root,current_bridge_policy='retired-registry').get()
    assert dataset.summary()['totalTvl'] == 100
    assert dataset.summary(1)['totalTvl'] == 100
    assert dataset.summary()['vaultBridgeExcluded'] == 50
    assert dataset.history(interval='daily')['chart'][0]['Ethereum'] == 150
    assert dataset.history(interval='daily')['chart'][1]['Ethereum'] == 100
    assert dataset.audit()['crossChainOverlap'] == 50


def test_unavailable_amounts_and_invalid_filters(source):
    conn,path,root = source
    run(conn,1,DAY,[(PARENT,None)])
    store = TvlStore(path,root)
    assert store.get().summary()['totalTvl'] is None
    assert store.get().history()['chart'] == [{'timestamp':DAY}]
    for query in ('chainId=999','from=x','from=10&to=1','interval=monthly','mode=unknown','groupBy=unknown','chainId=1&chainId=1','includeCurrent=maybe'):
        assert tvl_response(store,'/api/tvl/history/runs/latest?'+query)[0] == 400
    assert tvl_response(store,'/api/tvl/no-such-route')[0] == 404


def test_rpc_failure_holds_common_close_until_chain_repair(source):
    conn, path, root = source
    def add_other_chain(number, timestamp, amount, observed=True):
        run(conn, number, timestamp, [])
        row = {'chain_id':10,'vault':CHILD,'timestamp':timestamp,'block_number':timestamp,
               'version':'v3','category':'v3','asset_units':'10' if observed else None,
               'tvl_usd':amount,'total_supply_raw':'10','total_assets_raw':'10'}
        conn.execute('INSERT INTO tvl_snapshots VALUES (?,?,?,?,?,?)',
                     (number,10,CHILD,timestamp,timestamp,json.dumps(row)))
        conn.commit()
    run(conn, 1, DAY, [(PARENT,'100')])
    add_other_chain(2, DAY, '100')
    run(conn, 3, DAY+86400, [(PARENT,'200')])
    add_other_chain(4, DAY+86400, None, observed=False)
    store = TvlStore(path, root, refresh_seconds=0)
    assert store.get().summary()['asOfTimestamp'] == DAY
    assert store.get().summary()['totalTvl'] == 200
    # A healthy finalized RPC repairs only the failed chain at the same close.
    add_other_chain(5, DAY+86400, '150')
    assert store.get().summary()['asOfTimestamp'] == DAY+86400
    assert store.get().summary()['totalTvl'] == 350


def test_curation_and_audit_require_positive_allocator_evidence(source):
    conn, path, root = source
    run(conn,1,DAY,[(PARENT,'100'),(CHILD,'100')],[(CHILD,'50')])
    conn.execute("INSERT INTO strategy_reports (chain_id,vault_address,strategy_address,tx_hash,log_index,block_number,block_timestamp,version,gain_raw,loss_raw,net_raw,current_debt_raw) VALUES (1,?,?,?,0,1,?,'v3','0','0','0','0')",(PARENT,CHILD,'0x01',DAY))
    conn.commit()
    dataset = TvlStore(path,root).get()
    products = dataset.curation()
    assert products['vaultCount'] == 1
    assert products['byFamily']['v3_allocator']['grossTvlUsd'] == 100
    assert products['byFamily']['morpho_curated']['vaultCount'] == 0
    audit = {v['address']:v for v in dataset.audit()['vaults']}
    assert audit[PARENT]['vaultType'] == 1
    assert audit[CHILD]['vaultType'] is None
    assert audit[PARENT]['strategies'][0]['debtUsd'] == 50


def test_comparison_uses_native_totals_only(source, tmp_path):
    conn, path, root = source
    run(conn,1,DAY,[(PARENT,'100'),(CHILD,'100')],[(CHILD,'50')])
    reference_path = tmp_path/'external.sqlite'
    with sqlite3.connect(reference_path) as reference:
        reference.execute('CREATE TABLE defillama_snapshots (id INTEGER,protocol TEXT,chain TEXT,tvl_usd REAL,timestamp INTEGER)')
        reference.executemany('INSERT INTO defillama_snapshots VALUES (?,?,?,?,?)',
                             [(1,'yearn-finance',None,120,DAY),(2,'yearn-finance','Ethereum',120,DAY),
                              (3,'yearn-curating',None,10,DAY),(4,'yearn-curating','Ethereum',10,DAY)])
    result = TvlStore(path,root).get().comparison(reference_path)
    assert result['ourTotal'] == 150
    assert result['defillamaTotal'] == 130
    assert result['difference'] == 20
    assert result['byChain'] == [{'chain':'Ethereum','ours':150,'defillama':130,'difference':20}]


def test_rejected_provider_quote_is_not_served_or_used_as_reference(source, monkeypatch):
    import yearn_data.tvl_api as api
    conn, path, root = source
    asset = '0x'+'aa'*20
    monkeypatch.setattr(api, 'PRICE_REJECTIONS', [{'chainId':1,'asset':asset,'timestamp':DAY,
                                               'priceUsd':'10','reason':'verified_bad_quote'}])
    run(conn,1,DAY,[(PARENT,'100')])
    run(conn,2,DAY+86400,[(PARENT,'120')])
    store = TvlStore(path,root,refresh_seconds=0)
    result = store.get().history(interval='daily',constant=True)
    assert result['actualChart'][0] == {'timestamp':DAY}
    assert result['references'][0]['timestamp'] == DAY+86400
    # A corrected canonical quote at the same identity is accepted automatically.
    run(conn,3,DAY,[(PARENT,'130')])
    repaired = store.get().history(interval='daily',constant=True)
    assert repaired['actualChart'][0]['Ethereum'] == 130
    assert repaired['references'][0]['timestamp'] == DAY


def test_version_and_constant_price_share_accounting_and_all_time_bounds(source, monkeypatch):
    conn,path,root = source
    run(conn,1,DAY,[(PARENT,'100'),(CHILD,'100')],[(CHILD,'50')])
    run(conn,2,DAY+86400,[(PARENT,'200'),(CHILD,'200')],[(CHILD,'100')])
    dataset = TvlStore(path,root).get()
    chain = dataset.history(constant=True)
    def unexpected_scan(*args, **kwargs):
        pytest.fail('changing chart grouping rescanned stored source data')
    monkeypatch.setattr(dataset,'iter_frames',unexpected_scan)
    monkeypatch.setattr(dataset,'reference_candidates',unexpected_scan)
    # Explicit full bounds are the same cache identity as omitted bounds.
    assert dataset.history(start=DAY,end=DAY+86400,constant=True) is chain
    versions = dataset.history(group='category',constant=True)
    assert versions['actualChart'][0]['v3'] == chain['actualChart'][0]['Ethereum']
    assert versions['constantPriceChart'][1]['v3'] == chain['constantPriceChart'][1]['Ethereum']


def test_concurrent_group_requests_share_work_without_blocking_summary(source, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    conn,path,root = source
    run(conn,1,DAY,[(PARENT,'100')])
    dataset = TvlStore(path,root).get()
    entered, release, joined = Event(), Event(), Event()
    original = dataset._history_rows
    count = []
    def slow_rows(*args):
        count.append(1)
        entered.set()
        assert release.wait(5)
        return original(*args)
    monkeypatch.setattr(dataset,'_history_rows',slow_rows)
    original_once = dataset.compute_once
    def notice_join(identity,compute):
        if identity in dataset._pending:
            joined.set()
        return original_once(identity,compute)
    monkeypatch.setattr(dataset,'compute_once',notice_join)
    with ThreadPoolExecutor(max_workers=3) as pool:
        chain = pool.submit(dataset.history)
        assert entered.wait(5)
        versions = pool.submit(dataset.history,group='category')
        assert joined.wait(5)
        assert pool.submit(dataset.summary).result(timeout=2)['totalTvl'] == 100
        release.set()
        assert chain.result(timeout=5)['chart'][0]['Ethereum'] == 100
        assert versions.result(timeout=5)['chart'][0]['v3'] == 100
    assert len(count) == 1


def test_long_histories_keep_streaming_instead_of_filling_shared_cache(source, monkeypatch):
    import yearn_data.tvl_api as api
    conn,path,root = source
    run(conn,1,DAY,[(PARENT,'100')])
    run(conn,2,DAY+86400,[(PARENT,'200')])
    dataset = TvlStore(path,root).get()
    monkeypatch.setattr(api,'HISTORY_CACHE_MAX_DATES',1)
    def oversized_cache(*args, **kwargs):
        pytest.fail('long history was retained in the shared row cache')
    monkeypatch.setattr(dataset,'history_rows',oversized_cache)
    assert [r['Ethereum'] for r in dataset.history(interval='daily')['chart']] == [100,200]


def test_reference_skips_a_full_window_of_rejected_prices(source, monkeypatch):
    import yearn_data.tvl_api as api
    conn,path,root = source
    asset='0x'+'aa'*20
    rejected=[]
    for index in range(8):
        timestamp=DAY+index*86400
        run(conn,index+1,timestamp,[(PARENT,'720000000')])
        rejected.append({'chainId':1,'asset':asset,'timestamp':timestamp,
                         'priceUsd':'72000000','reason':'verified_bad_quote'})
    run(conn,9,DAY+8*86400,[(PARENT,'10')])
    monkeypatch.setattr(api,'PRICE_REJECTIONS',rejected)
    store=TvlStore(path,root)
    result=store.get().history(interval='daily',constant=True)
    assert result['references'] == [{'vault':'1:'+PARENT,'timestamp':DAY+8*86400,
                                     'priceUsd':1,'source':'stored-tvl','depegCandidateSkipped':False}]
    assert result['constantPriceChart'][-1]['Ethereum'] == 10
    assert all(row == {'timestamp':DAY+index*86400} for index,row in enumerate(result['actualChart'][:-1]))


def test_dated_bridge_cutoff_preserves_pre_migration_and_pinned_selection(source, monkeypatch):
    import yearn_data.tvl_api as api
    conn,path,root = source
    cutoff=DAY+86400
    monkeypatch.setattr(api,'BRIDGE_MIGRATIONS',[{'sourceChainId':1,'sourceVaultAddress':BRIDGE,
                                               'targetChainId':10,'migrationTimestamp':cutoff}])
    run(conn,1,DAY,[(PARENT,'100'),(BRIDGE,'50')])
    store=TvlStore(path,root,current_bridge_policy='retired-registry',refresh_seconds=0)
    old=store.get().id
    # Today's inactive flag must not exclude a dated vault before its migration.
    assert store.get().summary()['totalTvl'] == 150
    run(conn,2,cutoff,[(PARENT,'100'),(BRIDGE,'50')])
    run(conn,3,cutoff+86400,[(PARENT,'100'),(BRIDGE,'50')])
    dataset=store.get()
    assert dataset.id != old
    assert store.get(old).summary()['totalTvl'] == 150
    history=dataset.history(interval='daily',constant=True)
    assert [r['Ethereum'] for r in history['actualChart']] == [150,100,100]
    assert [r['Ethereum'] for r in history['constantPriceChart']] == [150,100,100]
    assert [r['Ethereum'] for r in dataset.history(mode='raw',interval='daily')['chart']] == [150,150,150]
    assert dataset.summary()['vaultBridgeExcluded'] == 50
    assert dataset.summary(1)['totalTvl'] == 100
    assert [r['Ethereum'] for r in dataset.history(chain_id=1,interval='daily')['chart']] == [150,100,100]


def test_excluded_bridge_parent_does_not_also_deduct_child_holdings(source, monkeypatch):
    import yearn_data.tvl_api as api
    conn,path,root = source
    monkeypatch.setattr(api,'BRIDGE_MIGRATIONS',[{'sourceChainId':1,'sourceVaultAddress':BRIDGE,
                                               'targetChainId':10,'migrationTimestamp':DAY+86400}])
    # Dated evidence remains authoritative if the live catalogue says active.
    conn.execute('UPDATE tvl_vaults SET active=1 WHERE address=?',(BRIDGE,));conn.commit()
    for number,timestamp in ((1,DAY),(2,DAY+86400)):
        run(conn,number,timestamp,[(BRIDGE,'100'),(CHILD,'100')],[(CHILD,'50')])
        row=conn.execute('SELECT data_json FROM tvl_positions WHERE run_id=?',(number,)).fetchone()
        edge=json.loads(row[0]);edge['parent']=BRIDGE
        conn.execute('UPDATE tvl_positions SET parent=?,data_json=? WHERE run_id=?',(BRIDGE,json.dumps(edge),number));conn.commit()
    dataset=TvlStore(path,root).get()
    assert [r['Ethereum'] for r in dataset.history(interval='daily')['chart']] == [150,100]
    assert dataset.summary()['overlapExcluded'] == 0
    assert dataset.summary()['vaultBridgeExcluded'] == 100
    _,_,positions,diagnostics=dataset.frames((DAY+86400,))[0]
    assert diagnostics['known_external_tvl_usd'] == '100'
    assert diagnostics['known_bridge_excluded_usd'] == '100'
    assert diagnostics['external_tvl_usd'] == '100'
    assert positions[0]['accounting_excluded'] is True
    assert positions[0]['overlap_usd'] == '0'


def test_chain_drilldown_reuses_all_chain_valuations_and_references(source, monkeypatch):
    conn,path,root = source
    run(conn,1,DAY,[(PARENT,'100'),(CHILD,'100')],[(CHILD,'50')])
    # Include a second chain with the same vault address to exercise chain identity.
    other={'chain_id':10,'vault':CHILD,'timestamp':DAY,'block_number':100+DAY,
           'version':'v3','category':'v3','asset_units':'10','tvl_usd':'300'}
    conn.execute('INSERT INTO tvl_snapshots VALUES (?,?,?,?,?,?)',(1,10,CHILD,DAY,100+DAY,json.dumps(other)));conn.commit()
    dataset=TvlStore(path,root).get()
    main=dataset.history(constant=True)
    def unexpected_scan(*args,**kwargs):
        pytest.fail('chain drilldown rescanned stored source observations')
    monkeypatch.setattr(dataset,'iter_frames',unexpected_scan)
    monkeypatch.setattr(dataset,'reference_candidates',unexpected_scan)
    drilldown=dataset.history(group='vault',chain_id=1,constant=True)
    assert sum(v for k,v in drilldown['actualChart'][0].items() if k!='timestamp') == main['actualChart'][0]['Ethereum']
    assert sum(v for k,v in drilldown['constantPriceChart'][0].items() if k!='timestamp') == main['constantPriceChart'][0]['Ethereum']
    assert {r['vault'] for r in drilldown['references']} == {'1:'+PARENT,'1:'+CHILD}
    optimism=dataset.history(group='vault',chain_id=10,constant=True)
    assert sum(v for k,v in optimism['actualChart'][0].items() if k!='timestamp') == main['actualChart'][0]['Optimism']
    assert len(optimism['references']) == 1
    assert optimism['references'][0]['vault'] == '10:'+CHILD
