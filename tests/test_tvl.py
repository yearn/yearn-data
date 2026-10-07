import json
import sqlite3
from decimal import Decimal

import pytest

from yearn_data.storage import connect, init_db
from yearn_data.tvl import account, asset_units, export_tvl
from yearn_data.tvl_sources import ArchiveReader, collect_tvl, import_service_catalog, scan_holders, candidate_edges
from yearn_data.cli import main


def address(n):
    return '0x'+f'{n:040x}'


def snapshot(n,value,version='v3',chain=1):
    return {'chain_id':chain,'vault':address(n),'timestamp':100,'tvl_usd':value,'version':version}


def edge(parent,strategy,child,debt,owned,chain=1):
    return {'chain_id':chain,'parent':address(parent),'strategy':address(strategy),'child':address(child),
            'timestamp':100,'debt_usd':debt,'owned_usd':owned,'status':'ok'}


def test_three_level_nesting_counts_capital_once():
    rows,edges,total=account([snapshot(i,'100') for i in (1,2,3)],
                            [edge(1,2,2,'100','100'),edge(2,3,3,'100','100')])
    assert total['gross_tvl_usd']=='300'
    assert total['overlap_usd']=='200'
    assert total['external_tvl_usd']=='100'
    assert [r['external_tvl_usd'] for r in rows]==['100','0','0']


def test_shared_holder_and_multiple_children_share_debt_budget():
    points=[snapshot(1,'100'),snapshot(2,'300'),snapshot(3,'500'),snapshot(4,'500')]
    positions=[edge(1,9,3,'100','400'),edge(2,9,3,'300','400'),
               edge(1,9,4,'100','400'),edge(2,9,4,'300','400')]
    _,edges,total=account(points,positions)
    assert [Decimal(e['overlap_usd']) for e in edges]==[50,150,50,150]
    assert Decimal(total['overlap_usd'])==400


def test_incoming_and_parent_tvl_caps():
    points=[snapshot(1,'50'),snapshot(2,'100'),snapshot(3,'100')]
    _,edges,total=account(points,[edge(1,9,3,'200','200'),edge(2,10,3,'200','200')])
    assert sum(Decimal(e['overlap_usd']) for e in edges)<=100
    assert Decimal(edges[0]['overlap_usd'])<=50
    assert Decimal(total['external_tvl_usd'])>=0


def test_curation_is_an_explicit_accounting_choice():
    points=[snapshot(1,'100'),snapshot(2,'100','curation')]
    assert account(points,[edge(1,9,2,'100','100')])[2]['external_tvl_usd']=='200'
    assert account(points,[edge(1,9,2,'100','100')],include_curation=True)[2]['external_tvl_usd']=='100'


def test_missing_values_are_not_zero_or_complete_totals():
    points=[snapshot(1,'100'),snapshot(2,None)]
    total=account(points,[edge(1,9,2,'100',None)])[2]
    assert total['external_tvl_usd'] is None
    assert total['known_gross_tvl_usd']=='100'
    assert total['status']=='incomplete'
    zero=account([snapshot(1,'0')],[])[2]
    assert zero['external_tvl_usd']=='0' and zero['status']=='complete'


def test_cross_chain_same_address_is_not_an_overlap():
    total=account([snapshot(1,'100',chain=1),snapshot(2,'100',chain=10)], [edge(1,9,2,'100','100')])[2]
    assert total['external_tvl_usd']=='200'


def test_raw_quantity_keeps_integer_precision_and_requires_decimals():
    assert asset_units('123456789123456789123456789123456789',18)==Decimal('123456789123456789.123456789123456789')
    with pytest.raises(ValueError):
        asset_units('1',None)


def test_versioned_debt_layouts():
    reader=object.__new__(ArchiveReader)
    reader.words=lambda *args: [10,11,12,13]
    assert reader.debt(address(1),address(2),'v3',1)==12
    reader.words=lambda *args: list(range(9))
    assert reader.debt(address(1),address(2),'v2',1)==6
    with pytest.raises(ValueError):
        reader.debt(address(1),address(2),'v3',1)


