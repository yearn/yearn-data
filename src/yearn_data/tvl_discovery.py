"""Independent TVL catalog discovery. No balances, prices, or old-service database."""
from __future__ import annotations

import json
import time
from pathlib import Path

from web3 import Web3

from .config import TVL_EXCLUDED_CHAIN_IDS
from .storage import to_json
from .tvl_sources import ArchiveReader

INVENTORIES = Path(__file__).parent / 'inventories'
V1_REGISTRY = json.loads((INVENTORIES / 'tvl-v1.json').read_text())


class DiscoveryReader(ArchiveReader):
    """Read metadata at one finalized block with the existing TVL RPC configuration."""

    def __init__(self, chain_id):
        super().__init__(chain_id)
        if int(self.w3.eth.chain_id) != chain_id:
            raise ValueError('RPC chain mismatch')

    def call(self, address, name, output, args=(), *, block=None):
        inputs = [{'name': f'arg{i}', 'type': kind} for i, (kind, _) in enumerate(args)]
        abi = [{'type': 'function', 'name': name, 'inputs': inputs,
                'outputs': [{'name': '', 'type': output}], 'stateMutability': 'view'}]
        contract = self.w3.eth.contract(address=Web3.to_checksum_address(address), abi=abi)
        return getattr(contract.functions, name)(*(value for _, value in args)).call(
            block_identifier=int(self.head['number']) if block is None else block)

    def deployment_block(self, address):
        lo, hi = 0, int(self.head['number'])
        if not self.exists(address,hi):
            raise ValueError('vault deployment unavailable')
        while lo<hi:
            mid = (lo+hi)//2
            if self.exists(address,mid):
                hi=mid
            else:
                lo=mid+1
        return lo

    def block(self, number):
        return self.w3.eth.get_block(number)

    def logs(self, address, abi, start, end):
        from eth_utils import event_abi_to_log_topic
        from web3._utils.events import get_event_data
        raw = self.w3.eth.get_logs({'address':Web3.to_checksum_address(address),
            'fromBlock':start,'toBlock':end,'topics':[event_abi_to_log_topic(abi)]})
        decoded = []
        for log in raw:
            if (log['address'].lower()!=address.lower() or not start<=int(log['blockNumber'])<=end
                    or log.get('removed')):
                raise ValueError('RPC log scope mismatch')
            decoded.append(dict(get_event_data(self.w3.codec,abi,log)))
        return decoded

    def metadata(self, address, family, *, block=None):
        block = int(self.head['number']) if block is None else block
        if not self.exists(address, block):
            raise ValueError('vault has no contract code')
        asset = self.call(address, 'token' if family == 'v1' else 'asset', 'address', block=block)
        if int(asset, 16) == 0 or not self.exists(asset, block):
            raise ValueError('asset has no contract code')
        decimals = self.call(asset, 'decimals', 'uint8', block=block)
        if isinstance(decimals, bool) or not isinstance(decimals, int) or not 0 <= decimals <= 255:
            raise ValueError('invalid asset decimals')
        optional = {}
        for key, target, name in [('name', address, 'name'), ('asset_symbol', asset, 'symbol')]:
            try:
                optional[key] = self.call(target, name, 'string', block=block)
            except Exception:
                optional[key] = None
        return {'asset': Web3.to_checksum_address(asset), 'asset_decimals': decimals, **optional}


