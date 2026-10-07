import importlib.util
import json
import sqlite3
from unittest.mock import Mock

import pytest

spec=importlib.util.spec_from_file_location('backfill','scripts/backfill_tvl.py')
b=importlib.util.module_from_spec(spec);spec.loader.exec_module(b)


def ts(s):return int(b.datetime.strptime(s,'%Y-%m-%d').replace(tzinfo=b.UTC).timestamp())+86399


def test_monthly_ranges_are_complete_disjoint_and_newest_first():
    ranges=list(b.monthly_ranges(ts('2020-02-27'),ts('2020-04-02')))
    assert ranges==[(ts('2020-04-01'),ts('2020-04-02')),(ts('2020-03-01'),ts('2020-03-31')),(ts('2020-02-27'),ts('2020-02-29'))]
    assert sorted(t for lo,hi in ranges for t in range(lo,hi+1,86400))==list(range(ts('2020-02-27'),ts('2020-04-02')+1,86400))


def test_exact_calldata_address_padding():
    assert b.call_data('totalSupply()')=='0x18160ddd'
    arg='0x'+'ab'*20
    assert b.call_data('balanceOf(address)',arg)=='0x70a08231'+'0'*24+'ab'*20


def test_direct_batch_matches_ids_even_when_out_of_order(monkeypatch):
    reader=object.__new__(b.BatchedReader)
    reader.w3=Mock();reader.w3.provider.endpoint_uri='https://rpc.test'
    response=Mock();response.json.return_value=[{'id':1,'result':'0x'+'00'*31+'02'},{'id':0,'result':'0x'+'00'*31+'01'}]
    post=Mock(return_value=response);monkeypatch.setattr(b.requests,'post',post)
    calls=[('0x'+'11'*20,'0x18160ddd'),('0x'+'22'*20,'0x18160ddd')]
    result=reader.transport(calls,123,False)
    assert int.from_bytes(result[calls[0]],'big')==1
    assert int.from_bytes(result[calls[1]],'big')==2
    assert post.call_args.kwargs['json'][0]['params'][1]=='0x7b'


def test_invalid_ids_cannot_become_success(monkeypatch):
    reader=object.__new__(b.BatchedReader);reader.w3=Mock()
    response=Mock();response.json.return_value=[{'id':9,'result':'0x1234'}]
    monkeypatch.setattr(b.requests,'post',Mock(return_value=response));monkeypatch.setattr(b.time,'sleep',lambda _:None)
    calls=[('0x'+'11'*20,'0x18160ddd')]
    assert isinstance(reader.transport(calls,123,False)[calls[0]],Exception)


def test_price_omissions_verified_and_persisted(monkeypatch):
    c=sqlite3.connect(':memory:');c.row_factory=sqlite3.Row
    c.execute('CREATE TABLE prices(chain_id,token_address,timestamp,block_number,source,price_usd,status,raw_json)')
    key=(1,'0x'+'11'*20,ts('2020-03-01'))
    monkeypatch.setattr(b,'fetch_yearn_prices_batch',lambda *_a,**_k:{})
    exact=Mock(return_value=(None,'missing',{'provider':'yearn-prices'}));monkeypatch.setattr(b,'fetch_yearn_price',exact)
    prices=b.Prices(c);prices.prime([key]);assert prices.get(*key)[1]=='missing'
    prices.cache.clear();assert prices.get(*key)[0] is None;assert exact.call_count==1
    assert c.execute('select status from prices').fetchone()[0]=='missing'


def test_birth_hint_checked_against_previous_block():
    r=Mock();r.head={'number':10}
    r.exists.side_effect=lambda _a,n:n>=4
    r.w3.eth.get_block.return_value={'timestamp':123}
    assert b.deployment(r,{'address':'0xabc','deployment_block':7})==(4,123)


def test_atomic_status_is_valid_json(tmp_path):
    p=tmp_path/'status.json';b.atomic_json(p,{'phase':'running','snapshots':7})
    assert json.loads(p.read_text())['snapshots']==7
    assert not p.with_suffix('.tmp').exists()


