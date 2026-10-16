"""TVL inventory migration and archive-state acquisition, independent of fee ingestion."""
from __future__ import annotations

import json
import sqlite3
import time
from decimal import Decimal, localcontext
from pathlib import Path

import requests
from web3 import Web3

from .config import get_rpc_url_by_chain_id, TVL_EXCLUDED_CHAIN_IDS
from .pricing import fetch_yearn_price, fetch_defillama_price
from .storage import to_json
from .tvl import asset_units, decimal

REGISTRY = json.loads((Path(__file__).parent / 'inventories/tvl-overlaps.json').read_text())


def put_strategy(conn, chain_id, parent, strategy, source):
    conn.execute('INSERT OR IGNORE INTO tvl_strategies VALUES (?,?,?,?)',
                 (chain_id, parent.lower(), strategy.lower(), source))


def seed_targets(conn):
    for entry in REGISTRY['targets']:
        conn.execute('INSERT OR IGNORE INTO tvl_targets VALUES (?,?,?,?)',
                     (entry['chainId'], entry['strategyAddress'].lower(), entry['targetVaultAddress'].lower(), 'service-registry'))
        if entry.get('sourceVaultAddress'):
            put_strategy(conn, entry['chainId'], entry['sourceVaultAddress'], entry['strategyAddress'], 'service-router-registry')


def import_service_catalog(conn, path):
    """Read the legacy catalog once. Never copy latest balances into old dates."""
    source = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)
    source.row_factory = sqlite3.Row
    now = int(time.time())
    try:
        with conn:
            count = 0
            for row in source.execute('SELECT * FROM vaults'):
                address = Web3.to_checksum_address(row['address'])
                asset = Web3.to_checksum_address(row['asset_address']) if row['asset_address'] else None
                conn.execute('''INSERT INTO tvl_vaults (chain_id,version,address,asset,asset_symbol,
                    asset_decimals,name,api_version,management,protocol,active,updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(chain_id,address) DO UPDATE SET
                    asset=COALESCE(excluded.asset,tvl_vaults.asset),
                    asset_decimals=COALESCE(excluded.asset_decimals,tvl_vaults.asset_decimals),
                    asset_symbol=COALESCE(excluded.asset_symbol,tvl_vaults.asset_symbol),name=excluded.name''',
                    (row['chain_id'],row['category'],address,asset,row['asset_symbol'],row['asset_decimals'],
                     row['name'],row['api_version'],'yearn','curation' if row['category']=='curation' else None,
                     not row['is_retired'],now))
                conn.execute('UPDATE tvl_vaults SET tvl_category=? WHERE chain_id=? AND address=?',
                             (row['category'],row['chain_id'],address))
                count += 1
            for row in source.execute('''SELECT s.chain_id,s.address,s.name,v.category,v.address AS parent
                                        FROM strategies s JOIN vaults v ON v.id=s.vault_id'''):
                # The service stored the child as strategy.address for Morpho V2
                # and put the actual holding adapter in its name.
                prefix = 'Morpho V2 adapter '
                adapter = (row['name'] or '').removeprefix(prefix)
                if row['category']=='curation' and (row['name'] or '').startswith(prefix) and Web3.is_address(adapter):
                    conn.execute("DELETE FROM tvl_strategies WHERE chain_id=? AND parent=? AND strategy=? AND source='service-catalog'",
                                 (row['chain_id'],row['parent'].lower(),row['address'].lower()))
                    put_strategy(conn,row['chain_id'],row['parent'],adapter,'service-morpho-adapter')
                    conn.execute('INSERT OR IGNORE INTO tvl_targets VALUES (?,?,?,?)',
                                 (row['chain_id'],adapter.lower(),row['address'].lower(),'service-morpho-adapter'))
                else:
                    put_strategy(conn,row['chain_id'],row['parent'],row['address'],'service-catalog')
            seed_targets(conn)
        return count
    finally:
        source.close()


