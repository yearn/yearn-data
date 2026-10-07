import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from yearn_data.cli import main
from yearn_data.coverage import Scope,commit_range,missing_ranges
from yearn_data.shared_db import TVL_TABLES,merge_databases,merge_plan,read_database
from yearn_data.storage import connect,init_db
from yearn_data.tvl_discovery import store_candidate


def address(n):return '0x'+f'{n:040x}'


def inputs(tmp_path,*,legacy=False):
    fees_path=tmp_path/'fees.sqlite';tvl_path=tmp_path/'tvl.sqlite'
    fees=connect(fees_path);init_db(fees)
    fees.execute("INSERT INTO vaults(chain_id,version,address,asset,asset_decimals,updated_at) VALUES (1,'v3',?,?,6,1)",(address(1),address(90)))
    fees.execute("INSERT INTO prices(chain_id,token_address,timestamp,source,price_usd,status) VALUES (1,?,86399,'yearn-prices',1,'ok')",(address(90),))
    fees.execute("INSERT INTO analysis_runs(id,name,started_at,status) VALUES (13,'lifetime-yield',1,'complete')")
    fees.execute('INSERT INTO analysis_outputs VALUES (13,?,?)',('reports',json.dumps({'quantity':str(2**200),'net_yield_usd':'123.4567890123456789'})))
    fees.commit();fees.close()
    tvl=connect(tvl_path);init_db(tvl)
    if legacy:
        for old,new in TVL_TABLES:
            if old!=new:tvl.execute('DROP TABLE '+new)
        tvl.execute('ALTER TABLE vaults ADD COLUMN tvl_category TEXT')
    catalog='vaults' if legacy else 'tvl_vaults'
    prices='prices' if legacy else 'tvl_prices'
    tvl.execute(f"INSERT INTO {catalog}(chain_id,version,address,asset,asset_decimals,updated_at,tvl_category) VALUES (1,'v1',?,?,18,1,'v1')",(address(1),address(90)))
    tvl.execute(f"INSERT INTO {prices}(chain_id,token_address,timestamp,source,price_usd,status) VALUES (1,?,86399,'yearn-prices',NULL,'missing')",(address(90),))
    tvl.execute("INSERT INTO tvl_runs(id,started_at,status,params_json) VALUES (9,1,'incomplete','{}')")
    snapshot=json.dumps({'chain_id':1,'vault':address(1),'timestamp':86399,'tvl_usd':None,'status':'missing','total_assets_raw':str(2**200)})
    tvl.execute('INSERT INTO tvl_snapshots VALUES (9,1,?,86399,123,?)',(address(1),snapshot))
    tvl.execute('UPDATE tvl_snapshots SET rowid=-1')
    tvl.execute("INSERT INTO tvl_positions VALUES (9,1,?,?,?,86399,?)",(address(1),address(9),address(2),'{"debt_usd":null,"status":"unavailable"}'))
    tvl.execute("INSERT INTO tvl_backfill_batches VALUES (1,86399,86399,9,'incomplete')")
    tvl.execute("INSERT INTO tvl_runs(id,started_at,status,params_json) VALUES (50,1,'failed','{}')")
    tvl.execute('DELETE FROM tvl_runs WHERE id=50');tvl.commit();tvl.close()
    return fees_path,tvl_path,snapshot


@pytest.mark.parametrize('legacy',[False,True])
def test_merge_preserves_both_domains_and_saved_run_identity(tmp_path,legacy):
    fees,tvl,snapshot=inputs(tmp_path,legacy=legacy)
    before=[hashlib.sha256(p.read_bytes()).hexdigest() for p in (fees,tvl)]
    out=tmp_path/'shared.sqlite'
    result=merge_databases(fees,tvl,out,chunk_rows=1,reserve_gib=0)
    assert result['integrity']==result['foreign_keys']=='ok'
    conn=connect(out)
    assert conn.execute('SELECT version,asset_decimals FROM vaults').fetchone()[:]==('v3',6)
    assert conn.execute('SELECT version,asset_decimals FROM tvl_vaults').fetchone()[:]==('v1',18)
    assert conn.execute('SELECT price_usd,status FROM prices').fetchone()[:]==(1,'ok')
    assert conn.execute('SELECT price_usd,status FROM tvl_prices').fetchone()[:]==(None,'missing')
    assert conn.execute('SELECT data_json FROM tvl_snapshots WHERE run_id=9').fetchone()[0]==snapshot
    assert conn.execute('SELECT run_id,status FROM tvl_backfill_batches').fetchone()[:]==(9,'incomplete')
    assert conn.execute('SELECT row_json FROM analysis_outputs WHERE run_id=13').fetchone()[0]==json.dumps({'quantity':str(2**200),'net_yield_usd':'123.4567890123456789'})
    # Deleted high IDs stay reserved, so future runs cannot reuse them.
    run=conn.execute("INSERT INTO tvl_runs(started_at,status,params_json) VALUES (2,'running','{}')").lastrowid
    assert run==51
    store_candidate(conn,1,address(1),'morpho-v2','curation','fixture',metadata={'asset':address(91),'asset_decimals':8},status='ok')
    assert conn.execute('SELECT version,asset_decimals FROM vaults').fetchone()[:]==('v3',6)
    assert conn.execute('SELECT version,asset_decimals FROM tvl_vaults').fetchone()[:]==('morpho-v2',8)
    conn.close()
    assert before==[hashlib.sha256(p.read_bytes()).hexdigest() for p in (fees,tvl)]


