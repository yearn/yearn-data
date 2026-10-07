import importlib.util
import json
from types import SimpleNamespace

import pytest
from web3 import Web3
from yearn_data.storage import connect,init_db
from yearn_data.tvl_discovery import morpho_api_candidates,discover_catalog
from yearn_data.tvl_sources import refresh_kong_catalog,collect_tvl


def address(n):return Web3.to_checksum_address('0x'+f'{n:040x}')


@pytest.fixture
def db(tmp_path):
    c=connect(tmp_path/'catalog.sqlite');init_db(c)
    yield c
    c.close()


def test_kong_default_discovery_ignores_excluded_chains(db,monkeypatch):
    rows=[{'address':address(n),'chainId':chain,'v3':True,'name':'Vault','apiVersion':'3.0.4',
           'asset':{'address':address(90),'decimals':18,'symbol':'UNIT'},'strategies':[]}
          for n,chain in [(1,1),(2,146),(3,80094)]]
    response=SimpleNamespace(raise_for_status=lambda:None,json=lambda:{'data':{'vaults':rows}})
    monkeypatch.setattr('yearn_data.tvl_sources.requests.post',lambda *_a,**_k:response)
    result=refresh_kong_catalog(db,with_summary=True)
    assert result['discovered']==1 and result['status']=='complete'
    assert [r['chain_id'] for r in db.execute('SELECT chain_id FROM vaults')]==[1]


def test_morpho_discovery_ignores_excluded_api_results(monkeypatch):
    monkeypatch.setattr('yearn_data.tvl_discovery.CURATION_REGISTRY',{'owners':[{'address':address(50),'chain_ids':None}]})
    def request(query,variables):
        collection='vaultV2s' if 'vaultV2s' in query else 'vaults'
        rows=[] if collection=='vaultV2s' else [{'address':address(n),'chain':{'id':chain}} for n,chain in [(1,1),(2,146),(3,80094)]]
        return {collection:{'items':rows}}
    rows,failures=morpho_api_candidates(request=request)
    assert failures==[] and [r['chain_id'] for r in rows]==[1]
    with pytest.raises(ValueError,match='excluded'):
        discover_catalog(None,chain_ids=[146])


def test_collection_skips_retained_excluded_catalog_rows(db,monkeypatch):
    for chain in [1,146,80094]:
        db.execute('INSERT INTO vaults(chain_id,version,address,asset,asset_decimals,updated_at) VALUES (?,?,?,?,?,?)',
                   (chain,'v3',address(1),address(90),18,1))
    db.commit()
    class Reader:
        def __init__(self,chain):assert chain==1
        def block_at(self,timestamp):return 100
        def exists(self,vault,block):return False
    run=collect_tvl(db,86399,86399,reader_factory=Reader)
    assert [r[0] for r in db.execute('SELECT DISTINCT chain_id FROM tvl_snapshots WHERE run_id=?',(run,))]==[1]
    assert db.execute('SELECT count(*) FROM vaults').fetchone()[0]==3


def test_backfill_does_not_report_excluded_rows_as_pending(tmp_path,monkeypatch):
    spec=importlib.util.spec_from_file_location('scope_backfill','scripts/backfill_tvl.py')
    b=importlib.util.module_from_spec(spec);spec.loader.exec_module(b)
    dbpath=tmp_path/'old.sqlite';c=connect(dbpath);init_db(c)
    for chain in [146,80094]:
        c.execute('INSERT INTO vaults(chain_id,version,address,updated_at) VALUES (?,?,?,?)',(chain,'v3',address(chain),1))
    c.commit();c.close()
    heads=tmp_path/'heads.json';heads.write_text('[]')
    env=tmp_path/'.env';env.write_text('')
    out=tmp_path/'out'
    monkeypatch.setattr(b,'load_environment',lambda _paths:None)
    monkeypatch.setattr('sys.argv',['backfill','--db',str(dbpath),'--heads',str(heads),'--env',str(env),'--out',str(out),'--to-date','2026-10-05'])
    b.main()
    state=json.loads((out/'status.json').read_text())
    assert state['pending_chains']==[] and state['scheduled_batches']==0
    assert state['excluded_chain_ids']==[146,80094]