def sync_strategies(conn):
    """Include old/revoked strategies seen in locally imported historical events."""
    for table in ('strategy_reports', 'strategy_debt_flows'):
        for row in conn.execute(f'SELECT DISTINCT chain_id,vault_address,strategy_address FROM {table}'):
            if row['strategy_address'].lower() != row['vault_address'].lower():
                put_strategy(conn,row['chain_id'],row['vault_address'],row['strategy_address'],'local-history')
    for table in ('events_raw','tvl_events_raw'):
        for row in conn.execute(f"SELECT chain_id,contract_address,decoded_json FROM {table} WHERE event_name IN ('StrategyAdded','StrategyChanged','StrategyMigrated','StrategyRevoked')"):
            data = json.loads(row['decoded_json'])
            for name in ('strategy','newVersion','oldVersion'):
                if data.get(name):
                    put_strategy(conn,row['chain_id'],row['contract_address'],data[name],'local-history')
    seed_targets(conn)
    conn.commit()


def refresh_kong_catalog(conn, *, chain_ids=None, with_summary=False):
    """Refresh V2/V3 inventory and strategy candidates with explicit pagination."""
    from .config import get_kong_graphql_url
    query = '''query($skip:Int!,$first:Int!,$chainId:Int){vaults(yearn:true,chainId:$chainId,offset:$skip,limit:$first){
        address chainId name apiVersion v3 asset{address symbol decimals} strategies meta{isRetired}}}'''
    seen = set()
    failures = []
    now = int(time.time())
    for chain in (sorted(set(chain_ids)-TVL_EXCLUDED_CHAIN_IDS) if chain_ids is not None else [None]):
        skip = 0
        while True:
            try:
                response = requests.post(get_kong_graphql_url(), json={'query':query,'variables':{'skip':skip,'first':100,'chainId':chain}}, timeout=30)
                response.raise_for_status()
                body = response.json()
                if body.get('errors'):
                    raise RuntimeError(f"Kong inventory errors: {body['errors']}")
                rows = body['data']['vaults']
                page_seen = set()
                page_failures = []
                with conn:
                    for row in rows:
                        key = (int(row['chainId']),row['address'].lower())
                        if key in seen or key in page_seen:
                            raise RuntimeError('Kong pagination repeated a vault')
                        page_seen.add(key)
                        if chain is not None and key[0]!=chain:
                            raise ValueError('Kong chain scope mismatch')
                        if key[0] in TVL_EXCLUDED_CHAIN_IDS:
                            continue
                        asset = row.get('asset') or {}
                        complete = bool(asset.get('address')) and asset.get('decimals') is not None
                        if not complete:
                            page_failures.append({'chain_id':key[0],'address':key[1],'stage':'metadata','reason':'missing_asset_metadata'})
                        conn.execute('''INSERT INTO tvl_catalog_evidence VALUES (?,?,?,?,?,?)
                            ON CONFLICT(chain_id,address,source) DO UPDATE SET metadata_status=excluded.metadata_status,
                            evidence_json=excluded.evidence_json,updated_at=excluded.updated_at''',
                            (key[0],key[1],'kong','ok' if complete else 'unavailable',to_json({'source':'kong'}),now))
                        conn.execute('''INSERT INTO tvl_vaults (chain_id,version,address,asset,asset_symbol,asset_decimals,
                            name,api_version,active,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)
                            ON CONFLICT(chain_id,address) DO UPDATE SET version=CASE WHEN tvl_vaults.version IN ('morpho-v1','morpho-v2') THEN tvl_vaults.version ELSE excluded.version END,asset=COALESCE(excluded.asset,tvl_vaults.asset),
                            asset_decimals=COALESCE(excluded.asset_decimals,tvl_vaults.asset_decimals),
                            asset_symbol=COALESCE(excluded.asset_symbol,tvl_vaults.asset_symbol),
                            name=excluded.name,api_version=excluded.api_version,active=excluded.active,updated_at=excluded.updated_at''',
                            (key[0],'v3' if row['v3'] else 'v2',Web3.to_checksum_address(row['address']),
                             Web3.to_checksum_address(asset['address']) if asset.get('address') else None,
                             asset.get('symbol'),asset.get('decimals'),row.get('name'),row.get('apiVersion'),
                             not (row.get('meta') or {}).get('isRetired',False),now))
                        for strategy in row.get('strategies') or []:
                            put_strategy(conn,key[0],key[1],strategy,'kong')
                seen.update(page_seen)
                failures.extend(page_failures)
                skip += len(rows)
                if len(rows) < 100:
                    break
            except Exception as exc:
                if not with_summary:
                    raise
                failures.append({'chain_id':chain,'stage':'pagination','offset':skip,'reason':type(exc).__name__})
                break
    with conn:
        seed_targets(conn)
    count = sum(chain not in TVL_EXCLUDED_CHAIN_IDS for chain,_ in seen)
    if with_summary:
        return {'source':'kong','discovered':count,'enriched':count-sum(f['stage']=='metadata' for f in failures),
                'failures':failures,'status':'incomplete' if failures else 'complete'}
    return count


