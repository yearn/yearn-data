import json
from types import SimpleNamespace

import pytest
from web3 import Web3

from yearn_data.storage import connect, init_db
from yearn_data.tvl_discovery import DiscoveryReader, V1_REGISTRY, discover_v1


def addr(n):
    return Web3.to_checksum_address('0x'+f'{n:040x}')


@pytest.fixture
def db(tmp_path):
    conn=connect(tmp_path/'catalog.sqlite')
    init_db(conn)
    yield conn
    conn.close()


class MetadataReader:
    def __init__(self,chain_id):
        self.head={'number':123,'hash':bytes.fromhex('ab'*32)}
    def metadata(self,address,family):
        assert family=='v1'
        return {'asset':addr(90),'asset_decimals':0,'asset_symbol':'UNIT','name':'Legacy vault'}


def test_bundled_v1_registry_is_valid_and_complete_for_configured_scope():
    addresses=V1_REGISTRY['vaults']
    assert len(addresses)==28
    assert len({a.lower() for a in addresses})==28
    assert all(Web3.is_address(a) for a in addresses)


def test_v1_discovery_starts_empty_and_repeats_without_duplicates(db):
    first=discover_v1(db,reader_factory=MetadataReader)
    assert first['status']=='complete' and first['enriched']==28
    assert discover_v1(db,reader_factory=MetadataReader)['discovered']==28
    assert db.execute('SELECT COUNT(*) FROM tvl_vaults').fetchone()[0]==28
    vault=db.execute('SELECT * FROM tvl_vaults LIMIT 1').fetchone()
    assert vault['version']=='v1' and vault['asset_decimals']==0
    evidence=db.execute('SELECT * FROM tvl_catalog_evidence LIMIT 1').fetchone()
    assert evidence['metadata_status']=='ok'
    assert json.loads(evidence['evidence_json'])['block_number']==123
    assert db.execute('SELECT COUNT(*) FROM tvl_snapshots').fetchone()[0]==0


def test_missing_rpc_keeps_v1_candidates_and_safe_failures(db):
    def broken(chain):
        raise ValueError('https://secret.example/credential')
    result=discover_v1(db,reader_factory=broken)
    assert result['status']=='incomplete' and result['enriched']==0
    assert len(result['failures'])==28
    assert db.execute('SELECT COUNT(*) FROM tvl_vaults WHERE asset IS NULL').fetchone()[0]==28
    assert 'secret' not in json.dumps(result)
    assert 'credential' not in db.execute('SELECT evidence_json FROM tvl_catalog_evidence LIMIT 1').fetchone()[0]


def test_metadata_retry_preserves_previous_good_values(db):
    discover_v1(db,reader_factory=MetadataReader)
    class Missing(MetadataReader):
        def metadata(self,*args):
            raise RuntimeError('provider unavailable')
    result=discover_v1(db,reader_factory=Missing)
    assert result['status']=='incomplete'
    assert db.execute('SELECT COUNT(*) FROM tvl_vaults WHERE asset_decimals=0').fetchone()[0]==28
    assert discover_v1(db,reader_factory=MetadataReader)['status']=='complete'


def test_v1_chain_filter_avoids_unselected_rpc(db):
    def unexpected(chain):
        raise AssertionError('RPC should not be called')
    assert discover_v1(db,chain_ids=[8453],reader_factory=unexpected)['discovered']==0
    assert db.execute('SELECT COUNT(*) FROM tvl_vaults').fetchone()[0]==0


def test_metadata_uses_v1_token_and_preserves_zero_decimals():
    reader=object.__new__(DiscoveryReader)
    reader.head={'number':123}
    reader.exists=lambda *args:True
    calls=[]
    def call(address,name,output,args=(),*,block=None):
        calls.append(name)
        if name=='token':return addr(90)
        if name=='decimals':return 0
        raise RuntimeError('optional text unavailable')
    reader.call=call
    data=reader.metadata(addr(1),'v1')
    assert data['asset_decimals']==0 and data['name'] is None
    assert 'token' in calls and 'asset' not in calls


def test_discovery_reader_rejects_wrong_rpc_chain(monkeypatch):
    def fake_init(self,chain_id):
        self.w3=SimpleNamespace(eth=SimpleNamespace(chain_id=10))
    monkeypatch.setattr('yearn_data.tvl_discovery.ArchiveReader.__init__',fake_init)
    with pytest.raises(ValueError,match='chain mismatch'):
        DiscoveryReader(1)


