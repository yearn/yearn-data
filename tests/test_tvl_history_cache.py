import json
from contextlib import closing

import pytest

from yearn_data.storage import connect,init_db
from yearn_data.tvl_api import TvlStore,TvlDataset,chart_response
from yearn_data.tvl_history_cache import prepare,ready
from test_tvl_api import run,DAY,PARENT,CHILD,BRIDGE


def test_prepared_history_preserves_repairs_unknowns_nested_caps_and_window_prices(tmp_path,monkeypatch):
    path=tmp_path/'history.sqlite'
    with closing(connect(path)) as conn:
        init_db(conn)
        for address in (PARENT,CHILD,BRIDGE):
            conn.execute("INSERT INTO tvl_vaults(chain_id,version,address,updated_at) VALUES (1,'v3',?,1)",(address,))
        for day in range(9):
            run(conn,day+1,DAY+day*86400,[(PARENT,'100'),(CHILD,str(100+10*day)),(BRIDGE,None)],[(CHILD,'50')])
        run(conn,10,DAY,[(CHILD,'150')])
        run(conn,11,DAY,[(PARENT,None)],status='incomplete')
    dataset=TvlStore(path,tmp_path/'publication').get()
    expected=[dataset.history(group='vault',interval='daily',constant=True),
              dataset.history(group='chain',interval='daily'),
              dataset.history(group='category',start=DAY+5*86400,constant=True)]
    cached=TvlDataset(dataset.manifest)
    assert not cached.history_prepared()
    assert prepare(dataset)['status']=='prepared'
    assert ready(dataset)
    monkeypatch.setattr(cached,'iter_frames',lambda *a,**k:pytest.fail('prepared read recalculated nested positions'))
    monkeypatch.setattr(cached,'reference_candidates',lambda *a,**k:pytest.fail('prepared reference read rescanned raw JSON'))
    actual=[cached.history(group='vault',interval='daily',constant=True),
            cached.history(group='chain',interval='daily'),
            cached.history(group='category',start=DAY+5*86400,constant=True)]
    assert actual==expected
    assert cached.history(interval='daily',start=DAY+100*86400)['chart']==[]
    assert prepare(dataset)['status']=='already-prepared'
    # A new immutable publication cannot consume the previous dataset's rows.
    with closing(connect(path)) as conn:run(conn,12,DAY+9*86400,[(PARENT,'200')])
    newer=TvlStore(path,tmp_path/'publication').get()
    assert newer.id!=dataset.id and not ready(newer)


def test_interrupted_preparation_never_exposes_partial_rows(tmp_path):
    path=tmp_path/'history.sqlite'
    with closing(connect(path)) as conn:
        init_db(conn)
        run(conn,1,DAY,[(PARENT,'100')])
    dataset=TvlStore(path,tmp_path/'publication').get()
    def interrupted(message):raise RuntimeError('injected interruption')
    with pytest.raises(RuntimeError,match='interruption'):prepare(dataset,interrupted)
    assert not ready(dataset)
    with closing(connect(path,readonly=True)) as conn:
        assert conn.execute('SELECT count(*) FROM tvl_chart_rows').fetchone()[0]==0
    assert prepare(dataset)['status']=='prepared'


def test_compact_top_series_preserves_all_capital_and_neutral_total():
    original={'meta':{'referenceWindowPoints':7},'points':[{'large':'duplicate'}],
              'references':[{'vault':'debug-only'}],
              'actualChart':[{'timestamp':1,'old':500,'a':1,'b':2},{'timestamp':2,'old':0,'a':10,'b':20}],
              'constantPriceChart':[{'timestamp':1,'old':450,'a':2,'b':4},{'timestamp':2,'old':0,'a':20,'b':40}]}
    compact=chart_response(original,top=1)
    # Rank by latest value, not peak. Keep every omitted vault in the remainder.
    assert compact['meta']['topSeries']==['b','All other vaults']
    assert compact['actualChart']==[{'timestamp':1,'b':2,'All other vaults':501},
                                   {'timestamp':2,'b':20,'All other vaults':10}]
    assert compact['constantPriceChart']==[{'timestamp':1,'Price-neutral TVL':456},
                                          {'timestamp':2,'Price-neutral TVL':60}]
    assert compact['points']==compact['references']==[]
    assert original['points'] and original['references']


def test_prepared_histories_keep_chain_identity_and_window_reference_changes(tmp_path):
    path=tmp_path/'history.sqlite'
    with closing(connect(path)) as conn:
        init_db(conn)
        for day in range(8):
            run(conn,day+1,DAY+day*86400,[(PARENT,str(100+day*10))])
            point={'chain_id':10,'vault':PARENT,'timestamp':DAY+day*86400,'block_number':100,
                   'version':'v3','asset_units':'10','tvl_usd':str(300+day*10)}
            conn.execute('INSERT INTO tvl_snapshots VALUES (?,?,?,?,?,?)',(day+1,10,PARENT,point['timestamp'],100,json.dumps(point)))
            conn.commit()
    source=TvlStore(path,tmp_path/'publication').get()
    old=source.history(chain_id=10,interval='daily',constant=True,start=DAY+5*86400)
    prepare(source)
    cached=TvlDataset(source.manifest)
    assert cached.history(chain_id=10,interval='daily',constant=True,start=DAY+5*86400)==old
    assert all(r['vault']=='10:'+PARENT for r in old['references'])