class ArchiveReader:
    """Use one historical block per chain/date and memoize reads within a run."""
    def __init__(self, chain_id):
        url = get_rpc_url_by_chain_id(chain_id)
        self.w3 = Web3(Web3.HTTPProvider(url.split(',')[0],request_kwargs={'timeout':30}))
        if chain_id != 1:
            from web3.middleware import ExtraDataToPOAMiddleware
            self.w3.middleware_onion.inject(ExtraDataToPOAMiddleware,layer=0)
        if int(self.w3.eth.chain_id) != chain_id:
            raise ValueError('RPC chain mismatch')
        self.cache = {}
        self.blocks = {}
        # Fix the boundary for this acquisition, never sample a moving latest block.
        # Opera publishes finalized main-chain blocks; its RPC has no Ethereum
        # 'finalized' tag. https://docs.fantom.foundation/technology/faq
        self.finality_policy = 'opera-bft' if chain_id == 250 else 'rpc-finalized'
        self.head = self.w3.eth.get_block('latest' if chain_id == 250 else 'finalized')

    def block_at(self, timestamp):
        if timestamp > int(self.head['timestamp']):
            raise ValueError('requested timestamp is newer than finalized chain state')
        if timestamp in self.blocks:
            return self.blocks[timestamp]
        lo, hi = 0, int(self.head['number'])
        if int(self.w3.eth.get_block(lo)['timestamp']) > timestamp:
            raise ValueError('requested timestamp predates chain genesis')
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if int(self.w3.eth.get_block(mid)['timestamp']) <= timestamp:
                lo = mid
            else:
                hi = mid - 1
        self.blocks[timestamp] = lo
        return lo

    def exists(self,address,block):
        key = ('code',address.lower(),block)
        if key not in self.cache:
            self.cache[key] = bool(self.w3.eth.get_code(Web3.to_checksum_address(address),block_identifier=block))
        return self.cache[key]

    def words(self,address,signature,block,arg=None):
        data = Web3.keccak(text=signature)[:4].hex()
        if not data.startswith('0x'):
            data = '0x' + data
        if arg is not None:
            data += arg.lower().removeprefix('0x').rjust(64,'0')
        key = (address.lower(),data,block)
        if key not in self.cache:
            raw = bytes(self.w3.eth.call({'to':Web3.to_checksum_address(address),'data':data},block_identifier=block))
            if not raw or len(raw)%32:
                raise ValueError('invalid RPC return data')
            self.cache[key] = [int.from_bytes(raw[i:i+32],'big') for i in range(0,len(raw),32)]
        return self.cache[key]

    def uint(self,address,signature,block,arg=None):
        words = self.words(address,signature,block,arg)
        # Early Yearn forwarders return a zero-padded 4096-byte buffer.
        # Decode the declared ABI value and reject nonzero trailing data.
        if not words or any(words[1:]):
            raise ValueError(f'invalid scalar result for {signature}')
        return words[0]

    def debt(self,parent,strategy,version,block):
        if version in ('curation','morpho-v2'):
            # Morpho V2 adapters hold child shares and report their realAssets.
            # Membership is verified at the same block, never inferred from a label.
            if not self.uint(parent,'isAdapter(address)',block,strategy):
                return 0
            return self.uint(strategy,'realAssets()',block)
        words = self.words(parent,'strategies(address)',block,strategy)
        # V2 StrategyParams: performanceFee,activation,debtRatio,min,max,lastReport,totalDebt,totalGain,totalLoss.
        # V3 StrategyParams: activation,last_report,current_debt,max_debt.
        expected, index = (9,6) if version == 'v2' else (4,2)
        if version not in ('v2','v3') or len(words) < expected or any(words[expected:]):
            raise ValueError('unsupported strategies(address) layout')
        return words[index]