def morpho_row(n,chain=1):
    return {'address':addr(n),'chain':{'id':chain},'name':'Curated vault','asset':{'address':addr(90),'decimals':6,'symbol':'USD'}}


def test_morpho_paginates_and_deduplicates_roles_and_chains(monkeypatch):
    from yearn_data.tvl_discovery import morpho_api_candidates
    monkeypatch.setattr('yearn_data.tvl_discovery.CURATION_REGISTRY',{'owners':[{'address':addr(50),'chain_ids':None}]})
    calls=[]
    def request(query,variables):
        calls.append(variables['skip'])
        collection='vaultV2s' if 'vaultV2s(' in query else 'vaults'
        if collection=='vaultV2s':return {collection:{'items':[]}}
        rows=[morpho_row(1),morpho_row(1,8453)] if variables['skip']==0 else [morpho_row(2)]
        return {collection:{'items':rows}}
    candidates,failures=morpho_api_candidates(request=request,page_size=2)
    assert not failures
    assert {(r['chain_id'],r['address']) for r in candidates}=={(1,addr(1)),(8453,addr(1)),(1,addr(2))}
    assert calls.count(2)==3


def test_morpho_repeated_pages_are_reported(monkeypatch):
    from yearn_data.tvl_discovery import morpho_api_candidates
    monkeypatch.setattr('yearn_data.tvl_discovery.CURATION_REGISTRY',{'owners':[{'address':addr(50),'chain_ids':None}]})
    def request(query,variables):
        collection='vaultV2s' if 'vaultV2s(' in query else 'vaults'
        return {collection:{'items':[morpho_row(1)] if collection=='vaults' else []}}
    candidates,failures=morpho_api_candidates(request=request,page_size=1)
    assert len(candidates)==1 and len(failures)==3


def test_chain_scoped_curator_is_not_queried_on_other_chains(monkeypatch):
    from yearn_data.tvl_discovery import morpho_api_candidates
    monkeypatch.setattr('yearn_data.tvl_discovery.CURATION_REGISTRY',{'owners':[{'address':addr(50),'chain_ids':[747474]}]})
    def unexpected(*args):raise AssertionError('out of scope')
    assert morpho_api_candidates(chain_ids=[1],request=unexpected)==([],[])


class FactoryReader(MetadataReader):
    scans=[]
    def metadata(self,address,family):
        return {'asset':addr(90),'asset_decimals':6,'name':'Curated vault'}
    def block(self,number):
        return {'number':number,'hash':bytes.fromhex('ab'*32),'timestamp':1000+number}
    def logs(self,address,abi,start,end):
        self.scans.append((abi['name'],start,end))
        if start<=110<=end:
            args=({'metaMorpho':addr(1),'initialOwner':addr(50),'asset':addr(90)}
                  if abi['name']=='CreateMetaMorpho' else {'newVaultV2':addr(2),'owner':addr(50),'asset':addr(90)})
            return [{'args':args,'blockNumber':110,'logIndex':1,'transactionHash':bytes.fromhex('cd'*32)}]
        return []


def test_factory_uses_actual_family_events_and_resumes(db,monkeypatch):
    from yearn_data.tvl_discovery import scan_factory
    monkeypatch.setattr('yearn_data.tvl_discovery.CURATION_REGISTRY',{'owners':[{'address':addr(50),'chain_ids':None}]})
    reader=FactoryReader(1);reader.scans=[]
    f={'chain_id':1,'address':addr(60),'from_block':100,'family':'morpho-v2'}
    first=scan_factory(db,f,reader,chunk_size=10)
    assert first[0]['address']==addr(2) and first[0]['deployment_block']==110
    assert all(name=='CreateVaultV2' for name,_,_ in reader.scans)
    scanned=len(reader.scans)
    assert scan_factory(db,f,reader,chunk_size=10)==first
    assert len(reader.scans)==scanned
    assert db.execute('SELECT COUNT(*) FROM tvl_inventory_events').fetchone()[0]==1


