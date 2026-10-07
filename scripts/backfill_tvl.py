"""Run daily TVL in restartable monthly batches, using the existing accounting.

Only transport and checkpoints live here. No balance, valuation, or overlap
formula is duplicated. Run with --help; progress is written beside the exports.
"""
from __future__ import annotations

import argparse
import calendar
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import sys
import time

import requests
from web3 import Web3

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from yearn_data.config import load_environment, TVL_EXCLUDED_CHAIN_IDS
from yearn_data.multicall import MULTICALL3, MULTICALL3_ABI
from yearn_data.pricing import fetch_yearn_price, fetch_yearn_prices_batch, yearn_prices_token_key
from yearn_data.storage import connect, init_db
from yearn_data.tvl import export_tvl
from yearn_data import tvl_sources as sources

UTC = timezone.utc
SCHEMA = '''
CREATE TABLE IF NOT EXISTS tvl_backfill_batches (
 chain_id INTEGER, from_timestamp INTEGER, to_timestamp INTEGER, run_id INTEGER,
 status TEXT NOT NULL, PRIMARY KEY(chain_id,from_timestamp,to_timestamp));
CREATE TABLE IF NOT EXISTS tvl_backfill_births (
 chain_id INTEGER, address TEXT, block_number INTEGER, timestamp INTEGER,
 status TEXT, reason TEXT, PRIMARY KEY(chain_id,address));
CREATE TABLE IF NOT EXISTS tvl_backfill_blocks (
 chain_id INTEGER, timestamp INTEGER, block_number INTEGER,
 PRIMARY KEY(chain_id,timestamp));
'''


def atomic_json(path, value):
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value, indent=2) + '\n')
    temp.replace(path)


def call_data(signature, arg=None):
    data = '0x' + bytes(Web3.keccak(text=signature)[:4]).hex()
    return data if arg is None else data + arg.lower().removeprefix('0x').rjust(64, '0')


class Prices:
    def __init__(self, conn):
        self.conn = conn
        self.cache = {}

    def get(self, chain, token, timestamp):
        key = (chain, token.lower(), timestamp)
        if key not in self.cache:
            self.prime([key])
        return self.cache[key]

    def prime(self, keys):
        pending = []
        for key in sorted(set(keys)):
            if key in self.cache:
                continue
            row = self.conn.execute("SELECT * FROM prices WHERE chain_id=? AND token_address=? AND timestamp=? AND source='yearn-prices'", key).fetchone()
            if row and row['status'] not in ('retryable', 'invalid', 'unavailable'):
                self.cache[key] = (row['price_usd'], row['status'], json.loads(row['raw_json']))
            else:
                pending.append(key)
        results = {}
        def batch(chunk):
            try:
                return fetch_yearn_prices_batch(chunk, timeout=30)
            except Exception:
                return {}  # Exact lookup still verifies every omitted target.
        with ThreadPoolExecutor(max_workers=4) as pool:
            for found in pool.map(batch, [pending[i:i+50] for i in range(0,len(pending),50)]):
                results.update(found)
            absent = [key for key in pending if key not in results]
            def exact(key):
                try:
                    return key, fetch_yearn_price(*key, timeout=30)
                except Exception as exc:
                    return key, (None, 'unavailable', {'failure_class':type(exc).__name__,
                        'failure_reason':str(exc) if isinstance(exc,ValueError) else type(exc).__name__})
            results.update(pool.map(exact, absent))
        with self.conn:
            for key, result in results.items():
                self.cache[key] = result
                price, status, evidence = result
                self.conn.execute('''INSERT OR REPLACE INTO prices
                    (chain_id,token_address,timestamp,source,price_usd,status,raw_json)
                    VALUES (?,?,?,'yearn-prices',?,?,?)''', (*key,price,status,json.dumps(evidence)))