def _snapshot(vault,timestamp,reader,price_cache,price_source):
    data = {k:vault[k] for k in ('chain_id','version','asset','asset_decimals','name')}
    data['category'] = vault.get('tvl_category') or vault['version']
    data.update(vault=vault['address'].lower(),timestamp=timestamp,block_number=None,
                total_assets_raw=None,total_supply_raw=None,asset_units=None,price_usd=None,
                tvl_usd=None,status='unavailable',source='archive-rpc')
    for bridge in REGISTRY.get('bridges', []):
        if bridge['sourceChainId'] == vault['chain_id'] and bridge['sourceVaultAddress'].lower() == data['vault']:
            data['bridge_target_chain'] = bridge['targetChainId']
    try:
        block = reader.block_at(timestamp)
        data['block_number'] = block
        if not reader.exists(data['vault'],block):
            data.update(total_assets_raw='0',total_supply_raw='0',asset_units='0',tvl_usd='0',status='not_deployed')
            return data
        supply = reader.uint(data['vault'],'totalSupply()',block)
        raw = ((supply * reader.uint(data['vault'],'getPricePerFullShare()',block) // 10**18 if supply else 0)
               if vault['version']=='v1' else reader.uint(data['vault'],'totalAssets()',block))
        data.update(total_assets_raw=str(raw),total_supply_raw=str(supply))
        units = asset_units(raw,vault['asset_decimals'])
        data['asset_units'] = str(units)
        if raw == 0:
            data.update(tvl_usd='0',status='ok')
            return data
        if not vault['asset']:
            raise ValueError('missing asset identity')
        key = (vault['chain_id'],vault['asset'].lower(),timestamp)
        if key not in price_cache:
            fn = fetch_yearn_price if price_source=='yearn-prices' else fetch_defillama_price
            price_cache[key] = fn(*key)
        price, status, evidence = price_cache[key]
        data['pricing'] = {'source':price_source,'status':status,'evidence':evidence}
        if status != 'ok' or price is None:
            data['status'] = status if status != 'ok' else 'unavailable'
            data['reason'] = 'missing_price'
        else:
            with localcontext() as ctx:
                ctx.prec = 78
                data.update(price_usd=str(decimal(price)),tvl_usd=str(units*decimal(price)),status='ok')
    except Exception as exc:
        # Do not retain fabricated success after a partial read or pricing failure.
        data.update(tvl_usd=None,status='unavailable',reason=type(exc).__name__)
    return data


def candidate_edges(conn,vaults):
    by_key = {(v['chain_id'],v['address'].lower()):v for v in vaults}
    catalog_keys = {(r['chain_id'],r['address'].lower()) for r in conn.execute('SELECT chain_id,address FROM tvl_vaults')}
    edges = {}
    adapter_status = {(r['chain_id'],r['address'],r['source'].removeprefix('adapter:')):r['metadata_status']
                      for r in conn.execute("SELECT * FROM tvl_catalog_evidence WHERE source LIKE 'adapter:%'")}
    targets = defaultdict_targets(conn)
    for row in conn.execute('SELECT * FROM tvl_strategies'):
        chain, parent, strategy = row['chain_id'],row['parent'],row['strategy']
        if (chain,parent) not in by_key:
            continue
        children = [(strategy,'direct-strategy')] if (chain,strategy) in catalog_keys else []
        if (chain,strategy) not in catalog_keys:
            children += targets.get((chain,strategy),[])
        if not children:
            edges[(chain,parent,strategy,strategy)] = {'chain_id':chain,'parent':parent,'strategy':strategy,'child':strategy,'method':'strategy-allocation',
                'mapping_status':adapter_status.get((chain,strategy,parent),'no_known_child')}
        for child, method in children:
            if parent != child:
                edges[(chain,parent,strategy,child)] = {'chain_id':chain,'parent':parent,'strategy':strategy,'child':child,'method':method}
    # When the holder is itself a tracked compounder, its child position belongs
    # to that compounder. Keep P -> S -> C rather than skipping the middle layer.
    for (chain,holder),children in targets.items():
        if (chain,holder) not in by_key:
            continue
        for child,source in children:
            if (chain,child) in catalog_keys and child != holder:
                edges[(chain,holder,holder,child)] = {'chain_id':chain,'parent':holder,'strategy':holder,
                                                     'child':child,'method':'vault-held-shares','mapping_source':source}
    # ERC4626 wrappers whose asset is another tracked vault share token.
    for (chain,parent),v in by_key.items():
        child = (v['asset'] or '').lower()
        if (chain,child) in catalog_keys and child != parent:
            edges[(chain,parent,parent,child)] = {'chain_id':chain,'parent':parent,'strategy':parent,'child':child,'method':'vault-asset'}
    return list(edges.values())


def defaultdict_targets(conn):
    targets = {}
    for row in conn.execute('SELECT * FROM tvl_targets'):
        targets.setdefault((row['chain_id'],row['strategy']),[]).append((row['child'],row['source']))
    return targets


def _position(edge,timestamp,reader,snapshots):
    data = dict(edge,timestamp=timestamp,status='unavailable',debt_raw=None,debt_usd=None,
                owned_usd=None,balance_raw=None,ownership_ratio=None)
    parent = snapshots[(edge['chain_id'],edge['parent'])]
    child = snapshots.get((edge['chain_id'],edge['child']))
    data['block_number'] = parent['block_number']
    try:
        block = parent['block_number']
        if parent['status']=='not_deployed' or (child is not None and child['status']=='not_deployed'):
            data.update(status='ok',debt_usd='0',owned_usd='0',ownership_ratio='0')
            return data
        if block is None:
            raise ValueError('missing historical block')
        debt = (int(parent['total_assets_raw']) if edge['method'] in ('vault-asset','vault-held-shares')
                else reader.debt(edge['parent'],edge['strategy'],parent['version'],block))
        data['debt_raw'] = str(debt)
        if debt == 0:
            data.update(status='ok',debt_usd='0',owned_usd='0',ownership_ratio='0')
            return data
        if edge['method']=='vault-asset' and child is None:
            raise ValueError('nested-share child must be selected for valuation')
        if edge['method']=='vault-asset':
            # Parent accounting is in child shares; do not recursively call its price API.
            supply = int(child['total_supply_raw'])
            balance = reader.uint(edge['child'],'balanceOf(address)',block,edge['parent'])
            with localcontext() as ctx:
                ctx.prec=78
                ratio = Decimal(min(debt,balance))/Decimal(supply) if supply else Decimal(0)
                data.update(debt_usd=str(decimal(child['tvl_usd'])*ratio),owned_usd=str(decimal(child['tvl_usd'])*ratio),
                            balance_raw=str(balance),ownership_ratio=str(ratio),status='ok')
            return data
        if parent['price_usd'] is None:
            raise ValueError('missing parent asset price')
        with localcontext() as ctx:
            ctx.prec=78
            debt_usd = asset_units(debt,parent['asset_decimals'])*decimal(parent['price_usd'])
            data['debt_usd'] = str(debt_usd)
            if edge['method']=='strategy-allocation':
                if edge.get('mapping_status')=='unresolved':
                    data.update(status='unavailable',reason='unresolved_adapter_mapping')
                else:
                    data.update(status='ok',mapping_status='no_known_child')
            elif child is None:
                data.update(status='ok',mapping_status='child_outside_selection')
            elif edge['method']=='direct-strategy':
                # Tokenized Strategy shares need not be held by the allocator.
                if (parent['asset'] or '').lower() != (child['asset'] or '').lower():
                    raise ValueError('direct strategy underlying mismatch')
                data.update(owned_usd=child['tvl_usd'],status='ok')
            else:
                balance = reader.uint(edge['child'],'balanceOf(address)',block,edge['strategy'])
                supply = int(child['total_supply_raw'])
                if balance > supply or (balance and not supply):
                    raise ValueError('invalid child ownership')
                ratio = Decimal(balance)/Decimal(supply) if supply else Decimal(0)
                data.update(balance_raw=str(balance),ownership_ratio=str(ratio),
                            owned_usd=str(decimal(child['tvl_usd'])*ratio) if ratio else '0',status='ok')
    except Exception as exc:
        data.update(status='unavailable',reason=type(exc).__name__)
    return data


def _value_nested_shares(points):
    """Resolve tracked share-token assets only after their children are valued."""
    pending = {key:(key[0],(p['asset'] or '').lower()) for key,p in points.items()
               if (key[0],(p['asset'] or '').lower()) in points
               and p['status'] != 'not_deployed' and p['total_assets_raw'] != '0'}
    for key,child_key in pending.items():
        p = points[key]
        # Provider share-token quotes are provisional: unknown child state must
        # not leave a seemingly complete parent valuation behind.
        p.update(tvl_usd=None,price_usd=None)
        if p['total_assets_raw'] is not None:
            p.update(status='unavailable',reason='missing_nested_share_valuation',
                     pricing={'source':'nested-share','child':points[child_key]['vault']})
    while pending:
        ready = [key for key,child in pending.items() if child not in pending]
        if not ready:
            # Cycles have no independently valued leaf. Leave them unknown.
            break
        for key in ready:
            child = points[pending.pop(key)]
            p = points[key]
            supply = int(child['total_supply_raw'] or 0)
            if child['tvl_usd'] is None or p['total_assets_raw'] is None or p['asset_units'] is None or not supply:
                continue
            with localcontext() as ctx:
                ctx.prec = 78
                value = decimal(child['tvl_usd'])*Decimal(p['total_assets_raw'])/Decimal(supply)
                units = decimal(p['asset_units'])
                p.update(tvl_usd=str(value),price_usd=str(value/units) if units else None,
                         status='ok',pricing={'source':'nested-share','child':child['vault']})
                p.pop('reason',None)


def collect_tvl(conn,from_timestamp,to_timestamp,*,interval=86400,chain_ids=None,addresses=None,
                price_source='yearn-prices',reader_factory=ArchiveReader,progress=None):
    if from_timestamp < 0 or from_timestamp > to_timestamp or interval < 1:
        raise ValueError('invalid TVL time range or interval')
    if price_source == 'yearn-prices' and (from_timestamp % 86400 != 86399 or interval % 86400):
        raise ValueError('Yearn Prices TVL samples must use UTC day-end timestamps and whole-day intervals')
    if price_source not in ('yearn-prices','defillama'):
        raise ValueError('unsupported TVL price source')
    vaults = [dict(r) for r in conn.execute('SELECT * FROM tvl_vaults ORDER BY chain_id,address')
              if r['chain_id'] not in TVL_EXCLUDED_CHAIN_IDS
              and (not chain_ids or r['chain_id'] in chain_ids)
              and (not addresses or r['address'].lower() in {a.lower() for a in addresses})]
    if not vaults:
        raise ValueError('empty TVL inventory selection; import or discover vaults first')
    sync_strategies(conn)
    edges = candidate_edges(conn,vaults)
    params = {'from':from_timestamp,'to':to_timestamp,'interval':interval,'chain_ids':sorted({v['chain_id'] for v in vaults}),
              'addresses':sorted(addresses) if addresses else None,'price_source':price_source,
              'vault_count':len(vaults),'mapping_scope':'known candidates, not exhaustive',
              'bridge_exclusions':'none; effective migration dates required for historical deductions',
              'finality_policy':{str(v['chain_id']):('opera-bft' if v['chain_id']==250 else 'rpc-finalized') for v in vaults}}
    run_id = conn.execute("INSERT INTO tvl_runs(started_at,status,params_json) VALUES (?,'running',?)",(int(time.time()),to_json(params))).lastrowid
    conn.commit()
    readers, price_cache, reader_errors = {}, {}, {}
    incomplete = False
    try:
        for timestamp in range(from_timestamp,to_timestamp+1,interval):
            points = {}
            for vault in vaults:
                chain = vault['chain_id']
                if chain not in readers:
                    try:
                        readers[chain] = reader_factory(chain)
                    except Exception as exc:
                        readers[chain] = None
                        reader_errors[chain] = type(exc).__name__
                point = _snapshot(vault,timestamp,readers[chain],price_cache,price_source)
                if chain in reader_errors:
                    point.update(reason='archive_rpc_setup_failed',failure_class=reader_errors[chain])
                points[(chain,vault['address'].lower())] = point
            _value_nested_shares(points)
            position_rows = [_position(e,timestamp,readers[e['chain_id']],points) for e in edges]
            with conn:
                for p in points.values():
                    conn.execute('INSERT INTO tvl_snapshots VALUES (?,?,?,?,?,?)',
                                 (run_id,p['chain_id'],p['vault'],timestamp,p['block_number'],to_json(p)))
                for p in position_rows:
                    conn.execute('INSERT INTO tvl_positions VALUES (?,?,?,?,?,?,?)',
                                 (run_id,p['chain_id'],p['parent'],p['strategy'],p['child'],timestamp,to_json(p)))
            incomplete |= any(p['tvl_usd'] is None for p in points.values()) or any(p['status']!='ok' for p in position_rows)
            if progress:
                progress(f'TVL run {run_id}: stored {len(points)} vaults and {len(position_rows)} positions at {timestamp}')
        conn.execute('UPDATE tvl_runs SET completed_at=?,status=? WHERE id=?',
                     (int(time.time()),'incomplete' if incomplete else 'complete',run_id))
        conn.commit()
    except BaseException:
        conn.execute("UPDATE tvl_runs SET completed_at=?,status='failed' WHERE id=?",(int(time.time()),run_id))
        conn.commit()
        raise
    return run_id


def scan_holders(conn,timestamp,*,chain_ids=None,reader_factory=ArchiveReader,progress=None):
    """Discover intermediary targets at an explicit date. Historical scans retain old paths.

    Scans can be expensive; failed balance reads are reported rather than treated as zero.
    Every discovered relation is still re-valued at each collection date.
    """
    sync_strategies(conn)
    vaults = [dict(v) for v in conn.execute('SELECT * FROM tvl_vaults') if v['chain_id'] not in TVL_EXCLUDED_CHAIN_IDS and (not chain_ids or v['chain_id'] in chain_ids)]
    holders = {(r['chain_id'],r['strategy']) for r in conn.execute('SELECT * FROM tvl_strategies')
               if r['chain_id'] not in TVL_EXCLUDED_CHAIN_IDS and (not chain_ids or r['chain_id'] in chain_ids)}
    holders.update((v['chain_id'],v['address'].lower()) for v in vaults)
    strategies = sorted(holders)
    readers = {}
    found = failed = checked = 0
    for chain,strategy in strategies:
        if chain not in readers:
            readers[chain] = reader_factory(chain)
        reader = readers[chain]
        block = reader.block_at(timestamp)
        for child in vaults:
            if child['chain_id'] != chain or child['address'].lower() == strategy:
                continue
            try:
                balance = reader.uint(child['address'],'balanceOf(address)',block,strategy) if reader.exists(child['address'],block) else 0
            except Exception:
                failed += 1
                continue
            checked += 1
            if balance:
                with conn:
                    conn.execute('INSERT OR IGNORE INTO tvl_targets VALUES (?,?,?,?)',
                                 (chain,strategy,child['address'].lower(),f'share-scan:{timestamp}'))
                found += 1
        if progress:
            progress(f'TVL mapping: chain {chain}, checked holder {strategy}')
    return {'positive_holdings':found,'checked':checked,'failed_reads':failed,'timestamp':timestamp}