def test_factory_failure_does_not_certify_missing_range(db,monkeypatch):
    from yearn_data.tvl_discovery import scan_factory
    monkeypatch.setattr('yearn_data.tvl_discovery.CURATION_REGISTRY',{'owners':[{'address':addr(50),'chain_ids':None}]})
    class Fail(FactoryReader):
        def logs(self,address,abi,start,end):
            if start==110:raise RuntimeError('RPC unavailable')
            return super().logs(address,abi,start,end)
    f={'chain_id':1,'address':addr(60),'from_block':100,'family':'morpho-v1'}
    with pytest.raises(RuntimeError):scan_factory(db,f,Fail(1),chunk_size=10)
    rows=db.execute('SELECT from_block,to_block FROM tvl_history_coverage').fetchall()
    assert [tuple(r) for r in rows]==[(100,109)]


def test_curation_unions_api_factory_and_extra_and_preserves_omissions(db,monkeypatch):
    from yearn_data.tvl_discovery import discover_curation
    registry={'owners':[{'address':addr(50),'chain_ids':None}],
              'factories':[{'chain_id':1,'address':addr(60),'from_block':100,'family':'morpho-v2'}],
              'extra_vaults':[{'chain_id':1,'address':addr(3),'family':'erc4626','label':'Extra'}]}
    monkeypatch.setattr('yearn_data.tvl_discovery.CURATION_REGISTRY',registry)
    def request(query,variables):
        collection='vaultV2s' if 'vaultV2s(' in query else 'vaults'
        return {collection:{'items':[morpho_row(4)] if collection=='vaults' else []}}
    result=discover_curation(db,reader_factory=FactoryReader,request=request,chunk_size=10)
    assert result['status']=='complete' and result['enriched']==3
    assert {(r['version'],r['tvl_category']) for r in db.execute('SELECT * FROM tvl_vaults')}=={
        ('morpho-v1','curation'),('morpho-v2','curation'),('erc4626','curation')}
    def empty(query,variables):
        return {('vaultV2s' if 'vaultV2s(' in query else 'vaults'):{'items':[]}}
    discover_curation(db,reader_factory=FactoryReader,request=empty,chunk_size=10)
    assert db.execute('SELECT COUNT(*) FROM tvl_vaults').fetchone()[0]==3


def test_curation_api_failure_does_not_prevent_factory_or_extra_discovery(db,monkeypatch):
    from yearn_data.tvl_discovery import discover_curation
    monkeypatch.setattr('yearn_data.tvl_discovery.CURATION_REGISTRY',{
        'owners':[{'address':addr(50),'chain_ids':None}],
        'factories':[{'chain_id':1,'address':addr(60),'from_block':100,'family':'morpho-v1'}],
        'extra_vaults':[{'chain_id':1,'address':addr(3),'family':'erc4626','label':'Extra'}]})
    def broken(*args):raise RuntimeError('API unavailable')
    result=discover_curation(db,reader_factory=FactoryReader,request=broken,chunk_size=10)
    assert result['status']=='incomplete' and result['enriched']==2
    assert any(f['stage']=='morpho-api' for f in result['failures'])
    assert db.execute('SELECT COUNT(*) FROM tvl_vaults').fetchone()[0]==2


def test_real_metamorpho_indexed_asset_log_decodes():
    # Independently construct the official four-topic event layout.
    from eth_abi import encode
    from yearn_data.tvl_discovery import FACTORY_EVENTS
    from web3._utils.events import get_event_data
    from hexbytes import HexBytes
    w3=Web3()
    signature='CreateMetaMorpho(address,address,address,uint256,address,string,string,bytes32)'
    log={'address':addr(60),'blockHash':HexBytes('0x'+'ab'*32),'blockNumber':110,
         'transactionHash':HexBytes('0x'+'cd'*32),'transactionIndex':0,'logIndex':1,
         'topics':[w3.keccak(text=signature),HexBytes(encode(['address'],[addr(1)])),
                   HexBytes(encode(['address'],[addr(2)])),HexBytes(encode(['address'],[addr(90)]))],
         'data':HexBytes(encode(['address','uint256','string','string','bytes32'],
                               [addr(50),86400,'Curated USD','yvUSD',bytes(32)]))}
    decoded=get_event_data(w3.codec,FACTORY_EVENTS['morpho-v1'],log)
    assert decoded['args']['asset']==addr(90)
    assert decoded['args']['initialOwner']==addr(50)
    assert decoded['args']['metaMorpho']==addr(1)