class BatchedReader(sources.ArchiveReader):
    def __init__(self, chain, conn, head, vaults, edges, births, prices):
        super().__init__(chain)
        if int(self.w3.eth.chain_id) != chain:
            raise ValueError('RPC chain mismatch')
        pinned = self.w3.eth.get_block(head['head_block'])
        if bytes(pinned['hash']).hex() != head['head_hash']:
            raise ValueError('pinned finalized block hash changed')
        self.head = pinned
        self.chain, self.conn = chain, conn
        self.vaults, self.edges, self.births, self.prices = vaults, edges, births, prices
        self.prepared = None
        self.multicall = self.w3.eth.contract(address=Web3.to_checksum_address(MULTICALL3), abi=MULTICALL3_ABI)

    def exists(self, address, block):
        birth = self.births.get(address.lower())
        if birth is not None:
            return block >= birth
        return super().exists(address, block)

    def words(self, address, signature, block, arg=None):
        key = (address.lower(),call_data(signature,arg),block)
        result = self.cache.get(key)
        if isinstance(result, Exception):
            raise result
        return super().words(address,signature,block,arg)

    def transport(self, calls, block, use_multicall):
        if use_multicall:
            payload = [(Web3.to_checksum_address(a),True,bytes.fromhex(d[2:])) for a,d in calls]
            try:
                rows = self.multicall.functions.aggregate3(payload).call(block_identifier=block)
                if len(rows) != len(calls):
                    raise ValueError('multicall response length mismatch')
                # Use direct calls for failures, preserving the native call context.
                bad = [c for c,(ok,raw) in zip(calls,rows) if not ok or not raw]
                fallback = self.transport(bad,block,False) if bad else {}
                return {c:(bytes(raw) if ok and raw else fallback[c]) for c,(ok,raw) in zip(calls,rows)}
            except Exception:
                pass
        payload = [{'jsonrpc':'2.0','id':i,'method':'eth_call','params':[{'to':a,'data':d},hex(block)]} for i,(a,d) in enumerate(calls)]
        for attempt in range(3):
            try:
                response = requests.post(self.w3.provider.endpoint_uri,json=payload,timeout=60)
                response.raise_for_status()
                rows = response.json()
                if not isinstance(rows,list) or len(rows)!=len(calls):
                    raise ValueError('RPC batch response length mismatch')
                indexed = {r['id']:r for r in rows}
                if set(indexed)!=set(range(len(calls))):
                    raise ValueError('RPC batch response IDs mismatch')
                return {c:(bytes.fromhex(indexed[i]['result'].removeprefix('0x')) if 'result' in indexed[i]
                           else ValueError('historical RPC call failed')) for i,c in enumerate(calls)}
            except Exception as exc:
                if attempt==2:
                    return {c:ValueError(type(exc).__name__) for c in calls}
                time.sleep(attempt+1)

    def block_at(self, timestamp):
        if timestamp > int(self.head['timestamp']):
            raise ValueError('requested timestamp newer than pinned finalized head')
        row = self.conn.execute('SELECT block_number FROM tvl_backfill_blocks WHERE chain_id=? AND timestamp=?',(self.chain,timestamp)).fetchone()
        block = row['block_number'] if row else super().block_at(timestamp)
        if not row:
            with self.conn:
                self.conn.execute('INSERT INTO tvl_backfill_blocks VALUES (?,?,?)',(self.chain,timestamp,block))
        if self.prepared == timestamp:
            return block
        self.prepared = timestamp
        self.cache.clear()
        calls = set()
        active = {v['address'].lower():v for v in self.vaults if self.exists(v['address'],block)}
        for address,v in active.items():
            calls.add((address,call_data('totalSupply()')))
            calls.add((address,call_data('getPricePerFullShare()' if v['version']=='v1' else 'totalAssets()')))
        for e in self.edges:
            if e['parent'] not in active:
                continue
            v = active[e['parent']]
            if e['method'] not in ('vault-asset','vault-held-shares'):
                if v['version'] in ('morpho-v2','curation'):
                    calls.add((e['parent'],call_data('isAdapter(address)',e['strategy'])))
                    calls.add((e['strategy'],call_data('realAssets()')))
                elif v['version'] in ('v2','v3'):
                    calls.add((e['parent'],call_data('strategies(address)',e['strategy'])))
            if e['method'] not in ('direct-strategy','strategy-allocation') and e['child'] in active:
                calls.add((e['child'],call_data('balanceOf(address)',e['strategy'])))
        use_multicall = bool(self.w3.eth.get_code(Web3.to_checksum_address(MULTICALL3),block_identifier=block))
        ordered = sorted(calls)
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(self.transport,ordered[i:i+150],block,use_multicall) for i in range(0,len(ordered),150)]
            for f in as_completed(futures):
                for (a,d),raw in f.result().items():
                    if isinstance(raw,bytes) and raw and len(raw)%32==0:
                        self.cache[a,d,block] = [int.from_bytes(raw[i:i+32],'big') for i in range(0,len(raw),32)]
                    else:
                        self.cache[a,d,block] = ValueError('invalid historical RPC result')
        keys = []
        for a,v in active.items():
            try:
                raw = self.uint(a,'getPricePerFullShare()' if v['version']=='v1' else 'totalAssets()',block)
            except Exception:
                continue
            if raw>0 and v['asset']:
                keys.append((self.chain,v['asset'].lower(),timestamp))
        self.prices.prime(keys)
        return block