def test_disk_failure_creates_no_output_or_partial_copy(tmp_path,monkeypatch):
    fees,tvl,_=inputs(tmp_path);out=tmp_path/'shared.sqlite'
    monkeypatch.setattr('yearn_data.shared_db.shutil.disk_usage',lambda _:SimpleNamespace(free=1))
    assert not merge_plan(fees,tvl,out)['space_sufficient']
    with pytest.raises(ValueError,match='insufficient disk'):merge_databases(fees,tvl,out)
    assert not out.exists() and not out.with_name(out.name+'.partial').exists()


def test_interrupted_merge_keeps_inputs_and_never_publishes(tmp_path):
    fees,tvl,_=inputs(tmp_path);out=tmp_path/'shared.sqlite'
    before=[p.read_bytes() for p in (fees,tvl)]
    def interrupt(message):
        if message.startswith('tvl_snapshots:'):raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):merge_databases(fees,tvl,out,reserve_gib=0,progress=interrupt)
    assert not out.exists() and not out.with_name(out.name+'.partial').exists()
    assert before==[p.read_bytes() for p in (fees,tvl)]


def test_merge_refuses_existing_output_and_existing_tvl_runs(tmp_path):
    fees,tvl,_=inputs(tmp_path);out=tmp_path/'shared.sqlite'
    out.write_text('keep this file')
    with pytest.raises(ValueError,match='already exists'):merge_databases(fees,tvl,out)
    assert out.read_text()=='keep this file'
    conn=connect(fees)
    conn.execute("INSERT INTO tvl_runs(started_at,status,params_json) VALUES (1,'complete','{}')");conn.commit();conn.close()
    with pytest.raises(ValueError,match='already contains tvl_runs'):merge_plan(fees,tvl,tmp_path/'other.sqlite')


def test_tvl_scan_coverage_cannot_certify_fee_scan_coverage(tmp_path):
    conn=connect(tmp_path/'shared.sqlite');init_db(conn)
    scope=Scope(1,address(1),'inventory:tvl-curation')
    commit_range(conn,scope,0,10,source='rpc',end_hash='abc',run_id=None,write=lambda c:0,table='tvl_history_coverage')
    assert list(missing_ranges(conn,scope,0,10,20,table='tvl_history_coverage'))==[]
    assert list(missing_ranges(conn,scope,0,10,20))==[(0,10)]
    conn.close()


def test_cli_preflight_does_not_initialize_a_default_database(tmp_path,monkeypatch,capsys):
    fees,tvl,_=inputs(tmp_path);monkeypatch.chdir(tmp_path)
    assert main(['merge-databases','--fees-db',str(fees),'--tvl-db',str(tvl),'--out','shared.sqlite','--check-only'])==0
    plan=json.loads(capsys.readouterr().out)
    assert plan['space_sufficient'] and not Path('shared.sqlite').exists() and not Path('data').exists()



def test_empty_namespace_created_by_init_does_not_hide_legacy_catalog(tmp_path):
    fees,tvl,_=inputs(tmp_path,legacy=True)
    conn=connect(tvl);init_db(conn);conn.close()
    out=tmp_path/'shared.sqlite'
    merge_databases(fees,tvl,out,reserve_gib=0)
    conn=read_database(out)
    assert conn.execute('SELECT version,asset_decimals FROM tvl_vaults').fetchone()[:]==('v1',18)
    assert conn.execute('SELECT price_usd,status FROM tvl_prices').fetchone()[:]==(None,'missing')
    conn.close()


def test_disk_reserve_reached_during_copy_removes_only_staging(tmp_path,monkeypatch):
    fees,tvl,_=inputs(tmp_path);out=tmp_path/'shared.sqlite';calls=0
    before=[p.read_bytes() for p in (fees,tvl)]
    def space(path):
        nonlocal calls
        calls+=1
        return SimpleNamespace(free=10**12 if calls==1 else 0)
    monkeypatch.setattr('yearn_data.shared_db.shutil.disk_usage',space)
    with pytest.raises(ValueError,match='reserve reached'):merge_databases(fees,tvl,out,reserve_gib=1)
    assert not out.exists() and not list(tmp_path.glob('shared.sqlite.partial*'))
    assert before==[p.read_bytes() for p in (fees,tvl)]