def test_generic_extra_does_not_erase_known_morpho_family_during_api_outage(db,monkeypatch):
    from yearn_data.tvl_discovery import discover_curation
    registry={'owners':[{'address':addr(50),'chain_ids':None}],'factories':[],
              'extra_vaults':[{'chain_id':1,'address':addr(3),'family':'erc4626','label':'Extra'}]}
    monkeypatch.setattr('yearn_data.tvl_discovery.CURATION_REGISTRY',registry)
    def request(query,variables):
        collection='vaultV2s' if 'vaultV2s(' in query else 'vaults'
        return {collection:{'items':[morpho_row(3)] if collection=='vaultV2s' else []}}
    discover_curation(db,reader_factory=FactoryReader,request=request)
    assert db.execute('SELECT version FROM tvl_vaults').fetchone()[0]=='morpho-v2'
    def broken(*args):raise RuntimeError('API unavailable')
    result=discover_curation(db,reader_factory=FactoryReader,request=broken)
    assert result['status']=='incomplete'
    assert db.execute('SELECT version FROM tvl_vaults').fetchone()[0]=='morpho-v2'


class AdapterReader(FactoryReader):
    def metadata(self,address,family,*,block=None):
        return {'asset':addr(90),'asset_decimals':6,'name':'Curated vault'}
    def deployment_block(self,address):return 100
    def call(self,address,name,output,args=(),*,block=None):
        if name=='adaptersLength':return 2
        if name=='adapters':return addr(9 if args[0][1]==0 else 10)
        if name=='parentVault':return addr(1)
        if name=='morphoVaultV1' and address.lower() in [addr(9).lower(),addr(11).lower()]:
            return addr(2 if address.lower()==addr(9).lower() else 3)
        if address.lower()==addr(10).lower() and name=='morpho':return addr(80)
        if address.lower()==addr(10).lower() and name=='marketIdsLength':return 1
        raise RuntimeError('unsupported getter')
    def logs(self,address,abi,start,end):
        if abi['name']=='AddAdapter' and start<=110<=end:
            return [{'args':{'account':addr(11)},'blockNumber':110,'logIndex':1,
                     'transactionHash':bytes.fromhex('cd'*32)}]
        return []


def seed_adapter_catalog(db,children=True):
    from yearn_data.tvl_discovery import store_candidate
    metadata={'asset':addr(90),'asset_decimals':6}
    with db:
        store_candidate(db,1,addr(1),'morpho-v2','curation','fixture',metadata=metadata,status='ok')
        if children:
            for n in (2,3):
                store_candidate(db,1,addr(n),'morpho-v1','curation','fixture',metadata=metadata,status='ok')


def test_adapter_discovery_includes_non_liquidity_and_removed_adapters(db):
    from yearn_data.tvl_discovery import discover_adapter_relations
    seed_adapter_catalog(db)
    first=discover_adapter_relations(db,reader_factory=AdapterReader,chunk_size=10)
    assert first['status']=='complete'
    assert (first['adapters'],first['nested'],first['market_adapters'])==(3,2,1)
    links=db.execute('SELECT parent,strategy FROM tvl_strategies').fetchall()
    assert {r['strategy'] for r in links}=={addr(n).lower() for n in (9,10,11)}
    assert db.execute('SELECT COUNT(*) FROM tvl_targets').fetchone()[0]==2
    discover_adapter_relations(db,reader_factory=AdapterReader,chunk_size=10)
    assert db.execute('SELECT COUNT(*) FROM tvl_targets').fetchone()[0]==2
    assert db.execute("SELECT COUNT(*) FROM tvl_events_raw WHERE event_name='AddAdapter'").fetchone()[0]==1


def test_external_child_is_a_relation_without_adding_other_protocol_tvl(db):
    from yearn_data.tvl_discovery import discover_adapter_relations
    from yearn_data.tvl_sources import candidate_edges
    seed_adapter_catalog(db,children=False)
    discover_adapter_relations(db,reader_factory=AdapterReader,chunk_size=10)
    assert db.execute('SELECT COUNT(*) FROM tvl_vaults').fetchone()[0]==1
    edges=candidate_edges(db,[dict(r) for r in db.execute('SELECT * FROM tvl_vaults')])
    assert {e['child'] for e in edges if e['method']!='strategy-allocation'}=={addr(2).lower(),addr(3).lower()}


def test_unresolved_adapter_parent_does_not_create_child_mapping(db):
    from yearn_data.tvl_discovery import discover_adapter_relations
    seed_adapter_catalog(db)
    class WrongParent(AdapterReader):
        def call(self,address,name,output,args=(),*,block=None):
            if name=='parentVault':return addr(99)
            return super().call(address,name,output,args,block=block)
    result=discover_adapter_relations(db,reader_factory=WrongParent,chunk_size=10)
    assert result['status']=='incomplete' and result['nested']==0
    assert db.execute('SELECT COUNT(*) FROM tvl_targets').fetchone()[0]==0
    assert db.execute("SELECT COUNT(*) FROM tvl_catalog_evidence WHERE metadata_status='unresolved'").fetchone()[0]==3