def store_candidate(conn, chain_id, address, family, category, source, *, metadata=None,
                    status='unavailable', evidence=None, deployment_block=None):
    """Keep known candidates even when enrichment fails; never erase good metadata."""
    address = Web3.to_checksum_address(address)
    metadata = metadata or {}
    now = int(time.time())
    existing = conn.execute('SELECT address FROM tvl_vaults WHERE chain_id=? AND lower(address)=?',
                            (chain_id, address.lower())).fetchone()
    stored_address = existing['address'] if existing else address
    conn.execute('''INSERT INTO tvl_vaults(chain_id,version,address,asset,asset_symbol,asset_decimals,
        name,management,protocol,deployment_block,updated_at,tvl_category)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(chain_id,address) DO UPDATE SET
        version=CASE WHEN excluded.version='erc4626' AND tvl_vaults.version IN ('v2','v3','morpho-v1','morpho-v2')
                     THEN tvl_vaults.version ELSE excluded.version END,
        asset=COALESCE(excluded.asset,tvl_vaults.asset),asset_symbol=COALESCE(excluded.asset_symbol,tvl_vaults.asset_symbol),
        asset_decimals=COALESCE(excluded.asset_decimals,tvl_vaults.asset_decimals),
        name=COALESCE(excluded.name,tvl_vaults.name),protocol=COALESCE(excluded.protocol,tvl_vaults.protocol),
        deployment_block=COALESCE(tvl_vaults.deployment_block,excluded.deployment_block),
        tvl_category=excluded.tvl_category,updated_at=excluded.updated_at''',
        (chain_id,family,stored_address,metadata.get('asset'),metadata.get('asset_symbol'),
         metadata.get('asset_decimals'),metadata.get('name'),'yearn',
         'curation' if category=='curation' else None,deployment_block,now,category))
    conn.execute('''INSERT INTO tvl_catalog_evidence VALUES (?,?,?,?,?,?)
        ON CONFLICT(chain_id,address,source) DO UPDATE SET metadata_status=excluded.metadata_status,
        evidence_json=excluded.evidence_json,updated_at=excluded.updated_at''',
        (chain_id,address.lower(),source,status,to_json(evidence or {}),now))


def source_summary(source):
    return {'source':source,'discovered':0,'enriched':0,'failures':[],'status':'complete'}


def enrich_candidates(conn, candidates, source, *, reader_factory=DiscoveryReader):
    result = source_summary(source)
    readers = {}
    seen = set()
    for candidate in candidates:
        chain_id, address = candidate['chain_id'], candidate['address']
        if chain_id in TVL_EXCLUDED_CHAIN_IDS:
            continue
        key = (chain_id,address.lower())
        if key in seen:
            continue
        seen.add(key)
        result['discovered'] += 1
        evidence = dict(candidate.get('evidence') or {})
        metadata, status = None, 'unavailable'
        try:
            if chain_id not in readers:
                try:
                    readers[chain_id] = reader_factory(chain_id)
                except Exception as exc:
                    readers[chain_id] = type(exc).__name__
            reader = readers[chain_id]
            if isinstance(reader, str):
                raise RuntimeError('RPC setup unavailable')
            metadata = reader.metadata(address, candidate['family'])
            # Reader substitutions must preserve the required metadata contract too.
            if not metadata.get('asset') or metadata.get('asset_decimals') is None:
                raise ValueError('incomplete asset metadata')
            status = 'ok'
            evidence['block_number'] = int(reader.head['number'])
            evidence['block_hash'] = Web3.to_hex(reader.head['hash'])
            result['enriched'] += 1
        except Exception as exc:
            result['status'] = 'incomplete'
            failure = {'chain_id':chain_id,'address':address.lower(),'stage':'metadata',
                       'reason':type(exc).__name__}
            result['failures'].append(failure)
            evidence['failure'] = failure
        with conn:
            store_candidate(conn,chain_id,address,candidate['family'],candidate['category'],source,
                            metadata=metadata,status=status,evidence=evidence,
                            deployment_block=candidate.get('deployment_block'))
    return result


def discover_v1(conn, *, chain_ids=None, reader_factory=DiscoveryReader):
    registry = V1_REGISTRY
    candidates = []
    if chain_ids is None or registry['chain_id'] in chain_ids:
        candidates = [{'chain_id':registry['chain_id'],'address':address,'family':'v1','category':'v1',
                       'evidence':{'registry':registry['source']}} for address in registry['vaults']]
    return enrich_candidates(conn,candidates,'v1-registry',reader_factory=reader_factory)

# Morpho's V1 and V2 factories emit different events. V2 is not MetaMorphoV2.
def _event(name, fields):
    return {'type':'event','name':name,'anonymous':False,
            'inputs':[{'name':name,'type':kind,'indexed':indexed} for name,kind,indexed in fields]}


FACTORY_EVENTS = {
    'morpho-v1': _event('CreateMetaMorpho', [
        ('metaMorpho','address',True),('caller','address',True),('initialOwner','address',False),
        ('initialTimelock','uint256',False),('asset','address',True),('name','string',False),
        ('symbol','string',False),('salt','bytes32',False)]),
    'morpho-v2': _event('CreateVaultV2', [
        ('owner','address',True),('asset','address',True),('salt','bytes32',False),('newVaultV2','address',True)]),
}
CURATION_REGISTRY = json.loads((INVENTORIES / 'tvl-curation.json').read_text())


