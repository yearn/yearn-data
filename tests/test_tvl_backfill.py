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
    c.execute('CREATE TABLE tvl_prices(chain_id,token_address,timestamp,block_number,source,price_usd,status,raw_json, '
              'PRIMARY KEY(chain_id,token_address,timestamp,source))')
    key=(1,'0x'+'11'*20,ts('2020-03-01'))
    monkeypatch.setattr(b,'fetch_yearn_prices_batch',lambda *_a,**_k:{})
    exact=Mock(return_value=(None,'missing',{'provider':'yearn-prices'}));monkeypatch.setattr(b,'fetch_yearn_price',exact)
    prices=b.Prices(c);prices.prime([key]);assert prices.get(*key)[1]=='missing'
    prices.cache.clear();assert prices.get(*key)[0] is None;assert exact.call_count==1
    assert c.execute('select status from tvl_prices').fetchone()[0]=='missing'


def test_birth_hint_checked_against_previous_block():
    r=Mock();r.head={'number':10}
    r.exists.side_effect=lambda _a,n:n>=4
    r.w3.eth.get_block.return_value={'timestamp':123}
    assert b.deployment(r,{'address':'0xabc','deployment_block':7})==(4,123)


@pytest.mark.parametrize('interrupted', [False,True])
def test_restart_restores_saved_batch_exports_without_recollection(tmp_path,monkeypatch,interrupted):
    from types import SimpleNamespace
    from yearn_data.storage import connect,init_db
    db=tmp_path/'restart.sqlite';conn=connect(db);init_db(conn)
    vault='0x'+'01'*20;asset='0x'+'02'*20
    conn.execute('INSERT INTO tvl_vaults(chain_id,version,address,asset,asset_decimals,updated_at) VALUES (?,?,?,?,?,?)',
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


def test_interrupted_status_publication_preserves_previous_status(tmp_path,monkeypatch):
    path=tmp_path/'status.json'
    previous={'phase':'running','snapshots':7}
    path.write_text(json.dumps(previous))
    def fail_publish(*args):raise OSError('publication interrupted')
    monkeypatch.setattr(type(path),'replace',fail_publish)
    with pytest.raises(OSError):b.atomic_json(path,{'phase':'finished','snapshots':8})
    assert json.loads(path.read_text())==previous


@pytest.mark.parametrize('multicall',[False,True])
@pytest.mark.parametrize('missing_child',[False,True])
def test_batched_collection_and_export_preserve_historical_accounting(tmp_path,monkeypatch,multicall,missing_child):
    from types import SimpleNamespace
    from decimal import Decimal
    from eth_abi import decode
    from web3 import Web3
    from yearn_data.storage import connect,init_db
    from yearn_data.tvl_sources import ArchiveReader,collect_tvl,candidate_edges
    from yearn_data.tvl import export_tvl
    def address(n):return '0x'+f'{n:040x}'
    births={address(n):(2 if n==7 else 0) for n in range(1,8)}
    signatures=['totalSupply()','totalAssets()','getPricePerFullShare()',
                'balanceOf(address)','strategies(address)','isAdapter(address)','realAssets()']
    signatures_by_selector={bytes(Web3.keccak(text=sig)[:4]):sig for sig in signatures}
    assets={address(n):v for n,v in [(2,300_000_000),(3,120_000_000),(4,150_000_000),
                                   (5,100_000_000),(6,25_000_000),(7,70_000_000)]}
    supplies={address(n):v for n,v in [(1,100_000_000),(2,200_000_000),(3,100_000_000),
                                     (4,150_000_000),(5,100_000_000),(6,50_000_000),(7,70_000_000)]}
    direct_calls=[];multicall_calls=[]
    def raw_call(vault,data,block):
        vault=vault.lower()
        data=bytes.fromhex(data.removeprefix('0x')) if isinstance(data,str) else bytes(data)
        signature=signatures_by_selector[data[:4]]
        if vault in births and block<births[vault]:raise ValueError('not deployed')
        if signature=='totalSupply()':words=[supplies[vault]]
        elif signature=='totalAssets()':
            if missing_child and vault==address(3) and block==3:raise ValueError('historical read unavailable')
            words=[assets[vault]]
        elif signature=='getPricePerFullShare()':words=[2*10**18]+[0]*127
        elif signature=='realAssets()':words=[60_000_000]
        else:
            holder=decode(['address'],data[4:])[0].lower()
            if signature=='balanceOf(address)':words=[{(address(3),address(6)):25_000_000,(address(5),address(9)):40_000_000}[(vault,holder)]]
            elif signature=='isAdapter(address)':words=[int((vault,holder)==(address(4),address(9)))]
            elif vault==address(2):words=[0,1,2,3,4,5,80_000_000,7,8]
            else:words=[1,2,10_000_000,100_000_000]
        return b''.join(n.to_bytes(32,'big') for n in words)
    def get_block(number):
        return {'number':number,'timestamp':number*43200,'hash':bytes.fromhex('01'*32)}
    def get_code(vault,block_identifier):
        if vault.lower()==b.MULTICALL3.lower():return b'code' if multicall else b''
        return b'code' if vault.lower() in births and block_identifier>=births[vault.lower()] else b''
    def eth_call(request,block_identifier):return raw_call(request['to'],request['data'],block_identifier)
    def aggregate(payload):
        def call(*,block_identifier):
            multicall_calls.append(block_identifier)
            rows=[]
            for vault,allow_failure,data in payload:
                # Exercise the real direct-call fallback, even for a valid adapter.
                if vault.lower()==address(9):rows.append((False,b''));continue
                try:rows.append((True,raw_call(vault,data,block_identifier)))
                except ValueError:rows.append((False,b''))
            return rows
        return SimpleNamespace(call=call)
    w3=SimpleNamespace(provider=SimpleNamespace(endpoint_uri='https://fixture.invalid'),eth=SimpleNamespace(
        chain_id=1,get_block=get_block,get_code=get_code,call=eth_call,
        contract=lambda **kwargs:SimpleNamespace(functions=SimpleNamespace(aggregate3=aggregate))))
    def initialize(self,chain):
        self.w3=w3;self.head=get_block(4);self.cache={};self.blocks={};self.finality_policy='rpc-finalized'
    monkeypatch.setattr(ArchiveReader,'__init__',initialize)
    def post(url,*,json,timeout):
        rows=[]
        for item in json:
            request,block=item['params'];block=int(block,16)
            direct_calls.append((request['to'].lower(),block))
            try:rows.append({'id':item['id'],'result':'0x'+eth_call(request,block).hex()})
            except ValueError:rows.append({'id':item['id'],'error':{'code':-32000}})
        return SimpleNamespace(raise_for_status=lambda:None,json=lambda:list(reversed(rows)))
    monkeypatch.setattr(b.requests,'post',post)
    class Prices:
        def prime(self,keys):pass
        def get(self,chain,token,timestamp):return (999 if token==address(3) else 1),'ok',{'fixture':True}
    prices=Prices();monkeypatch.setattr(b.sources,'fetch_yearn_price',prices.get)
    outputs=[];batched_conn=None
    for batched in [False,True]:
        conn=connect(tmp_path/('batched.sqlite' if batched else 'ordinary.sqlite'));init_db(conn)
        conn.executescript(b.SCHEMA)
        for n,family in [(1,'v1'),(2,'v2'),(3,'v3'),(4,'morpho-v2'),(5,'erc4626'),(6,'v3'),(7,'v3')]:
            conn.execute('INSERT INTO tvl_vaults(chain_id,version,address,asset,asset_decimals,updated_at,tvl_category) VALUES (1,?,?,?,?,1,?)',
                         (family,address(n),address(3) if n==6 else address(90),6,'curation' if n in (4,5) else family))
        for parent,strategy in [(2,3),(3,10),(4,9)]:
            conn.execute('INSERT INTO tvl_strategies VALUES (1,?,?,?)',(address(parent),address(strategy),'fixture'))
        conn.execute('INSERT INTO tvl_targets VALUES (1,?,?,?)',(address(9),address(5),'fixture'));conn.commit()
        if batched:
            vaults=[dict(r) for r in conn.execute('SELECT * FROM tvl_vaults')]
            reader=b.BatchedReader(1,conn,{'head_block':4,'head_hash':'01'*32},vaults,candidate_edges(conn,vaults),births,prices)
            factory=lambda chain:reader
            batched_conn=conn
        else:factory=ArchiveReader
        run=collect_tvl(conn,86399,172799,reader_factory=factory)
        out=tmp_path/('batched' if batched else 'ordinary');export_tvl(conn,out,run,include_curation=True)
        data=json.loads((out/'tvl.json').read_text())
        outputs.append({key:data[key] for key in ['vaults','positions','history']})
        if not batched:conn.close()
    assert outputs[0]==outputs[1]
    history=outputs[1]['history']
    # Day one: $900 gross less $80 strategy debt, $30 wrapper shares and
    # $40 adapter shares. Day two adds a newly deployed $70 vault.
    assert Decimal(history[0]['external_tvl_usd'])==750
    if missing_child:
        assert history[1]['status']=='incomplete' and history[1]['external_tvl_usd'] is None
        assert Decimal(history[1]['known_overlap_usd'])==40
    else:
        assert Decimal(history[1]['external_tvl_usd'])==820
    assert [(r['timestamp'],r['block_number']) for r in batched_conn.execute('SELECT * FROM tvl_backfill_blocks ORDER BY timestamp')]==[(86399,1),(172799,3)]
    assert (address(7),1) not in direct_calls
    assert bool(multicall_calls)==multicall
    if multicall:assert (address(9),1) in direct_calls
    batched_conn.close()