def test_missing_adapter_history_keeps_current_relationships(db):
    from yearn_data.tvl_discovery import discover_adapter_relations
    seed_adapter_catalog(db)
    class MissingHistory(AdapterReader):
        def logs(self,*args):raise RuntimeError('archive unavailable')
    result=discover_adapter_relations(db,reader_factory=MissingHistory,chunk_size=10)
    assert result['status']=='incomplete' and result['nested']==1
    assert any(f['stage']=='adapter-history' for f in result['failures'])
    assert db.execute('SELECT COUNT(*) FROM tvl_targets').fetchone()[0]==1


def test_adapter_list_failure_still_discovers_removed_adapters(db):
    from yearn_data.tvl_discovery import discover_adapter_relations
    seed_adapter_catalog(db)
    class NoCurrentList(AdapterReader):
        def call(self,address,name,output,args=(),*,block=None):
            if name=='adaptersLength':raise RuntimeError('current list unavailable')
            return super().call(address,name,output,args,block=block)
    result=discover_adapter_relations(db,reader_factory=NoCurrentList,chunk_size=10)
    assert result['status']=='incomplete' and result['nested']==1
    assert any(f['stage']=='adapter-list' for f in result['failures'])
    assert db.execute('SELECT child FROM tvl_targets').fetchone()[0]==addr(3).lower()


def test_positive_debt_with_unresolved_adapter_mapping_is_incomplete(db,monkeypatch,tmp_path):
    from yearn_data.tvl_discovery import discover_adapter_relations
    from yearn_data.tvl_sources import collect_tvl
    from yearn_data.tvl import export_tvl
    seed_adapter_catalog(db)
    class Unknown(AdapterReader):
        def call(self,address,name,output,args=(),*,block=None):
            if name=='parentVault':return addr(99)
            return super().call(address,name,output,args,block=block)
    discover_adapter_relations(db,reader_factory=Unknown,chunk_size=10)
    class Collector:
        def __init__(self,chain):pass
        def block_at(self,timestamp):return 123
        def exists(self,*args):return True
        def uint(self,address,signature,block,arg=None):return 100000000
        def debt(self,*args):return 60000000
    monkeypatch.setattr('yearn_data.tvl_sources.fetch_defillama_price',lambda *args:(1,'ok',{}))
    run=collect_tvl(db,100,100,reader_factory=Collector,price_source='defillama')
    export_tvl(db,tmp_path/'out',run,include_curation=True)
    data=json.loads((tmp_path/'out/tvl.json').read_text())
    assert data['history'][0]['external_tvl_usd'] is None
    assert data['history'][0]['issues']==['unresolved_adapter_mapping']
    assert any(p.get('reason')=='unresolved_adapter_mapping' and p['debt_usd']=='60' for p in data['positions'])


def test_unified_discovery_runs_all_sources_and_retains_summary(db,monkeypatch):
    from yearn_data.tvl_discovery import discover_catalog,store_candidate
    monkeypatch.setattr('yearn_data.tvl_discovery.CURATION_REGISTRY',{
        'owners':[],'factories':[],'extra_vaults':[{'chain_id':1,'address':addr(3),'family':'erc4626','label':'Extra'}]})
    def kong(conn,*,chain_ids,with_summary):
        assert chain_ids==[1] and with_summary
        with conn:store_candidate(conn,1,addr(100),'v2','v2','kong',metadata={'asset':addr(90),'asset_decimals':6},status='ok')
        return {'source':'kong','status':'complete','discovered':1,'enriched':1,'failures':[]}
    monkeypatch.setattr('yearn_data.tvl_sources.refresh_kong_catalog',kong)
    def forbidden(*args):raise AssertionError('old service must not be read')
    monkeypatch.setattr('yearn_data.tvl_sources.import_service_catalog',forbidden)
    result=discover_catalog(db,chain_ids=[1],reader_factory=FactoryReader)
    assert result['status']=='complete' and result['catalog_vaults']==30
    assert [r['source'] for r in result['sources']]==['kong','v1-registry','curation-discovery','curation-relations']
    run=db.execute('SELECT * FROM tvl_discovery_runs WHERE id=?',(result['run_id'],)).fetchone()
    assert json.loads(run['summary_json'])==result