@pytest.fixture
def db(tmp_path):
    conn=connect(tmp_path/'test.sqlite')
    init_db(conn)
    for n,version in [(1,'v3'),(2,'v3'),(3,'v1'),(4,'curation')]:
        conn.execute('''INSERT INTO vaults(chain_id,version,address,asset,asset_decimals,updated_at,active)
                        VALUES (1,?,?,?,?,1,0)''',(version,address(n),address(8),6))
    conn.execute('INSERT INTO tvl_strategies VALUES (1,?,?,?)',(address(1),address(2),'fixture'))
    conn.commit()
    yield conn
    conn.close()


class Reader:
    def __init__(self,chain): pass
    def block_at(self,timestamp): return timestamp//10
    def exists(self,address,block): return block>=10
    def uint(self,addr,signature,block,arg=None):
        return {'totalSupply()':100000000,'totalAssets()':100000000,
                'getPricePerFullShare()':10**18,'balanceOf(address)':20000000}[signature]
    def debt(self,parent,strategy,version,block): return 60000000


def test_collection_retired_v1_curation_historical_debt_and_export(db,tmp_path,monkeypatch):
    monkeypatch.setattr('yearn_data.tvl_sources.fetch_defillama_price',lambda *a:(1,'ok',{'fixture':True}))
    run=collect_tvl(db,90,100,interval=10,reader_factory=Reader,price_source='defillama')
    assert db.execute('SELECT COUNT(*) FROM tvl_snapshots').fetchone()[0]==8
    export_tvl(db,tmp_path/'out',run)
    data=json.loads((tmp_path/'out/tvl.json').read_text())
    assert data['history'][0]['external_tvl_usd']=='0'
    assert Decimal(data['history'][1]['external_tvl_usd'])==340
    assert {p['version'] for p in data['vaults']}=={'v1','v3','curation'}
    assert data['positions'][1]['debt_raw']=='60000000'
    assert data['positions'][1]['method']=='direct-strategy'
    assert (tmp_path/'out/history.csv').exists()


def test_missing_rpc_and_missing_price_are_retained_and_retryable(db,tmp_path,monkeypatch):
    monkeypatch.setattr('yearn_data.tvl_sources.fetch_defillama_price',lambda *a:(None,'retryable',{}))
    run=collect_tvl(db,100,100,reader_factory=Reader,price_source='defillama')
    assert db.execute('SELECT status FROM tvl_runs WHERE id=?',(run,)).fetchone()[0]=='incomplete'
    point=json.loads(db.execute('SELECT data_json FROM tvl_snapshots LIMIT 1').fetchone()[0])
    assert point['tvl_usd'] is None and point['status']=='retryable'
    monkeypatch.setattr('yearn_data.tvl_sources.fetch_defillama_price',lambda *a:(1,'ok',{}))
    repaired=collect_tvl(db,100,100,reader_factory=Reader,price_source='defillama')
    assert repaired!=run
    assert db.execute('SELECT status FROM tvl_runs WHERE id=?',(run,)).fetchone()[0]=='incomplete'
    def unavailable(chain): raise ValueError('no RPC')
    failed=collect_tvl(db,100,100,reader_factory=unavailable,price_source='defillama')
    assert db.execute('SELECT status FROM tvl_runs WHERE id=?',(failed,)).fetchone()[0]=='incomplete'


def test_share_scan_discovers_nonregistry_intermediaries(db):
    db.execute('INSERT INTO tvl_strategies VALUES (1,?,?,?)',(address(1),address(9),'fixture'))
    result=scan_holders(db,100,reader_factory=Reader)
    assert result['positive_holdings']>0 and result['failed_reads']==0
    vaults=[dict(r) for r in db.execute('SELECT * FROM vaults')]
    assert any(e['strategy']==address(9) and e['child']==address(2) for e in candidate_edges(db,vaults))