def deployment(reader, vault):
    address = vault['address'].lower()
    known = vault['deployment_block']
    if known and reader.exists(address,known) and not reader.exists(address,known-1):
        block = known
    else:
        if not reader.exists(address,int(reader.head['number'])):
            raise ValueError('no contract at finalized head')
        lo,hi = 0,int(reader.head['number'])
        while lo<hi:
            mid = (lo+hi)//2
            if reader.exists(address,mid): hi=mid
            else: lo=mid+1
        block = lo
    return block,int(reader.w3.eth.get_block(block)['timestamp'])


def monthly_ranges(start,end):
    cursor = datetime.fromtimestamp(end,UTC).replace(day=1,hour=0,minute=0,second=0)
    while int(cursor.timestamp()) <= end:
        last = cursor.replace(day=calendar.monthrange(cursor.year,cursor.month)[1],hour=23,minute=59,second=59)
        lo,hi = max(start,int(cursor.timestamp())+86399),min(end,int(last.timestamp()))
        if lo<=hi: yield lo,hi
        year,month = (cursor.year-1,12) if cursor.month==1 else (cursor.year,cursor.month-1)
        cursor = cursor.replace(year=year,month=month)
        if int(last.timestamp())<start: break


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--db',type=Path,required=True)
    p.add_argument('--heads',type=Path,required=True,help='Verified finalized-head preflight JSON')
    p.add_argument('--env',type=Path,required=True)
    p.add_argument('--out',type=Path,required=True)
    p.add_argument('--from-date',default='2020-01-01')
    p.add_argument('--to-date',required=True)
    p.add_argument('--limit-batches',type=int)
    args=p.parse_args()
    args.out.mkdir(parents=True,exist_ok=True)
    lock=(args.db.with_suffix('.backfill.lock')).open('w')
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    load_environment([args.env])
    conn=connect(args.db);init_db(conn)
    conn.execute('PRAGMA journal_mode=WAL');conn.execute('PRAGMA busy_timeout=60000');conn.executescript(SCHEMA)
    heads={r['chain_id']:r for r in json.loads(args.heads.read_text())}
    start=int(datetime.strptime(args.from_date,'%Y-%m-%d').replace(tzinfo=UTC).timestamp())+86399
    end=int(datetime.strptime(args.to_date,'%Y-%m-%d').replace(tzinfo=UTC).timestamp())+86399
    state={'phase':'preparing','pid':os.getpid(),'from':args.from_date,'through':args.to_date,
           'interval':'daily UTC close','price_source':'yearn-prices','db':str(args.db.resolve()),
           'scope':'known catalog; known candidate overlap mappings; curation reported separately',
           'excluded_chain_ids':sorted(TVL_EXCLUDED_CHAIN_IDS),
           'pending_chains':[], 'price_limits':[], 'completed_batches':0}
    def publish(**values):
        state.update(values,updated_at=int(time.time()))
        state['completed_batches']=conn.execute("SELECT count(*) FROM tvl_backfill_batches WHERE status IN ('complete','incomplete')").fetchone()[0]
        state['snapshots']=conn.execute('SELECT count(*) FROM tvl_snapshots s JOIN tvl_backfill_batches b ON b.run_id=s.run_id').fetchone()[0]
        atomic_json(args.out/'status.json',state)
    discovery = conn.execute('SELECT summary_json FROM tvl_discovery_runs WHERE summary_json IS NOT NULL ORDER BY id DESC LIMIT 1').fetchone()
    if discovery:
        summary = json.loads(discovery['summary_json'])
        state['catalog_status'] = summary['status']
        state['catalog_failures'] = [f for source in summary['sources'] for f in source.get('failures',[])]
    publish()
    try:
        all_vaults=[dict(r) for r in conn.execute('SELECT * FROM vaults')
                    if r['chain_id'] not in TVL_EXCLUDED_CHAIN_IDS]
        chain_vaults={}
        for v in all_vaults:chain_vaults.setdefault(v['chain_id'],[]).append(v)
        readers={};births={};starts={}
        for chain,vaults in chain_vaults.items():
            h=heads.get(chain)
            if not h or h['status']!='ready':
                state['pending_chains'].append({'chain_id':chain,'vaults':len(vaults),'reason':h.get('reason','missing_archive_rpc') if h else 'missing_archive_rpc'})
                continue
            try:
                reader=sources.ArchiveReader(chain)
                if int(reader.w3.eth.chain_id)!=chain:raise ValueError('RPC chain mismatch')
                pinned=reader.w3.eth.get_block(h['head_block'])
                if bytes(pinned['hash']).hex()!=h['head_hash']:raise ValueError('pinned head changed')
                reader.head=pinned
                readers[chain]=reader;births[chain]={}
                try:
                    yearn_prices_token_key(chain,'0x'+'00'*20)
                except ValueError:
                    state['price_limits'].append({'chain_id':chain,'reason':'unsupported_yearn_prices_namespace','balances':'archive-rpc'})
            except Exception as exc:
                state['pending_chains'].append({'chain_id':chain,'vaults':len(vaults),'reason':type(exc).__name__});continue
            publish(active_chain=chain,phase='resolving deployment dates')
            cached={r['address']:r for r in conn.execute("SELECT * FROM tvl_backfill_births WHERE chain_id=? AND status='ok'",(chain,))}
            pending=[v for v in vaults if v['address'].lower() not in cached]
            def resolve(v):
                try:
                    block,ts=deployment(reader,v)
                    return (chain,v['address'].lower(),block,ts,'ok',None)
                except Exception as exc:
                    return (chain,v['address'].lower(),None,None,'unavailable',type(exc).__name__)
            with ThreadPoolExecutor(max_workers=6) as pool:
                for index,result in enumerate(pool.map(resolve,pending),1):
                    if index%20==0: publish(deployment_progress={"chain_id":chain,"checked":index,"remaining":len(pending)-index})
                    with conn:conn.execute('INSERT OR REPLACE INTO tvl_backfill_births VALUES (?,?,?,?,?,?)',result)
            rows=conn.execute('SELECT * FROM tvl_backfill_births WHERE chain_id=?',(chain,)).fetchall()
            for r in rows:
                if r['status']=='ok':births[chain][r['address']]=r['block_number']
            known_times=[r['timestamp'] for r in rows if r['status']=='ok']
            earliest=min(known_times) if len(known_times)==len(vaults) else max(start-86399,h['genesis_timestamp'])
            starts[chain]=max(start,(earliest//86400)*86400+86399)
            publish(deployment_dates={'chain_id':chain,'verified':len(known_times),'unavailable':len(vaults)-len(known_times)})
        prices=Prices(conn);sources.fetch_yearn_price=prices.get
        sources.sync_strategies(conn)
        tasks=[]
        for chain in readers:
            for lo,hi in monthly_ranges(starts[chain],end):tasks.append((lo,hi,chain))
        tasks.sort(reverse=True)
        state['scheduled_batches']=len(tasks)
        performed=0
        for lo,hi,chain in tasks:
            existing=conn.execute('SELECT * FROM tvl_backfill_batches WHERE chain_id=? AND from_timestamp=? AND to_timestamp=?',(chain,lo,hi)).fetchone()
            if existing and existing['status'] in ('complete','incomplete'):continue
            selected=[v for v in chain_vaults[chain] if v['address'].lower() not in births[chain] or births[chain][v['address'].lower()]<=readers[chain].block_at(hi)]
            if not selected:continue
            edges=sources.candidate_edges(conn,selected)
            reader=BatchedReader(chain,conn,heads[chain],selected,edges,births[chain],prices)
            month=datetime.fromtimestamp(lo,UTC).strftime('%Y-%m')
            publish(phase='running',active_chain=chain,active_month=month,selected_vaults=len(selected))
            with conn:conn.execute("INSERT OR REPLACE INTO tvl_backfill_batches VALUES (?,?,?,NULL,'running')",(chain,lo,hi))
            def progress(message):
                print(message,flush=True)
                run=conn.execute('SELECT max(id) FROM tvl_runs').fetchone()[0]
                with conn:conn.execute('UPDATE tvl_backfill_batches SET run_id=? WHERE chain_id=? AND from_timestamp=? AND to_timestamp=?',(run,chain,lo,hi))
                publish(active_run=run,last_message=message)
                prices.cache.clear()
            run=sources.collect_tvl(conn,lo,hi,chain_ids=[chain],addresses=[v['address'] for v in selected],reader_factory=lambda _:reader,progress=progress)
            status=conn.execute('SELECT status FROM tvl_runs WHERE id=?',(run,)).fetchone()[0]
            with conn:conn.execute('UPDATE tvl_backfill_batches SET run_id=?,status=? WHERE chain_id=? AND from_timestamp=? AND to_timestamp=?',(run,status,chain,lo,hi))
            export_tvl(conn,args.out/'batches'/str(chain)/month,run)
            performed+=1
            publish(last_batch_status=status)
            if args.limit_batches and performed>=args.limit_batches:
                publish(phase='paused after requested batch limit');return
        publish(phase='finished; inspect incomplete batches and pending chains')
    except BaseException as exc:
        publish(phase='stopped',failure_class=type(exc).__name__)
        raise


if __name__=='__main__':main()