def test_source_failure_does_not_prevent_other_discovery(db,monkeypatch):
    from yearn_data.tvl_discovery import discover_catalog
    def broken(*args,**kwargs):raise RuntimeError('https://secret.example/key')
    monkeypatch.setattr('yearn_data.tvl_sources.refresh_kong_catalog',broken)
    result=discover_catalog(db,sources=['kong','v1'],reader_factory=MetadataReader)
    assert result['status']=='incomplete' and result['catalog_vaults']==28
    assert result['sources'][1]['status']=='complete'
    assert 'secret' not in json.dumps(result)
    assert db.execute('SELECT status FROM tvl_discovery_runs').fetchone()[0]=='incomplete'


def test_unified_source_chain_filter_and_invalid_bounds(db):
    from yearn_data.tvl_discovery import discover_catalog
    def unexpected(chain):raise AssertionError('unselected RPC')
    result=discover_catalog(db,sources=['v1'],chain_ids=[8453],reader_factory=unexpected)
    assert result['status']=='complete' and result['catalog_vaults']==0
    with pytest.raises(ValueError,match='from-block'):
        discover_catalog(db,from_block=200,to_block=100)
    with pytest.raises(ValueError,match='positive'):
        discover_catalog(db,chain_ids=[0])
    assert db.execute('SELECT COUNT(*) FROM tvl_discovery_runs').fetchone()[0]==1


def test_cli_discovery_returns_nonzero_for_partial_source_results(tmp_path,monkeypatch,capsys):
    from yearn_data.cli import main
    args_seen={}
    def discover(conn,**kwargs):
        args_seen.update(kwargs)
        return {'status':'incomplete','run_id':1,'sources':[]}
    monkeypatch.setattr('yearn_data.tvl_discovery.discover_catalog',discover)
    code=main(['--db',str(tmp_path/'cli.sqlite'),'tvl','discover','--sources','v1',
               '--chain-ids','1','--from-block','100','--to-block','200','--chunk-size','10'])
    assert code==2
    assert args_seen['sources']==['v1'] and args_seen['chain_ids']==[1]
    assert (args_seen['from_block'],args_seen['to_block'],args_seen['chunk_size'])==(100,200,10)
    assert json.loads(capsys.readouterr().out)['status']=='incomplete'


def test_kong_refresh_paginates_selected_chain_and_preserves_morpho_family(db,monkeypatch):
    from yearn_data.tvl_sources import refresh_kong_catalog
    from yearn_data.tvl_discovery import store_candidate
    with db:store_candidate(db,8453,addr(1000),'morpho-v2','curation','fixture',metadata={'asset':addr(90),'asset_decimals':6})
    calls=[]
    class Response:
        def raise_for_status(self):pass
        def json(self):return {'data':{'vaults':self.rows}}
    def request(url,*,json,timeout):
        variables=json['variables'];calls.append(variables)
        assert variables['chainId']==8453
        response=Response()
        response.rows=[{'address':addr(n),'chainId':8453,'v3':True,'name':'Vault','apiVersion':'3.0.4',
                        'asset':{'address':addr(90),'decimals':6,'symbol':'USD'},'strategies':[]}
                       for n in (range(1000,1100) if variables['skip']==0 else [1100])]
        return response
    monkeypatch.setattr('yearn_data.tvl_sources.requests.post',request)
    summary=refresh_kong_catalog(db,chain_ids=[8453],with_summary=True)
    assert summary['status']=='complete' and summary['discovered']==101
    assert [v['skip'] for v in calls]==[0,100]
    assert db.execute('SELECT COUNT(*) FROM tvl_vaults').fetchone()[0]==101
    old=db.execute('SELECT version,tvl_category FROM tvl_vaults WHERE lower(address)=?',(addr(1000).lower(),)).fetchone()
    assert tuple(old)==('morpho-v2','curation')