def owners_for_chain(chain_id):
    return {row['address'].lower() for row in CURATION_REGISTRY['owners']
            if row['chain_ids'] is None or chain_id in row['chain_ids']}


def morpho_request(query, variables):
    import os
    import requests
    response = requests.post(os.environ.get('YEARN_MORPHO_GRAPHQL_URL','https://api.morpho.org/graphql'),
                             json={'query':query,'variables':variables},timeout=30)
    response.raise_for_status()
    body = response.json()
    if body.get('errors') or not isinstance(body.get('data'),dict):
        raise ValueError('Morpho discovery query failed')
    return body['data']


def morpho_api_candidates(*, chain_ids=None, request=morpho_request, page_size=100):
    """Paginate each role/family query; include unlisted vaults and scoped owners."""
    if page_size < 1:
        raise ValueError('invalid Morpho page size')
    candidates = {}
    failures = []
    scopes = {}
    for owner in CURATION_REGISTRY['owners']:
        scope = owner['chain_ids']
        if chain_ids is not None:
            scope = sorted((set(chain_ids) if scope is None else set(scope)&set(chain_ids))-TVL_EXCLUDED_CHAIN_IDS)
            if not scope:
                continue
        if scope is not None:
            scope = sorted(set(scope)-TVL_EXCLUDED_CHAIN_IDS)
            if not scope:
                continue
        scopes.setdefault(tuple(scope) if scope is not None else None,[]).append(owner['address'])
    for scope, owners in scopes.items():
        for family, collection, roles in [('morpho-v1','vaults',('owner','creator','curator')),
                                          ('morpho-v2','vaultV2s',('owner','curator'))]:
            for role in roles:
                filters = f'{role}Address_in:{json.dumps(owners)}'
                if scope is not None:
                    filters += f',chainId_in:{json.dumps(scope)}'
                query = f'''query($skip:Int!,$first:Int!){{{collection}(skip:$skip,first:$first,
                    where:{{{filters}}}){{items{{address name chain{{id}} asset{{address decimals symbol}}}}}}}}'''
                skip, seen = 0, set()
                try:
                    while True:
                        data = request(query,{'skip':skip,'first':page_size})
                        rows = data[collection]['items']
                        if not isinstance(rows,list):
                            raise ValueError('invalid Morpho page')
                        for row in rows:
                            chain = int(row['chain']['id'])
                            address = Web3.to_checksum_address(row['address'])
                            key = (chain,address.lower())
                            if key in seen or (scope is not None and chain not in scope):
                                raise ValueError('Morpho pagination or scope mismatch')
                            seen.add(key)
                            if chain in TVL_EXCLUDED_CHAIN_IDS:
                                continue
                            old = candidates.get(key)
                            if old and old['family'] != family:
                                raise ValueError('conflicting Morpho contract family')
                            candidates[key] = {'chain_id':chain,'address':address,'family':family,
                                               'category':'curation','evidence':{'role':role,'source':'morpho-api'}}
                        skip += len(rows)
                        if len(rows)<page_size:
                            break
                except Exception as exc:
                    failures.append({'stage':'morpho-api','family':family,'role':role,
                                     'chain_ids':list(scope) if scope is not None else None,'reason':type(exc).__name__})
    return list(candidates.values()), failures