def test_migration_is_read_only_idempotent_and_includes_all_categories(tmp_path):
    path=tmp_path/'legacy.sqlite'
    source=sqlite3.connect(path)
    source.executescript('''CREATE TABLE vaults(id INTEGER,chain_id INTEGER,address TEXT,category TEXT,
        asset_address TEXT,asset_symbol TEXT,asset_decimals INTEGER,name TEXT,api_version TEXT,is_retired INTEGER);
        CREATE TABLE strategies(chain_id INTEGER,address TEXT,vault_id INTEGER,name TEXT);''')
    for n,version in [(1,'v1'),(2,'v2'),(3,'v3'),(4,'curation')]:
        source.execute('INSERT INTO vaults VALUES (?,?,?,?,?,?,?,?,?,?)',(n,999,address(n),version,address(8),'USD',6,'name',None,1))
    source.execute('INSERT INTO strategies VALUES (999,?,3,NULL)',(address(9),))
    source.commit();source.close()
    original=path.read_bytes()
    conn=connect(tmp_path/'new.sqlite');init_db(conn)
    assert import_service_catalog(conn,path)==4
    assert import_service_catalog(conn,path)==4
    assert conn.execute('SELECT COUNT(*) FROM vaults').fetchone()[0]==4
    assert path.read_bytes()==original
    assert conn.execute('SELECT COUNT(*) FROM tvl_strategies WHERE chain_id=999').fetchone()[0]==1
    conn.close()


def test_cli_export_empty_run_is_actionable(tmp_path):
    with pytest.raises(ValueError,match='no collected TVL run'):
        main(['--db',str(tmp_path/'cli.sqlite'),'tvl','export','--out',str(tmp_path/'out')])


def test_yearn_prices_requires_day_end_state(db):
    with pytest.raises(ValueError,match='UTC day-end'):
        collect_tvl(db,100,100,reader_factory=Reader)


def test_bridge_candidates_do_not_silently_rewrite_history():
    source=snapshot(1,'100')
    source['bridge_target_chain']=747474
    total=account([source,snapshot(2,'100',chain=747474)],[])[2]
    assert total['external_tvl_usd'] is None
    assert total['known_gross_tvl_usd']=='200'
    assert total['issues']==['bridge_migration_timing_unverified']


def test_vault_share_underlying_is_valued_from_dated_child(db,monkeypatch):
    db.execute('UPDATE vaults SET asset=? WHERE address=?',(address(2),address(1)))
    monkeypatch.setattr('yearn_data.tvl_sources.fetch_defillama_price',
                        lambda chain,asset,ts:(999,'ok',{}) if asset==address(2) else (1,'ok',{}))
    run=collect_tvl(db,100,100,reader_factory=Reader,price_source='defillama')
    points=[json.loads(r[0]) for r in db.execute('SELECT data_json FROM tvl_snapshots WHERE run_id=?',(run,))]
    parent=next(p for p in points if p['vault']==address(1))
    assert Decimal(parent['tvl_usd'])==100
    assert parent['pricing']['source']=='nested-share'


def test_failed_scans_are_not_zero_balances(db):
    class Broken(Reader):
        def uint(self,*args): raise RuntimeError('archive unavailable')
    result=scan_holders(db,100,reader_factory=Broken)
    assert result['failed_reads']>0 and result['positive_holdings']==0


def test_collection_exports_ordinary_strategy_debt_without_overlap(db,monkeypatch,tmp_path):
    db.execute('INSERT INTO tvl_strategies VALUES (1,?,?,?)',(address(1),address(9),'fixture'))
    monkeypatch.setattr('yearn_data.tvl_sources.fetch_defillama_price',lambda *a:(1,'ok',{}))
    run=collect_tvl(db,100,100,reader_factory=Reader,price_source='defillama')
    export_tvl(db,tmp_path/'out',run)
    data=json.loads((tmp_path/'out/tvl.json').read_text())
    allocation=next(p for p in data['positions'] if p['method']=='strategy-allocation')
    assert Decimal(allocation['debt_usd'])==60
    assert allocation['mapping_status']=='no_known_child'
    assert allocation['overlap_usd']=='0'


def test_morpho_adapter_debt_requires_historical_membership():
    reader=object.__new__(ArchiveReader)
    reader.uint=lambda addr,sig,block,arg=None: 1 if sig=='isAdapter(address)' else 123
    assert reader.debt(address(1),address(9),'curation',10)==123
    reader.uint=lambda *args: 0
    assert reader.debt(address(1),address(9),'curation',10)==0