def test_kong_partial_pages_are_retained_and_other_chains_continue(db,monkeypatch):
    from yearn_data.tvl_discovery import discover_catalog
    failed=True
    class Response:
        def raise_for_status(self):pass
        def json(self):return {'data':{'vaults':self.rows}}
    def request(url,*,json,timeout):
        variables=json['variables'];chain=variables['chainId'];skip=variables['skip']
        if chain==1 and skip==100 and failed:
            raise TimeoutError('https://secret.example/key')
        response=Response()
        ids=range(1000,1100) if chain==1 and skip==0 else ([1100] if chain==1 else [1000])
        response.rows=[{'address':addr(n),'chainId':chain,'v3':True,'name':'Vault',
                        'asset':{'address':addr(90),'decimals':6},'strategies':[]} for n in ids]
        return response
    monkeypatch.setattr('yearn_data.tvl_sources.requests.post',request)
    first=discover_catalog(db,sources=['kong'],chain_ids=[1,8453])
    assert first['status']=='incomplete' and first['catalog_vaults']==101
    assert first['sources'][0]['discovered']==101 and first['sources'][0]['enriched']==101
    assert db.execute("SELECT COUNT(*) FROM tvl_catalog_evidence WHERE source='kong'").fetchone()[0]==101
    assert 'secret' not in json.dumps(first)
    failed=False
    second=discover_catalog(db,sources=['kong'],chain_ids=[1,8453])
    assert second['status']=='complete' and second['catalog_vaults']==102


def test_factory_window_before_deployment_is_an_empty_scope(db):
    from yearn_data.tvl_discovery import scan_factory
    factory={'chain_id':1,'address':addr(60),'from_block':120,'family':'morpho-v2'}
    reader=FactoryReader(1)
    assert scan_factory(db,factory,reader,from_block=100,to_block=110)==[]
    assert db.execute('SELECT COUNT(*) FROM tvl_history_coverage').fetchone()[0]==0


def test_fresh_catalog_collect_and_export_without_migration(db,monkeypatch,tmp_path):
    from yearn_data.tvl_discovery import discover_catalog,store_candidate
    from yearn_data.tvl_sources import collect_tvl
    from yearn_data.tvl import export_tvl
    registry={'owners':[{'address':addr(50),'chain_ids':None}],'factories':[],
              'extra_vaults':[{'chain_id':1,'address':addr(4),'family':'erc4626','label':'Extra'}]}
    monkeypatch.setattr('yearn_data.tvl_discovery.CURATION_REGISTRY',registry)
    def kong(conn,**kwargs):
        with conn:
            for n,family in [(100,'v2'),(101,'v3')]:
                store_candidate(conn,1,addr(n),family,family,'kong',metadata={'asset':addr(90),'asset_decimals':6},status='ok')
        return {'source':'kong','status':'complete','discovered':2,'enriched':2,'failures':[]}
    monkeypatch.setattr('yearn_data.tvl_sources.refresh_kong_catalog',kong)
    def request(query,variables):
        collection='vaultV2s' if 'vaultV2s(' in query else 'vaults'
        return {collection:{'items':[morpho_row(1 if collection=='vaultV2s' else 2)]}}
    def forbidden(*args):raise AssertionError('old TVL service access forbidden')
    monkeypatch.setattr('yearn_data.tvl_sources.import_service_catalog',forbidden)
    first=discover_catalog(db,chain_ids=[1],reader_factory=AdapterReader,request=request,chunk_size=10)
    second=discover_catalog(db,chain_ids=[1],reader_factory=AdapterReader,request=request,chunk_size=10)
    assert first['status']==second['status']=='complete'
    assert first['catalog_vaults']==second['catalog_vaults']
    assert {r[0] for r in db.execute('SELECT COALESCE(tvl_category,version) FROM tvl_vaults')}=={'v1','v2','v3','curation'}
    class Collector:
        def __init__(self,chain):pass
        def block_at(self,timestamp):return 123
        def exists(self,*args):return True
        def uint(self,address,signature,block,arg=None):
            return {'totalSupply()':100000000,'totalAssets()':100000000,'balanceOf(address)':40000000}[signature]
        def debt(self,*args):return 60000000
    monkeypatch.setattr('yearn_data.tvl_sources.fetch_defillama_price',lambda *args:(1,'ok',{}))
    run=collect_tvl(db,100,100,addresses=[addr(1),addr(2)],reader_factory=Collector,price_source='defillama')
    export_tvl(db,tmp_path/'out',run,include_curation=True)
    data=json.loads((tmp_path/'out/tvl.json').read_text())
    assert data['history'][0]['status']=='complete'
    assert float(data['history'][0]['external_tvl_usd'])==160
    assert any(p['strategy']==addr(9).lower() and float(p['overlap_usd'])==40 for p in data['positions'])