def scan_factory(conn, factory, reader, *, from_block=None, to_block=None, chunk_size=50_000):
    """Store matching creation evidence and successful coverage in one transaction."""
    from .coverage import Scope, fingerprint, missing_ranges, commit_range
    start = max(factory['from_block'],from_block if from_block is not None else factory['from_block'])
    end = int(reader.head['number']) if to_block is None else int(to_block)
    if end > int(reader.head['number']) or start<0 or end<0 or chunk_size<1:
        raise ValueError('invalid finalized factory range')
    if end<factory['from_block']:
        return []
    owners = owners_for_chain(factory['chain_id'])
    abi = FACTORY_EVENTS[factory['family']]
    scope = Scope(factory['chain_id'],factory['address'],'inventory:tvl-curation',fingerprint([abi,sorted(owners)]))
    for lo,hi in missing_ranges(conn,scope,start,end,chunk_size,table='tvl_history_coverage'):
        logs = reader.logs(factory['address'],abi,lo,hi)
        # Reject a possible capped response instead of certifying a truncated scan.
        if len(logs)>=10_000:
            raise ValueError('factory result cap; use smaller chunks')
        accepted = []
        for log in logs:
            args = log['args']
            owner = args['initialOwner'] if factory['family']=='morpho-v1' else args['owner']
            if owner.lower() not in owners:
                continue
            address = args['metaMorpho'] if factory['family']=='morpho-v1' else args['newVaultV2']
            accepted.append((log,Web3.to_checksum_address(address)))
        def write(db):
            for log,address in accepted:
                header = reader.block(log['blockNumber'])
                args = log['args']
                tx = Web3.to_hex(log['transactionHash'])
                asset = Web3.to_checksum_address(args['asset'])
                db.execute('''INSERT OR IGNORE INTO tvl_inventory_events
                    (chain_id,version,vault_address,source_kind,source_address,asset,tx_hash,
                     log_index,block_number,block_timestamp,decoded_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
                    (factory['chain_id'],factory['family'],address,'tvl-curation-factory',factory['address'].lower(),
                     asset,tx,int(log['logIndex']),int(log['blockNumber']),int(header['timestamp']),
                     to_json({'owner':args.get('initialOwner',args.get('owner')),'event':abi['name']})))
                store_candidate(db,factory['chain_id'],address,factory['family'],'curation','morpho-factory',
                                evidence={'factory':factory['address'],'tx_hash':tx,'log_index':int(log['logIndex'])},
                                deployment_block=int(log['blockNumber']))
            return len(accepted)
        end_hash = Web3.to_hex(reader.block(hi)['hash'])
        commit_range(conn,scope,lo,hi,source='rpc',end_hash=end_hash,run_id=None,write=write,table='tvl_history_coverage')
    rows = conn.execute('''SELECT vault_address,MIN(block_number) AS deployment_block FROM tvl_inventory_events
        WHERE chain_id=? AND source_kind='tvl-curation-factory' AND lower(source_address)=?
        AND block_number<=? GROUP BY vault_address''',(factory['chain_id'],factory['address'].lower(),end))
    return [{'chain_id':factory['chain_id'],'address':r['vault_address'],'family':factory['family'],
             'category':'curation','deployment_block':r['deployment_block'],
             'evidence':{'factory':factory['address'],'source':'morpho-factory'}} for r in rows]


def discover_curation(conn, *, chain_ids=None, reader_factory=DiscoveryReader, request=morpho_request,
                      from_block=None, to_block=None, chunk_size=50_000, page_size=100, progress=None):
    candidates, failures = morpho_api_candidates(chain_ids=chain_ids,request=request,page_size=page_size)
    result = {'api_candidates':len(candidates),'factories':[]}
    readers = {}
    for factory in CURATION_REGISTRY['factories']:
        chain = factory['chain_id']
        if chain in TVL_EXCLUDED_CHAIN_IDS or (chain_ids is not None and chain not in chain_ids):
            continue
        scope = {'chain_id':chain,'address':factory['address'],'family':factory['family'],
                 'from_block':max(factory['from_block'],from_block or factory['from_block'])}
        try:
            if chain not in readers:
                try:
                    readers[chain] = reader_factory(chain)
                except Exception as exc:
                    readers[chain] = type(exc).__name__
            reader = readers[chain]
            if isinstance(reader,str):
                raise RuntimeError('factory RPC unavailable')
            candidates.extend(scan_factory(conn,factory,reader,from_block=from_block,to_block=to_block,
                                           chunk_size=chunk_size))
            scope.update(status='complete',to_block=int(reader.head['number']) if to_block is None else to_block)
        except Exception as exc:
            failure = {**scope,'stage':'factory','reason':type(exc).__name__}
            failures.append(failure)
            scope['status'] = 'incomplete'
        result['factories'].append(scope)
        if progress:
            progress(f"Curation factory chain {chain} {factory['address']}: {scope['status']}")
    for extra in CURATION_REGISTRY['extra_vaults']:
        if extra['chain_id'] not in TVL_EXCLUDED_CHAIN_IDS and (chain_ids is None or extra['chain_id'] in chain_ids):
            candidates.append({**extra,'category':'curation','evidence':{'source':'extra-product','label':extra['label']}})
    # API and factory evidence must agree on a contract family. Generic extras are
    # allowed to add a product without replacing an already known Morpho family.
    merged = {}
    for candidate in candidates:
        key = (candidate['chain_id'],candidate['address'].lower())
        previous = merged.get(key)
        if previous and candidate['family']=='erc4626':
            continue
        if previous and previous['family']!=candidate['family'] and previous['family']!='erc4626':
            failures.append({'chain_id':key[0],'address':key[1],'stage':'identity','reason':'contract_family_conflict'})
            continue
        merged[key] = candidate
    enrichment = enrich_candidates(conn,list(merged.values()),'curation-discovery',reader_factory=reader_factory)
    result.update(enrichment)
    result['failures'] = failures+result['failures']
    result['status'] = 'incomplete' if result['failures'] else 'complete'
    return result

ADD_ADAPTER_EVENT = _event('AddAdapter',[('account','address',True)])


def historical_adapters(conn, parent, reader, *, from_block=None, to_block=None, chunk_size=50_000):
    from .coverage import Scope, fingerprint, missing_ranges, commit_range
    chain, address = parent['chain_id'],parent['address']
    end = int(reader.head['number']) if to_block is None else int(to_block)
    if end>int(reader.head['number']) or chunk_size<1:
        raise ValueError('invalid finalized adapter range')
    deployment = parent.get('deployment_block')
    if deployment is None:
        deployment = reader.deployment_block(address)
        with conn:
            conn.execute('UPDATE tvl_vaults SET deployment_block=? WHERE chain_id=? AND lower(address)=?',
                         (deployment,chain,address.lower()))
    start = max(deployment,from_block if from_block is not None else deployment)
    scope = Scope(chain,address,'tvl-adapter-membership',fingerprint(ADD_ADAPTER_EVENT))
    if start<=end:
        for lo,hi in missing_ranges(conn,scope,start,end,chunk_size,table='tvl_history_coverage'):
            logs = reader.logs(address,ADD_ADAPTER_EVENT,lo,hi)
            if len(logs)>=10_000:
                raise ValueError('adapter result cap; use smaller chunks')
            def write(db):
                for log in logs:
                    adapter = Web3.to_checksum_address(log['args']['account'])
                    header = reader.block(log['blockNumber'])
                    db.execute('''INSERT OR IGNORE INTO tvl_events_raw(chain_id,contract_address,event_name,
                        tx_hash,log_index,block_number,block_timestamp,decoded_json) VALUES (?,?,?,?,?,?,?,?)''',
                        (chain,address.lower(),'AddAdapter',Web3.to_hex(log['transactionHash']),
                         int(log['logIndex']),int(log['blockNumber']),int(header['timestamp']),
                         to_json({'account':adapter})))
                    from .tvl_sources import put_strategy
                    put_strategy(db,chain,address,adapter,'morpho-adapter-event')
                return len(logs)
            commit_range(conn,scope,lo,hi,source='rpc',end_hash=Web3.to_hex(reader.block(hi)['hash']),run_id=None,write=write,table='tvl_history_coverage')
    rows = conn.execute('''SELECT decoded_json,MIN(block_number) AS block_number FROM tvl_events_raw
        WHERE chain_id=? AND lower(contract_address)=? AND event_name='AddAdapter' AND block_number<=?
        GROUP BY decoded_json''',(chain,address.lower(),end))
    return {json.loads(r['decoded_json'])['account'].lower():r['block_number'] for r in rows}


def discover_adapter_relations(conn, *, chain_ids=None, reader_factory=DiscoveryReader,
                               from_block=None, to_block=None, chunk_size=50_000, progress=None):
    """Keep current and removed adapter candidates; collection values them at its own date."""
    from .tvl_sources import put_strategy
    result = {'source':'curation-relations','parents':0,'adapters':0,'nested':0,'market_adapters':0,
              'failures':[],'status':'complete'}
    readers = {}
    parents = [dict(r) for r in conn.execute("SELECT * FROM tvl_vaults WHERE version='morpho-v2'")
               if r['chain_id'] not in TVL_EXCLUDED_CHAIN_IDS and (chain_ids is None or r['chain_id'] in chain_ids)]
    for parent in parents:
        chain, address = parent['chain_id'],parent['address']
        result['parents'] += 1
        try:
            if chain not in readers:
                try:
                    readers[chain] = reader_factory(chain)
                except Exception as exc:
                    readers[chain] = type(exc).__name__
            reader = readers[chain]
            if isinstance(reader,str):
                raise RuntimeError('adapter RPC unavailable')
            current = {}
            try:
                count = reader.call(address,'adaptersLength','uint256')
                if not isinstance(count,int) or not 0<=count<=10_000:
                    raise ValueError('invalid adapter count')
            except Exception as exc:
                count = 0
                result['failures'].append({'chain_id':chain,'parent':address.lower(),'stage':'adapter-list',
                                           'reason':type(exc).__name__})
            for index in range(count):
                try:
                    adapter = reader.call(address,'adapters','address',(('uint256',index),))
                    if not Web3.is_address(adapter) or int(adapter,16)==0:
                        raise ValueError('invalid adapter address')
                    current[adapter.lower()] = int(reader.head['number'])
                except Exception as exc:
                    result['failures'].append({'chain_id':chain,'parent':address.lower(),'stage':'adapter-list',
                                               'index':index,'reason':type(exc).__name__})
            try:
                historical = historical_adapters(conn,parent,reader,from_block=from_block,
                                                  to_block=to_block,chunk_size=chunk_size)
                # Current endpoints take precedence; older removed adapters remain candidates.
                current = {**historical,**current}
            except Exception as exc:
                result['failures'].append({'chain_id':chain,'parent':address.lower(),'stage':'adapter-history',
                                           'reason':type(exc).__name__})
            # Also retry candidates persisted by earlier partial scans or discovery.
            for row in conn.execute("SELECT strategy FROM tvl_strategies WHERE chain_id=? AND parent=? AND source LIKE 'morpho-adapter%'",
                                    (chain,address.lower())):
                current.setdefault(row['strategy'],int(reader.head['number']))
            for adapter,block in current.items():
                result['adapters'] += 1
                child, status = None, 'unresolved'
                evidence = {'parent':address.lower(),'block_number':block}
                with conn:
                    put_strategy(conn,chain,address,adapter,'morpho-adapter')
                try:
                    owner = reader.call(adapter,'parentVault','address',block=block)
                    if owner.lower()!=address.lower():
                        raise ValueError('adapter parent mismatch')
                    # Explicit child-vault getters, never the ambiguous vault() getter.
                    for getter in ('morphoVaultV1','morphoVaultV2','innerVault'):
                        try:
                            target = reader.call(adapter,getter,'address',block=block)
                        except Exception:
                            continue
                        if not Web3.is_address(target) or int(target,16)==0 or target.lower()==address.lower():
                            raise ValueError('invalid child address')
                        metadata = reader.metadata(target,'erc4626',block=block)
                        if (metadata['asset'].lower()!=(parent['asset'] or '').lower()):
                            raise ValueError('adapter child asset mismatch')
                        child = target.lower()
                        evidence.update(getter=getter,child=child,child_metadata=metadata)
                        with conn:
                            conn.execute('INSERT OR IGNORE INTO tvl_targets VALUES (?,?,?,?)',
                                         (chain,adapter,child,'morpho-adapter'))
                        status = 'nested'
                        result['nested'] += 1
                        break
                    if child is None:
                        # The documented Morpho market adapter is a lending position,
                        # not another vault layer to add to protocol TVL.
                        morpho = reader.call(adapter,'morpho','address',block=block)
                        reader.call(adapter,'marketIdsLength','uint256',block=block)
                        if int(morpho,16)==0:
                            raise ValueError('invalid market adapter')
                        status = 'market'
                        result['market_adapters'] += 1
                except Exception as exc:
                    result['failures'].append({'chain_id':chain,'parent':address.lower(),'adapter':adapter,
                                               'stage':'adapter-mapping','reason':type(exc).__name__})
                    evidence['reason'] = type(exc).__name__
                # Keep link diagnostics separately from historical valuation evidence.
                with conn:
                    conn.execute('''INSERT INTO tvl_catalog_evidence VALUES (?,?,?,?,?,?)
                        ON CONFLICT(chain_id,address,source) DO UPDATE SET metadata_status=excluded.metadata_status,
                        evidence_json=excluded.evidence_json,updated_at=excluded.updated_at''',
                        (chain,adapter,'adapter:'+address.lower(),status,to_json(evidence),int(time.time())))
        except Exception as exc:
            result['failures'].append({'chain_id':chain,'parent':address.lower(),'stage':'adapter-list',
                                       'reason':type(exc).__name__})
        if progress:
            progress(f"Curation relationships chain {chain} parent {address}: checked")
    result['status'] = 'incomplete' if result['failures'] else 'complete'
    return result


def discover_catalog(conn, *, sources=None, chain_ids=None, from_block=None, to_block=None,
                     chunk_size=50_000, reader_factory=DiscoveryReader, request=morpho_request, progress=None):
    """One normal bootstrap command; each selected source has a retained outcome."""
    from .tvl_sources import refresh_kong_catalog, seed_targets
    requested = ('kong','v1','curation') if sources is None else tuple(dict.fromkeys(sources))
    if not requested or set(requested)-{'kong','v1','curation'}:
        raise ValueError('select at least one of kong, v1, curation')
    if chain_ids is not None and (not chain_ids or any(c<=0 for c in chain_ids)):
        raise ValueError('chain IDs must be positive')
    if chain_ids is not None and set(chain_ids)&TVL_EXCLUDED_CHAIN_IDS:
        raise ValueError('Sonic and Berachain are excluded from TVL scope')
    if chunk_size<1 or (from_block is not None and from_block<0) or (to_block is not None and to_block<0):
        raise ValueError('invalid discovery scan bounds')
    if from_block is not None and to_block is not None and from_block>to_block:
        raise ValueError('discovery from-block exceeds to-block')
    # Specific curation families/categories enrich the generic Kong inventory last.
    selected = [s for s in ('kong','v1','curation') if s in requested]
    params = {'sources':selected,'chain_ids':sorted(set(chain_ids)) if chain_ids is not None else None,
              'from_block':from_block,'to_block':to_block,'chunk_size':chunk_size,
              'excluded_chain_ids':sorted(TVL_EXCLUDED_CHAIN_IDS)}
    run_id = conn.execute("INSERT INTO tvl_discovery_runs(started_at,status,params_json) VALUES (?,'running',?)",
                          (int(time.time()),to_json(params))).lastrowid
    conn.commit()
    results = []
    try:
        for source in selected:
            try:
                if source=='kong':
                    result = refresh_kong_catalog(conn,chain_ids=chain_ids,with_summary=True)
                elif source=='v1':
                    result = discover_v1(conn,chain_ids=chain_ids,reader_factory=reader_factory)
                else:
                    result = discover_curation(conn,chain_ids=chain_ids,reader_factory=reader_factory,request=request,
                        from_block=from_block,to_block=to_block,chunk_size=chunk_size,progress=progress)
                results.append(result)
            except Exception as exc:
                conn.rollback()
                results.append({'source':source,'status':'incomplete','discovered':0,'enriched':0,
                                'failures':[{'stage':source,'reason':type(exc).__name__}]})
            if progress:
                progress(f"Discovery {source}: {results[-1]['status']}")
        if 'curation' in selected:
            results.append(discover_adapter_relations(conn,chain_ids=chain_ids,reader_factory=reader_factory,
                from_block=from_block,to_block=to_block,chunk_size=chunk_size,progress=progress))
        with conn:
            seed_targets(conn)
        status = 'complete' if all(r['status']=='complete' for r in results) else 'incomplete'
        catalog_count = sum(1 for r in conn.execute('SELECT chain_id FROM tvl_vaults')
                            if r['chain_id'] not in TVL_EXCLUDED_CHAIN_IDS and (chain_ids is None or r['chain_id'] in chain_ids))
        summary = {'run_id':run_id,'status':status,'catalog_vaults':catalog_count,'sources':results,
                   'scope':'configured registries, current API identities, and selected historical scan ranges'}
        conn.execute('UPDATE tvl_discovery_runs SET completed_at=?,status=?,summary_json=? WHERE id=?',
                     (int(time.time()),status,to_json(summary),run_id))
        conn.commit()
        return summary
    except BaseException:
        conn.rollback()
        conn.execute("UPDATE tvl_discovery_runs SET completed_at=?,status='failed' WHERE id=?",(int(time.time()),run_id))
        conn.commit()
        raise