def test_known_child_outside_sample_stays_a_known_relationship(db,monkeypatch):
    monkeypatch.setattr('yearn_data.tvl_sources.fetch_defillama_price',lambda *a:(1,'ok',{}))
    run=collect_tvl(db,100,100,addresses=[address(1)],reader_factory=Reader,price_source='defillama')
    position=json.loads(db.execute('SELECT data_json FROM tvl_positions WHERE run_id=?',(run,)).fetchone()[0])
    assert position['method']=='direct-strategy'
    assert position['mapping_status']=='child_outside_selection'
    assert Decimal(position['debt_usd'])==60


def test_compounder_child_is_a_separate_level(db,monkeypatch):
    db.execute('INSERT INTO tvl_targets VALUES (1,?,?,?)',(address(2),address(4),'fixture'))
    monkeypatch.setattr('yearn_data.tvl_sources.fetch_defillama_price',lambda *a:(1,'ok',{}))
    run=collect_tvl(db,100,100,reader_factory=Reader,price_source='defillama')
    points=[json.loads(r[0]) for r in db.execute('SELECT data_json FROM tvl_snapshots WHERE run_id=?',(run,))]
    positions=[json.loads(r[0]) for r in db.execute('SELECT data_json FROM tvl_positions WHERE run_id=?',(run,))]
    assert {(p['parent'],p['child']) for p in positions}=={(address(1),address(2)),(address(2),address(4))}
    assert Decimal(account(points,positions,include_curation=True)[2]['overlap_usd'])==80


def test_curated_product_can_have_v3_contract_semantics():
    child=snapshot(2,'100','v3')
    child['category']='curation'
    positions=[edge(1,2,2,'60','100')]
    assert Decimal(account([snapshot(1,'100'),child],positions)[2]['external_tvl_usd'])==200
    assert Decimal(account([snapshot(1,'100'),child],positions,include_curation=True)[2]['external_tvl_usd'])==140



def test_legacy_forwarder_scalar_padding():
    reader=object.__new__(ArchiveReader)
    reader.words=lambda *_args: [123]+[0]*127
    assert reader.uint('0xabc','totalAssets()',42)==123
    reader.words=lambda *_args: [123,456]+[0]*126
    with pytest.raises(ValueError,match='invalid scalar'):
        reader.uint('0xabc','totalAssets()',42)


def test_legacy_forwarder_strategy_padding():
    reader=object.__new__(ArchiveReader)
    params=[0,1,2,3,4,5,987,7,8]
    reader.words=lambda *_args: params+[0]*119
    assert reader.debt('0xparent','0xstrategy','v2',42)==987
    reader.words=lambda *_args: params+[1]+[0]*118
    with pytest.raises(ValueError,match='unsupported strategies'):
        reader.debt('0xparent','0xstrategy','v2',42)



def test_empty_v1_does_not_require_undefined_share_price():
    from yearn_data.tvl_sources import _snapshot
    class EmptyV1:
        def block_at(self,timestamp): return 123
        def exists(self,vault,block): return True
        def uint(self,vault,signature,block):
            assert signature=='totalSupply()'
            return 0
    vault={'chain_id':1,'version':'v1','address':address(1),'asset':address(8),
           'asset_decimals':18,'name':'empty V1'}
    point=_snapshot(vault,86399,EmptyV1(),{},'yearn-prices')
    assert point['total_assets_raw']=='0' and point['total_supply_raw']=='0'
    assert point['tvl_usd']=='0' and point['status']=='ok'