@pytest.mark.parametrize('interrupted', [False,True])
def test_restart_restores_saved_batch_exports_without_recollection(tmp_path,monkeypatch,interrupted):
    from types import SimpleNamespace
    from yearn_data.storage import connect,init_db
    db=tmp_path/'restart.sqlite';conn=connect(db);init_db(conn)
    vault='0x'+'01'*20;asset='0x'+'02'*20
    conn.execute('INSERT INTO vaults(chain_id,version,address,asset,asset_decimals,updated_at) VALUES (?,?,?,?,?,?)',
                 (1,'v3',vault,asset,18,1));conn.commit();conn.close()
    heads=tmp_path/'heads.json'
    heads.write_text(json.dumps([dict(chain_id=1,status='ready',head_block=100,head_hash='01'*32,genesis_timestamp=0)]))
    env=tmp_path/'.env';env.write_text('');out=tmp_path/'out'
    monkeypatch.setattr('sys.argv',['backfill','--db',str(db),'--heads',str(heads),'--env',str(env),'--out',str(out),
                                 '--from-date','2020-01-01','--to-date','2020-01-01'])
    monkeypatch.setattr(b,'load_environment',lambda _:None)
    class Reader:
        def __init__(self,*args):
            self.w3=SimpleNamespace(eth=SimpleNamespace(chain_id=1,get_block=lambda _:dict(number=100,hash=bytes.fromhex('01'*32))))
        def block_at(self,timestamp):return 100
        def exists(self,*args):return True
        def uint(self,*args):return 100*10**18
    class Prices:
        def __init__(self,*args):self.cache={}
        def get(self,*args):return 1,'ok',{}
    monkeypatch.setattr(b.sources,'ArchiveReader',Reader)
    monkeypatch.setattr(b,'BatchedReader',Reader)
    monkeypatch.setattr(b,'Prices',Prices)
    monkeypatch.setattr(b,'deployment',lambda *a:(1,ts('2020-01-01')-86399))
    # main temporarily installs its persisted price reader in the source module.
    monkeypatch.setattr(b.sources,'fetch_yearn_price',b.sources.fetch_yearn_price)
    collect=Mock(wraps=b.sources.collect_tvl);monkeypatch.setattr(b.sources,'collect_tvl',collect)
    real_export=b.export_tvl;attempts=[]
    def fail_once(conn,path,run):
        attempts.append(run)
        if len(attempts)==1:
            path.mkdir(parents=True,exist_ok=True)
            (path/'vaults.csv').write_text('partial output')
            if interrupted:
                real_export(conn,path,run)
                raise KeyboardInterrupt()
            raise OSError('simulated disk failure')
        return real_export(conn,path,run)
    monkeypatch.setattr(b,'export_tvl',fail_once)
    with pytest.raises(KeyboardInterrupt if interrupted else OSError):b.main()
    batch=out/'batches/1/2020-01'
    assert not (batch/'export-complete.json').exists()
    collect.reset_mock()
    b.main()
    collect.assert_not_called()
    assert len(attempts)==2 and attempts[0]==attempts[1]
    assert json.loads((batch/'export-complete.json').read_text())=={'run_id':attempts[0]}
    data=json.loads((batch/'tvl.json').read_text())
    assert data['run']['id']==attempts[0]
    assert data['history'][0]['external_tvl_usd']=='100'
    assert all((batch/name).is_file() for name in ['vaults.csv','positions.csv','history.csv','tvl.json'])
    assert (batch/'vaults.csv').read_text()!='partial output'
    assert json.loads((out/'status.json').read_text())['phase'].startswith('finished')
    # A successful saved export is skipped on the next restart.
    b.main()
    assert len(attempts)==2
    collect.assert_not_called()

    # Even with a receipt, a missing output is repaired from the same run.
    (batch/'history.csv').unlink()
    b.main()
    assert len(attempts)==3 and len(set(attempts))==1
    assert (batch/'history.csv').is_file()
    collect.assert_not_called()