@pytest.mark.parametrize('ids', [(1,2,3),(3,2,1)])
@pytest.mark.parametrize('share_prices', [True,False])
def test_multilevel_wrappers_use_leaf_value_in_any_order(tmp_path,monkeypatch,ids,share_prices):
    conn=connect(tmp_path/'wrappers.sqlite');init_db(conn)
    outer,middle,leaf=map(address,ids)
    for vault,asset in [(outer,middle),(middle,leaf),(leaf,address(90))]:
        conn.execute("INSERT INTO vaults(chain_id,version,address,asset,asset_decimals,updated_at) VALUES (1,'v3',?,?,18,1)",(vault,asset))
    conn.commit()
    class FullOwner(Reader):
        def uint(self,*args):return 100*10**18
        def debt(self,*args):return 0
    def price(chain,asset,timestamp):
        if not share_prices and asset!=address(90):return None,'missing',{}
        return {middle:2,leaf:3,address(90):1}[asset],'ok',{}
    monkeypatch.setattr('yearn_data.tvl_sources.fetch_defillama_price',price)
    run=collect_tvl(conn,100,100,reader_factory=FullOwner,price_source='defillama')
    export_tvl(conn,tmp_path/'out',run)
    data=json.loads((tmp_path/'out/tvl.json').read_text())
    assert {Decimal(p['tvl_usd']) for p in data['vaults']}=={Decimal(100)}
    assert Decimal(data['history'][0]['external_tvl_usd'])==100
    assert data['history'][0]['status']=='complete'
    conn.close()


@pytest.mark.parametrize('failure', ['membership','assets','positive-debt','zero-debt'])
def test_unresolved_adapter_read_failure_cannot_export_complete(tmp_path,monkeypatch,failure):
    conn=connect(tmp_path/'adapter.sqlite');init_db(conn)
    conn.execute("INSERT INTO vaults(chain_id,version,address,asset,asset_decimals,updated_at) VALUES (1,'curation',?,?,18,1)",(address(1),address(90)))
    conn.execute('INSERT INTO tvl_strategies VALUES (1,?,?,?)',(address(1),address(9),'fixture'))
    conn.execute("INSERT INTO tvl_catalog_evidence VALUES (1,?,?,'unresolved','{}',1)",(address(9),'adapter:'+address(1)))
    conn.commit()
    class AdapterReader(Reader):
        debt=ArchiveReader.debt
        def uint(self,vault,signature,block,arg=None):
            if signature=='isAdapter(address)':
                if failure=='membership':raise ValueError('historical membership unavailable')
                return 1
            if signature=='realAssets()':
                if failure=='assets':raise RuntimeError('historical assets unavailable')
                return 0 if failure=='zero-debt' else 10*10**18
            return 100*10**18
    monkeypatch.setattr('yearn_data.tvl_sources.fetch_defillama_price',lambda *a:(1,'ok',{}))
    run=collect_tvl(conn,100,100,reader_factory=AdapterReader,price_source='defillama')
    export_tvl(conn,tmp_path/'out',run)
    data=json.loads((tmp_path/'out/tvl.json').read_text())
    summary=data['history'][0]
    if failure=='zero-debt':
        assert summary['status']=='complete' and Decimal(summary['external_tvl_usd'])==100
        assert data['positions'][0]['overlap_usd']=='0'
    else:
        assert summary['status']=='incomplete'
        assert summary['gross_tvl_usd'] is None and summary['external_tvl_usd'] is None
        assert data['positions'][0]['overlap_usd'] is None
    conn.close()


@pytest.mark.parametrize('cycle', [False,True])
def test_nested_valuation_without_a_known_leaf_stays_unavailable(tmp_path,monkeypatch,cycle):
    conn=connect(tmp_path/'unknown.sqlite');init_db(conn)
    for vault,asset in [(address(1),address(2)),(address(2),address(1) if cycle else address(90))]:
        conn.execute("INSERT INTO vaults(chain_id,version,address,asset,asset_decimals,updated_at) VALUES (1,'v3',?,?,18,1)",(vault,asset))
    conn.commit()
    class FullOwner(Reader):
        def uint(self,*args):return 100*10**18
    monkeypatch.setattr('yearn_data.tvl_sources.fetch_defillama_price',
                        lambda chain,asset,timestamp:(None,'missing',{}) if asset==address(90) else (999,'ok',{}))
    run=collect_tvl(conn,100,100,reader_factory=FullOwner,price_source='defillama')
    export_tvl(conn,tmp_path/'out',run)
    data=json.loads((tmp_path/'out/tvl.json').read_text())
    assert all(p['tvl_usd'] is None for p in data['vaults'])
    assert data['history'][0]['status']=='incomplete'
    assert data['history'][0]['external_tvl_usd'] is None
    conn.close()
