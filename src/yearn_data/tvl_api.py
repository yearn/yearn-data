"""Read-only TVL publications assembled from accumulated finished collection runs."""
from __future__ import annotations

from collections import defaultdict
from concurrent.futures import Future
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal, localcontext
from functools import lru_cache
import hashlib
from itertools import groupby
import json
from pathlib import Path
import re
from statistics import median
import threading
import time
from urllib.parse import parse_qs, urlsplit

from .tvl import account, decimal
from .storage import connect, database_reference, table_exists, DATABASE_ERRORS

CHAINS = {1: 'Ethereum', 10: 'Optimism', 100: 'Gnosis', 137: 'Polygon', 250: 'Fantom',
          999: 'HyperEVM', 4663: 'Yeer', 8453: 'Base', 42161: 'Arbitrum', 747474: 'Katana'}
CATEGORIES = ('v1', 'v2', 'v3', 'curation')
HISTORY_CACHE_MAX_DATES = 400
BRIDGE_MIGRATIONS = [r for r in json.loads((Path(__file__).parent/'inventories'/'tvl-overlaps.json').read_text())['bridges'] if r.get('migrationTimestamp') is not None]
PRICE_REJECTIONS = json.loads((Path(__file__).parent/'inventories'/'tvl-price-rejections.json').read_text())


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def read_db(path):
    return connect(path, readonly=True)


def key(row):
    return row['chain_id'], row['vault'].lower()


def money(rows, field):
    if not rows:
        return 0.0
    values = [decimal(r.get(field)) for r in rows if r.get(field) is not None]
    with localcontext() as ctx:
        ctx.prec = 78
        return float(sum(values, Decimal(0))) if values else None


def chart_response(result,top=None):
    """Opt-in chart payload; the full diagnostic/export response is unchanged."""
    output={key:value for key,value in result.items() if key not in ('points','references')}
    output.update(points=[],references=[],meta=dict(result['meta']))
    if 'references' in result:
        output['meta']['referenceCount']=len(result['references'])
    if top is None:
        return output
    actual=result['actualChart']
    series=list(dict.fromkeys(name for row in actual for name in row if name!='timestamp'))
    def latest(name):
        return next((row[name] for row in reversed(actual) if name in row),0)
    ranked=sorted(series,key=latest,reverse=True)
    selected,remaining=ranked[:top],ranked[top:]
    other='All other vaults'
    chart=[]
    neutral=[]
    for row in actual:
        point={'timestamp':row['timestamp']}|{name:row[name] for name in selected if name in row}
        values=[row[name] for name in remaining if name in row]
        if values:point[other]=sum(values)
        chart.append(point)
    for row in result['constantPriceChart']:
        values=[value for name,value in row.items() if name!='timestamp']
        point={'timestamp':row['timestamp']}
        if values:point['Price-neutral TVL']=sum(values)
        neutral.append(point)
    output.pop('chart',None)
    output.update(actualChart=chart,constantPriceChart=neutral,
                  series=selected+([other] if remaining else []))
    output['meta'].update(topSeries=output['series'],sourceSeriesCount=len(series))
    return output


def publish_tvl(database, publication, *, current_bridge_policy='none'):
    """Publishing the union of finished runs never selects one latest backfill."""
    if current_bridge_policy not in ('none', 'retired-registry'):
        raise ValueError('unsupported current bridge policy')
    with closing(read_db(database)) as conn:
        runs = [dict(r) for r in conn.execute(
            "SELECT * FROM tvl_runs WHERE status IN ('complete','incomplete') AND completed_at IS NOT NULL ORDER BY id")]
        if not runs:
            raise ValueError('no finished TVL observations')
        if not conn.execute("SELECT 1 FROM tvl_snapshots s JOIN tvl_runs r ON r.id=s.run_id WHERE r.status IN ('complete','incomplete') AND r.completed_at IS NOT NULL LIMIT 1").fetchone():
            raise ValueError('no stored finished TVL observations')
        catalog = [dict(r) for r in conn.execute('SELECT * FROM tvl_vaults ORDER BY chain_id,address')]
        # Allocator recognition uses positive report evidence, not a V3 display label.
        allocator_keys = []
        if table_exists(conn, 'strategy_reports'):
            allocator_keys = [list(r) for r in conn.execute(
                "SELECT DISTINCT chain_id,lower(vault_address) FROM strategy_reports WHERE version='v3'")]
    context = {'runs': runs, 'catalog': catalog, 'allocatorKeys': sorted(allocator_keys),
               'currentBridgePolicy': current_bridge_policy, 'selectionPolicy': 'known-value-then-newest-finished-common-close-6', 'bridgeMigrations': BRIDGE_MIGRATIONS, 'priceRejections': PRICE_REJECTIONS,
               'historicalBridgePolicy': 'dated-evidence-only', 'database': database_reference(database)}
    dataset_id = hashlib.sha256(encoded(context).encode()).hexdigest()
    root = Path(publication)
    (root/'datasets').mkdir(parents=True, exist_ok=True)
    manifest = context | {'datasetId': dataset_id, 'publishedAt': int(time.time())}
    target = root/'datasets'/f'{dataset_id}.json'
    if not target.exists():
        dataset = TvlDataset(manifest)
        manifest['asOfTimestamp'] = dataset.as_of()
        _, rows, edges, diagnostics = dataset.frames((manifest['asOfTimestamp'],))[0]
        manifest['diagnostics'] = diagnostics | {
            'unavailableVaults': [{'chainId':r['chain_id'],'vault':r['vault'],'sourceRunId':r['source_run_id'],'reason':r.get('reason')}
                                  for r in rows if r.get('tvl_usd') is None],
            'unavailablePositions': [{'chainId':r['chain_id'],'parent':r['parent'],'strategy':r['strategy'],'child':r['child'],
                                     'sourceRunId':r['source_run_id'],'reason':r.get('reason')}
                                    for r in edges if r.get('status')!='ok']}
        temp = target.with_suffix('.tmp')
        temp.write_text(encoded(manifest)+'\n')
        temp.replace(target)
    temp = root/'.current.tmp'
    temp.write_text(encoded({'datasetId': dataset_id})+'\n')
    temp.replace(root/'current.json')
    return manifest


class TvlDataset:
    def __init__(self, manifest):
        self.manifest = manifest
        self.id = manifest['datasetId']
        self.database = manifest['database']
        self.run_ids = tuple(r['id'] for r in manifest['runs'])
        self.catalog = {(r['chain_id'], r['address'].lower()): r for r in manifest['catalog']}
        self.allocators = {tuple(r) for r in manifest['allocatorKeys']}
        self.bridge_migrations = {(r['sourceChainId'],r['sourceVaultAddress'].lower()):r for r in manifest.get('bridgeMigrations',[])}
        self.base = max(self.run_ids)+1
        self.price_rejections = {(r['chainId'],r['asset'].lower(),r['timestamp']): r for r in manifest.get('priceRejections',[])}
        self._lock = threading.RLock()
        self._pending = {}
        self._prepared = None

    def history_prepared(self):
        # Preparation may finish after this API process first saw the dataset.
        if not self._prepared:
            from .tvl_history_cache import ready
            self._prepared=ready(self)
        return self._prepared

    @lru_cache(maxsize=1)
    def dates(self):
        with closing(read_db(self.database)) as conn:
            placeholders = ','.join('?' for _ in self.run_ids)
            return tuple(r[0] for r in conn.execute(
                f'SELECT DISTINCT timestamp FROM tvl_snapshots WHERE run_id IN ({placeholders}) ORDER BY timestamp', self.run_ids))

    @lru_cache(maxsize=1)
    def as_of(self):
        if self.manifest.get('asOfTimestamp') is not None:
            return self.manifest['asOfTimestamp']
        expected = {c for r in self.manifest['runs'] for c in json.loads(r['params_json']).get('chain_ids',[])}
        marks = ','.join('?' for _ in self.run_ids)
        with closing(read_db(self.database)) as conn:
            if not expected:
                expected = {r[0] for r in conn.execute(f'SELECT DISTINCT chain_id FROM tvl_snapshots WHERE run_id IN ({marks})',self.run_ids)}
            for timestamp in reversed(self.dates()):
                observed = {r[0] for r in conn.execute(f"SELECT DISTINCT chain_id FROM tvl_snapshots WHERE run_id IN ({marks}) AND timestamp=? AND json_extract(data_json,'$.asset_units') IS NOT NULL",(*self.run_ids,timestamp))}
                if expected <= observed:
                    return timestamp
        raise ValueError('no common observed TVL close across the collected chain scope')

    def selected_rows(self, conn, table, timestamps, chain_id=None):
        """Prefer known observations; a failed retry cannot erase known history.

        Successful later repairs supersede earlier values only at the same identity
        and timestamp. Positions remain dated and must match the selected block.
        """
        dimensions = ('chain_id,vault,timestamp' if table == 'tvl_snapshots'
                      else 'chain_id,parent,strategy,child,timestamp')
        quality = ("CASE WHEN json_extract(data_json,'$.tvl_usd') IS NOT NULL THEN 2 "
                   "WHEN json_extract(data_json,'$.asset_units') IS NOT NULL THEN 1 ELSE 0 END"
                   if table == 'tvl_snapshots' else
                   "CASE WHEN json_extract(data_json,'$.status')='ok' THEN 1 ELSE 0 END")
        run_marks = ','.join('?' for _ in self.run_ids)
        date_marks = ','.join('?' for _ in timestamps)
        where = f'run_id IN ({run_marks}) AND timestamp IN ({date_marks})'
        args = (*self.run_ids, *timestamps)
        if chain_id is not None:
            where += ' AND chain_id=?'
            args += (chain_id,)
        joins = ' AND '.join('s.'+field+'=w.'+field for field in dimensions.split(','))
        sql = f'''WITH winners AS (
            SELECT {dimensions},MAX(({quality})*{self.base}+run_id) AS rank
            FROM {table} WHERE {where} GROUP BY {dimensions})
            SELECT s.run_id,s.data_json FROM {table} s JOIN winners w
            ON {joins} AND s.run_id=w.rank%{self.base} ORDER BY s.timestamp,s.chain_id'''
        records=conn.stream(sql,args) if hasattr(conn,'raw') else conn.execute(sql,args)
        for row in records:
            data = json.loads(row['data_json'])
            data['source_run_id'] = row['run_id']
            yield self.validate_price(data) if table == 'tvl_snapshots' else data

    def validate_price(self, row):
        rejected = self.price_rejections.get((row['chain_id'],(row.get('asset') or '').lower(),row['timestamp']))
        if rejected and decimal(row.get('price_usd')) == decimal(rejected['priceUsd']):
            row.update(tvl_usd=None, price_usd=None, status='unavailable', reason=rejected['reason'])
        return row

    def reference_candidates(self, conn, timestamps, chain_id=None):
        if not timestamps:
            return
        run_marks, date_marks = ','.join('?' for _ in self.run_ids), ','.join('?' for _ in timestamps)
        where = f'run_id IN ({run_marks}) AND timestamp IN ({date_marks})'
        args = (*self.run_ids,*timestamps)
        if chain_id is not None:
            where += ' AND chain_id=?'
            args += (chain_id,)
        # Reject invalid quotes before ranking valid daily observations. Otherwise
        # seven rejected initial quotes can exhaust the reference window and hide
        # the later corrected canonical prices.
        rejected = []
        for (chain,asset,timestamp), quote in self.price_rejections.items():
            rejected.append("(s.chain_id=? AND s.timestamp=? AND lower(json_extract(s.data_json,'$.asset'))=? AND CAST(json_extract(s.data_json,'$.price_usd') AS TEXT)=?)")
            args += (chain,timestamp,asset,quote['priceUsd'])
        exclude = ' AND NOT ('+' OR '.join(rejected)+')' if rejected else ''
        sql = f'''WITH winners AS (
            SELECT chain_id,vault,timestamp,MAX((CASE WHEN json_extract(data_json,'$.tvl_usd') IS NOT NULL THEN 2
                WHEN json_extract(data_json,'$.asset_units') IS NOT NULL THEN 1 ELSE 0 END)*{self.base}+run_id) rank
            FROM tvl_snapshots WHERE {where} GROUP BY chain_id,vault,timestamp),
            candidates AS (SELECT s.run_id,s.timestamp,s.chain_id,s.vault,
                ROW_NUMBER() OVER (PARTITION BY s.chain_id,s.vault ORDER BY s.timestamp) n
                FROM tvl_snapshots s JOIN winners w ON s.chain_id=w.chain_id AND s.vault=w.vault
                AND s.timestamp=w.timestamp AND s.run_id=w.rank%{self.base}
                WHERE CAST(json_extract(s.data_json,'$.asset_units') AS REAL)>0
                AND CAST(json_extract(s.data_json,'$.tvl_usd') AS REAL)>0{exclude})
            SELECT s.data_json FROM candidates c JOIN tvl_snapshots s
            ON s.run_id=c.run_id AND s.chain_id=c.chain_id AND s.vault=c.vault AND s.timestamp=c.timestamp
            WHERE c.n<=7 ORDER BY c.timestamp'''
        for row in conn.execute(sql,args):
            yield self.validate_price(json.loads(row['data_json']))

    @lru_cache(maxsize=8)
    def frames(self, timestamps, chain_id=None, include_curation=False):
        return tuple(self.iter_frames(timestamps, chain_id, include_curation))

    def iter_frames(self, timestamps, chain_id=None, include_curation=False):
        if not timestamps:
            return
        with closing(read_db(self.database)) as conn, conn:
            snapshots = groupby(self.selected_rows(conn, 'tvl_snapshots', timestamps, chain_id), lambda r: r['timestamp'])
            edges = iter(groupby(self.selected_rows(conn, 'tvl_positions', timestamps, chain_id), lambda r: r['timestamp']))
            edge_group = next(edges, None)
            for timestamp, points in snapshots:
                points = list(points)
                while edge_group and edge_group[0] < timestamp:
                    list(edge_group[1])
                    edge_group = next(edges, None)
                positions = list(edge_group[1]) if edge_group and edge_group[0] == timestamp else []
                if edge_group and edge_group[0] == timestamp:
                    edge_group = next(edges, None)
                by_key = {key(row): row for row in points}
                for edge in positions:
                    parent = by_key.get((edge['chain_id'], edge['parent'].lower()))
                    if parent and edge.get('block_number') != parent.get('block_number'):
                        edge.update(status='unavailable', reason='selected_block_mismatch')
                excluded = set()
                for point in points:
                    migration = self.bridge_migrations.get(key(point))
                    if migration is not None:
                        # A dated migration takes precedence over today's active
                        # flag, including when the latest close is pre-migration.
                        if timestamp >= migration['migrationTimestamp']:
                            excluded.add(key(point))
                    elif timestamp == self.as_of() and self.manifest['currentBridgePolicy'] == 'retired-registry':
                        meta = self.catalog.get(key(point), {})
                        if point.get('bridge_target_chain') in {c for c,_ in self.catalog} and meta.get('active') == 0:
                            excluded.add(key(point))
                # Excluded parents must not also deduct their holdings from child
                # TVL. Keep their dated raw positions available for diagnostics.
                excluded_positions = [edge for edge in positions if (edge['chain_id'],edge['parent'].lower()) in excluded]
                included_positions = [edge for edge in positions if (edge['chain_id'],edge['parent'].lower()) not in excluded]
                account_points = [dict(point,bridge_target_chain=None) if key(point) in self.bridge_migrations else point for point in points]
                rows, positions, diagnostics = account(account_points, included_positions, include_curation=include_curation)
                positions.extend(dict(edge,overlap_usd='0',accounting_excluded=True,reason='bridge_parent_excluded') for edge in excluded_positions)
                for row in rows:
                    migration = self.bridge_migrations.get(key(row))
                    if migration:
                        row['bridge_target_chain'] = migration['targetChainId']
                    if key(row) in excluded:
                        row['bridge_excluded_usd'] = row['external_tvl_usd']
                        if migration:
                            row['bridge_migration_timestamp'] = migration['migrationTimestamp']
                        if row['external_tvl_usd'] is not None:
                            row['external_tvl_usd'] = '0'
                with localcontext() as ctx:
                    ctx.prec = 78
                    bridge_amount = sum((decimal(row['bridge_excluded_usd']) for row in rows if row.get('bridge_excluded_usd') is not None),Decimal(0))
                    diagnostics['known_bridge_excluded_usd'] = str(bridge_amount)
                    diagnostics['known_external_tvl_usd'] = str(max(Decimal(0),decimal(diagnostics['known_external_tvl_usd'])-bridge_amount))
                    if diagnostics['external_tvl_usd'] is not None:
                        diagnostics['external_tvl_usd'] = str(max(Decimal(0),decimal(diagnostics['external_tvl_usd'])-bridge_amount))
                diagnostics['position_count'] = len(positions)
                yield timestamp, rows, positions, diagnostics

    def sampled_dates(self, interval, start, end):
        dates = [t for t in self.dates() if t <= self.as_of() and (start is None or t >= start) and (end is None or t <= end)]
        buckets = {}
        for timestamp in dates:
            date = datetime.fromtimestamp(timestamp, timezone.utc)
            bucket = (date.date().isoformat() if interval == 'daily' else
                      (date.isocalendar().year, date.isocalendar().week) if interval == 'weekly' else timestamp//(3*86400))
            buckets[bucket] = timestamp
        return tuple(sorted(buckets.values()))

    def summary(self, chain_id=None):
        _, rows, _, diagnostics = self.frames((self.as_of(),), chain_id)[0]
        categories = {c: [r for r in rows if r.get('category', r['version']) == c] for c in CATEGORIES}
        active = [r for r in rows if self.catalog.get(key(r), {}).get('active', 1)]
        retired = [r for r in rows if r not in active]
        chain_rows = {CHAINS.get(c, f'Chain {c}'): [r for r in rows if r['chain_id'] == c] for c in sorted({r['chain_id'] for r in rows})}
        bridge = [r for r in rows if r.get('bridge_excluded_usd') is not None]
        result = {'totalTvl': money(rows, 'external_tvl_usd'), 'activeVaultTvl': money(active, 'tvl_usd'),
                  'retiredVaultTvl': money(retired, 'tvl_usd'), 'overlapExcluded': money(rows, 'overlap_usd'),
                  'vaultBridgeExcluded': money(bridge, 'bridge_excluded_usd') if bridge else 0,
                  'tvlByChain': {c: money(rs, 'tvl_usd') for c, rs in chain_rows.items()},
                  'overlapByChain': {c: money(rs, 'overlap_usd') for c, rs in chain_rows.items()},
                  'crossChainOverlapByChain': {c: money([r for r in rs if 'bridge_excluded_usd' in r], 'bridge_excluded_usd') or 0 for c, rs in chain_rows.items()},
                  'tvlByCategory': {c: money([r for r in rs if r in active], 'tvl_usd') for c, rs in categories.items()},
                  'retiredTvlByCategory': {c: money([r for r in rs if r in retired], 'tvl_usd') for c, rs in categories.items()},
                  'crossChainOverlapByCategory': {c: money([r for r in rs if 'bridge_excluded_usd' in r], 'bridge_excluded_usd') or 0 for c, rs in categories.items()},
                  'vaultCount': {'total': len(rows), 'active': len(active), 'retired': len(retired), **{c:len(rs) for c,rs in categories.items()}},
                  'retiredVaults': [{'address':r['vault'],'chainId':r['chain_id'],'name':r.get('name'),
                                    'category':r.get('category',r['version']),'tvlUsd':float(decimal(r['tvl_usd'])) if r['tvl_usd'] is not None else None,
                                    'isCrossChainOverlap':'bridge_excluded_usd' in r} for r in retired]}
        result.update({c+'Tvl': result['tvlByCategory'][c] for c in CATEGORIES})
        return result | {'datasetId': self.id, 'asOfTimestamp': self.as_of()}

    def compute_once(self, identity, compute):
        # Coalesce only identical expensive reads. Unrelated requests, including
        # the current summary, must not wait behind a full-history calculation.
        with self._lock:
            future = self._pending.get(identity)
            owner = future is None
            if owner:
                future = self._pending[identity] = Future()
        if not owner:
            return future.result()
        try:
            result = compute()
            future.set_result(result)
            return result
        except Exception as error:
            future.set_exception(error)
            raise
        finally:
            with self._lock:
                del self._pending[identity]

    @lru_cache(maxsize=4)
    def history_rows(self, timestamps, chain_id):
        return self.compute_once(('rows',timestamps,chain_id), lambda:self._history_rows(timestamps,chain_id))

    def _history_rows(self, timestamps, chain_id):
        if self.history_prepared():
            from .tvl_history_cache import rows
            return tuple(rows(self,timestamps,chain_id))
        if chain_id is not None:
            # Main charts and drilldowns share one dated accounting calculation.
            return tuple((timestamp,selected) for timestamp,rows in self.history_rows(timestamps,None)
                         if (selected := tuple(row for row in rows if row['chain_id']==chain_id)))
        # Keep only the fields charts need, never full snapshot/position payloads.
        # Version, chain and constant-price views share this dated accounting.
        fields = ('chain_id','vault','name','category','version','asset_units','tvl_usd','external_tvl_usd')
        return tuple((timestamp, tuple({field:row[field] for field in fields if field in row} for row in rows))
                     for timestamp,rows,_,_ in self.iter_frames(timestamps, chain_id))

    @lru_cache(maxsize=8)
    def price_references(self, window, chain_id):
        return self.compute_once(('references',window,chain_id), lambda:self._price_references(window,chain_id))

    def _price_references(self, window, chain_id):
        if chain_id is not None:
            references, prices = self.price_references(window,None)
            return ({identity:reference for identity,reference in references.items() if identity[0]==chain_id},
                    {identity:price for identity,price in prices.items() if identity[0]==chain_id})
        candidates = defaultdict(list)
        def source_rows():
            if self.history_prepared():
                from .tvl_history_cache import references
                yield from references(self,window)
            else:
                with closing(read_db(self.database)) as conn:
                    yield from self.reference_candidates(conn, window, chain_id)
        for row in source_rows():
            units, value = decimal(row.get('asset_units')), decimal(row.get('tvl_usd'))
            if units and value and len(candidates[key(row)]) < 7:
                with localcontext() as ctx:
                    ctx.prec = 78
                    candidates[key(row)].append((row['timestamp'], value/units))
        references, reference_prices = {}, {}
        for identity, values in candidates.items():
            center = median(price for _,price in values)
            first = values[0]
            skipped = max(first[1]/center,center/first[1]) > Decimal('1.5')
            chosen = next(((t,p) for t,p in values if max(p/center,center/p)<=Decimal('1.5')),first) if skipped else first
            references[identity] = {'vault':f'{identity[0]}:{identity[1]}','timestamp':chosen[0],
                                    'priceUsd':float(chosen[1]),'source':'stored-tvl','depegCandidateSkipped':skipped}
            reference_prices[identity] = chosen[1]
        return references, reference_prices

    def history(self, group='chain', mode='external', interval='weekly', start=None, end=None, chain_id=None, constant=False):
        # Canonicalize all-time bounds before caching: explicit first/last closes
        # and omitted bounds select the same observations and reference window.
        dates = self.dates()
        if start is not None and start <= dates[0]:
            start = None
        if end is not None and end >= self.as_of():
            end = None
        return self._history(group,mode,interval,start,end,chain_id,constant)

    @lru_cache(maxsize=32)
    def _history(self, group, mode, interval, start, end, chain_id, constant):
        timestamps = self.sampled_dates(interval, start, end)
        if not constant and group in ('chain','category','type') and chain_id is None:
            from .tvl_history_cache import totals_ready,totals
            if totals_ready(self):
                chart={timestamp:{'timestamp':timestamp} for timestamp in timestamps}
                points=[]
                for row in totals(self,timestamps,'category' if group=='type' else group,mode,chain_id):
                    value=float(Decimal(row['value']))
                    chart[row['timestamp']][row['series']]=value
                    points.append({'timestamp':row['timestamp'],'series':row['series'],'tvlUsd':value})
                return self.history_result(group,mode,interval,start,end,timestamps,points,list(chart.values()))
        # Long daily histories remain streamed instead of retaining millions of
        # vault rows. The main weekly charts fit within the bounded shared cache.
        if self.history_prepared():
            from .tvl_history_cache import rows
            frames=rows(self,timestamps,chain_id)
        else:
            frames = (self.history_rows(timestamps, chain_id) if len(timestamps) <= HISTORY_CACHE_MAX_DATES
                      else ((timestamp,rows) for timestamp,rows,_,_ in self.iter_frames(timestamps,chain_id)))
        references = {}
        reference_prices = {}
        if constant:
            # Reference selection remains daily and scoped to the requested window.
            window = tuple(t for t in self.dates() if t <= self.as_of() and (start is None or t >= start) and (end is None or t <= end))
            references, reference_prices = self.price_references(window, chain_id)
        points = []
        actual_chart, fixed_chart = [], []
        for timestamp, rows in frames:
            actual, fixed = defaultdict(list), defaultdict(list)
            for row in rows:
                name = (CHAINS.get(row['chain_id'],f"Chain {row['chain_id']}") if group == 'chain' else
                        row.get('category',row['version']) if group in ('category','type') else
                        f"{row.get('name') or row['vault']} ({row['chain_id']}:{row['vault'][:8]})")
                field = 'external_tvl_usd' if mode == 'external' else 'tvl_usd'
                value = decimal(row.get(field))
                if value is not None:
                    actual[name].append(value)
                reference = references.get(key(row))
                units, raw = decimal(row.get('asset_units')), decimal(row.get('tvl_usd'))
                if reference and units is not None and value is not None and raw is not None:
                    with localcontext() as ctx:
                        ctx.prec = 78
                        fraction = value/raw if mode == 'external' and raw else Decimal(1)
                        fixed[name].append(units*reference_prices[key(row)]*fraction)
            chart = {'timestamp':timestamp}
            neutral = {'timestamp':timestamp}
            for name, values in sorted(actual.items()):
                with localcontext() as ctx:
                    ctx.prec = 78
                    value = float(sum(values,Decimal(0)))
                    chart[name] = value
                    points.append({'timestamp':timestamp,'series':name,'tvlUsd':value})
                    if name in fixed:
                        neutral[name] = float(sum(fixed[name],Decimal(0)))
            actual_chart.append(chart)
            fixed_chart.append(neutral)
        result = self.history_result(group,mode,interval,start,end,timestamps,points,actual_chart)
        if constant:
            fixed_by_timestamp = {row['timestamp']:row for row in fixed_chart}
            result.update(schemaVersion=1,methodology='asset units × timeframe reference × dated external fraction',
                          actualChart=actual_chart,constantPriceChart=fixed_chart,references=list(references.values()),
                          meta={'referenceWindowPoints':7},
                          points=[{'timestamp':p['timestamp'],'series':p['series'],'actualTvlUsd':p['tvlUsd'],
                                   'constantPriceTvlUsd':fixed_by_timestamp[p['timestamp']].get(p['series'])} for p in points])
        return result

    def history_result(self,group,mode,interval,start,end,timestamps,points,chart):
        return {'id':max(self.run_ids),'runId':max(self.run_ids),'createdAt':datetime.fromtimestamp(self.manifest['publishedAt'],timezone.utc).isoformat(),
                'mode':mode,'groupBy':group,'interval':interval,'range':{'from':timestamps[0] if timestamps else start,'to':timestamps[-1] if timestamps else end},
                'series':sorted({r['series'] for r in points}),'points':points,'chart':chart,'pointCount':len(points),
                'maxVaults':None,'top':None,'query':{},'meta':{},'datasetId':self.id}

    def audit(self, chain_id=None):
        _, rows, positions, _ = self.frames((self.as_of(),), chain_id)[0]
        by_parent = defaultdict(list)
        by_key = {key(r):r for r in rows}
        for edge in positions:
            child = by_key.get((edge['chain_id'],edge['child'].lower()))
            by_parent[(edge['chain_id'],edge['parent'].lower())].append({
                'address':edge['strategy'],'name':None,'debtUsd':float(decimal(edge['debt_usd'])) if edge.get('debt_usd') is not None else None,
                'targetVaultAddress':child['vault'] if child else None,'targetVaultChainId':child['chain_id'] if child else None,
                'detectionMethod':'registry' if edge.get('method')=='service-registry' else 'auto' if child else None,'label':None})
        summary = self.summary(chain_id)
        return {'summedTvl':money(rows,'tvl_usd'),'overlapTvl':summary['overlapExcluded'],
                'crossChainOverlap':summary['vaultBridgeExcluded'],'vaultCount':len(rows),
                'vaults':[{'address':r['vault'],'chainId':r['chain_id'],'name':r.get('name'),'category':r.get('category',r['version']),
                           'vaultType':1 if key(r) in self.allocators else None,'tvlUsd':float(decimal(r['tvl_usd'])) if r['tvl_usd'] is not None else None,
                           'isRetired':not self.catalog.get(key(r),{}).get('active',1),'isHidden':False,
                           'strategies':by_parent[key(r)]} for r in rows],
                'crossChainVaults':[{'address':r['vault'],'chainId':r['chain_id'],'targetChainId':r['bridge_target_chain'],
                                    'name':r.get('name'),'category':r.get('category',r['version']),
                                    'tvlUsd':float(decimal(r['bridge_excluded_usd'])),'label':'Dated pre-deposit migration' if r.get('bridge_migration_timestamp') is not None else 'Retired bridge registry'}
                                   for r in rows if r.get('bridge_excluded_usd') is not None], 'datasetId':self.id}

    def curation(self, chain_id=None):
        timestamp, all_rows, positions, _ = self.frames((self.as_of(),), chain_id, True)[0]
        selected = [r for r in all_rows if self.catalog.get(key(r),{}).get('active',1) and
                    (r.get('category')=='curation' or key(r) in self.allocators)]
        rows, edges, _ = account(selected, positions, include_curation=True)
        def family(row):
            return 'morpho_curated' if row.get('category')=='curation' else 'v3_allocator'
        def totals(rs):
            return {'grossTvlUsd':money(rs,'tvl_usd'),'knownInternalOverlapTvlUsd':money(rs,'overlap_usd'),
                    'netTvlUsd':money(rs,'external_tvl_usd'),'vaultCount':len(rs)}
        v2 = [r for r in all_rows if r['version']=='v2' and self.catalog.get(key(r),{}).get('active',1)]
        product_keys = {key(r) for r in selected}
        v2_keys = {key(r) for r in v2}
        pass_through = [e for e in positions if (e['chain_id'],e['parent'].lower()) in v2_keys
                        and (e['chain_id'],e['child'].lower()) in product_keys]
        passed = money(pass_through, 'overlap_usd')
        gross_v2 = money(v2, 'tvl_usd')
        incremental = max(0, gross_v2-passed) if gross_v2 is not None and passed is not None else None
        return {'methodologyVersion':'yearn-data-1','asOf':datetime.fromtimestamp(timestamp,timezone.utc).isoformat(),
                'definition':{'headline':'Yearn curation products','includes':['V3 allocators with report evidence','Morpho curated products'],
                              'excludes':['Tokenized Strategy layer'],'accounting':'Stored product TVL less verified internal holdings'},
                'grossTvlUsd':money(rows,'tvl_usd'),'knownInternalOverlapTvlUsd':money(rows,'overlap_usd'),
                'totalTvlUsd':money(rows,'external_tvl_usd'),'vaultCount':len(rows),
                'byFamily':{f:totals([r for r in rows if family(r)==f]) for f in ('v3_allocator','morpho_curated')},
                'byChain':{CHAINS.get(c,f'Chain {c}'):money([r for r in rows if r['chain_id']==c],'external_tvl_usd') for c in {r['chain_id'] for r in rows}},
                'vaults':[{'address':r['vault'],'chainId':r['chain_id'],'name':r.get('name'),'family':family(r),**{k:v for k,v in totals([r]).items() if k!='vaultCount'}} for r in rows],
                'potentialV2':{'grossTvlUsd':gross_v2,'knownPassThroughTvlUsd':passed,'incrementalTvlUsd':incremental,'vaultCount':len(v2),
                               'note':'Potential V2 product attribution remains separate from V3 and Morpho products'},'limitations':[], 'datasetId':self.id}

    def comparison(self, reference_database):
        """Only DefiLlama reference observations come from the external source."""
        if reference_database is None:
            raise ValueError('DefiLlama reference database is not configured')
        summary = self.summary()
        if summary['totalTvl'] is None:
            raise ValueError('no available TVL total for comparison')
        reference = {}
        with closing(read_db(reference_database)) as conn:
            for protocol in ('yearn-finance','yearn-curating'):
                latest = conn.execute('SELECT timestamp FROM defillama_snapshots WHERE protocol=? ORDER BY id DESC LIMIT 1',(protocol,)).fetchone()
                if latest is None:
                    raise ValueError('no stored DefiLlama reference for '+protocol)
                reference[protocol] = {row['chain'] or 'total':row['tvl_usd'] for row in conn.execute(
                    'SELECT chain,tvl_usd FROM defillama_snapshots WHERE protocol=? AND timestamp=?',(protocol,latest[0]))}
        dl_total = sum(reference[p]['total'] for p in reference)
        by_chain = []
        names = set(summary['tvlByChain']) | {c for rs in reference.values() for c in rs if c!='total'}
        for name in sorted(names):
            gross = summary['tvlByChain'].get(name,0)
            ours = gross-summary['overlapByChain'].get(name,0)-summary['crossChainOverlapByChain'].get(name,0) if gross is not None else None
            dl = sum(rs.get(name,0) for rs in reference.values())
            by_chain.append({'chain':name,'ours':ours,'defillama':dl,'difference':ours-dl if ours is not None else None})
        _, rows, _, _ = self.frames((self.as_of(),))[0]
        categories = []
        for label,protocol,curation in (('V1 + V2 + V3','yearn-finance',False),('Curation','yearn-curating',True)):
            ours = money([r for r in rows if (r.get('category')=='curation')==curation],'external_tvl_usd')
            dl = reference[protocol]['total']
            categories.append({'category':label,'defillamaProtocol':protocol,'ours':ours,'defillama':dl,'difference':ours-dl if ours is not None else None})
        return {'ourTotal':summary['totalTvl'],'defillamaTotal':dl_total,'difference':summary['totalTvl']-dl_total,
                'differencePercent':(summary['totalTvl']/dl_total-1)*100 if dl_total else None,
                'retiredTvl':summary['retiredVaultTvl'],'overlapDeducted':summary['overlapExcluded'],
                'crossChainOverlap':summary['vaultBridgeExcluded'],'grossTvl':money(rows,'tvl_usd'),
                'gapComponents':[],'retiredTvlByChain':{name:money([r for r in rows if CHAINS.get(r['chain_id'],f"Chain {r['chain_id']}")==name and not self.catalog.get(key(r),{}).get('active',1)],'tvl_usd') for name in summary['tvlByChain']},
                'notes':[],'byChain':by_chain,'byCategory':categories,'datasetId':self.id}


class TvlStore:
    def __init__(self, database, publication, *, current_bridge_policy='none', refresh_seconds=30, comparison_database=None):
        self.database, self.root = database, Path(publication)
        self.policy, self.refresh_seconds = current_bridge_policy, refresh_seconds
        self.comparison_database = comparison_database
        self.lock = threading.RLock()
        self.last_check = 0
        self.last_error = None
        self.refresh()

    def refresh(self):
        with self.lock:
            try:
                publish_tvl(self.database, self.root, current_bridge_policy=self.policy)
                self.last_error = None
            except (OSError, *DATABASE_ERRORS, ValueError) as error:
                self.last_error = str(error)
                if not (self.root/'current.json').exists():
                    raise
            self.last_check = time.monotonic()

    @lru_cache(maxsize=2)
    def load(self, dataset_id):
        manifest = json.loads((self.root/'datasets'/f'{dataset_id}.json').read_text())
        return TvlDataset(manifest)

    def get(self, dataset_id=None):
        with self.lock:
            if dataset_id is None:
                if time.monotonic()-self.last_check >= self.refresh_seconds:
                    self.refresh()
                dataset_id = json.loads((self.root/'current.json').read_text())['datasetId']
            if not isinstance(dataset_id,str) or not re.fullmatch('[a-f0-9]{64}',dataset_id):
                raise ValueError('invalid TVL datasetId')
            return self.load(dataset_id)


def tvl_response(store, url):
    request = urlsplit(url)
    paths = {'/api/tvl','/api/tvl/','/api/tvl/history/runs/latest','/api/tvl/history/runs/latest/constant-price',
             '/api/tvl/curation-products','/api/audit/tree','/api/comparison'}
    if request.path not in paths:
        return 404, {'error':'Not found'}
    try:
        query = parse_qs(request.query,keep_blank_values=True)
        if set(query)-{'chainId','groupBy','mode','interval','from','to','datasetId','includeCurrent','format','top'} or any(len(v)!=1 for v in query.values()):
            raise ValueError('unsupported or repeated TVL filter')
        def value(name, default=None):
            return query.get(name,[default])[0]
        def integer(name):
            raw = value(name)
            if raw is None:
                return None
            if not re.fullmatch(r'\d+',raw):
                raise ValueError(name+' must be a nonnegative integer')
            return int(raw)
        chain_id, start, end = integer('chainId'), integer('from'), integer('to')
        if chain_id == 0 or start is not None and end is not None and start > end:
            raise ValueError('invalid chain or time range')
        group, mode, interval = value('groupBy','chain'), value('mode','external'), value('interval','weekly')
        response_format=value('format','full')
        top=integer('top')
        if response_format not in ('full','chart'):
            raise ValueError('unsupported history format')
        if top is not None and (not 1<=top<=50 or response_format!='chart' or group!='vault' or not request.path.endswith('/constant-price')):
            raise ValueError('top requires chart-format constant-price vault history and must be between 1 and 50')
        if group not in ('chain','vault','category','type') or mode not in ('raw','external') or interval not in ('daily','3day','weekly'):
            raise ValueError('unsupported TVL grouping, mode or interval')
        if value('includeCurrent') not in (None,'true','false'):
            raise ValueError('includeCurrent must be true or false')
        dataset = store.get(value('datasetId'))
        if chain_id is not None and chain_id not in {c for c,_ in dataset.catalog}:
            raise ValueError('unsupported TVL chain')
        if request.path.startswith('/api/tvl/history'):
            result = dataset.history(group,mode,interval,start,end,chain_id,request.path.endswith('/constant-price'))
            if response_format=='chart':result=chart_response(result,top)
        elif request.path.endswith('/curation-products'):
            result = dataset.curation(chain_id)
        elif request.path == '/api/audit/tree':
            result = dataset.audit(chain_id)
        elif request.path == '/api/comparison':
            result = dataset.comparison(store.comparison_database)
        else:
            result = dataset.summary(chain_id)
        return 200, result
    except ValueError as error:
        return 400, {'error':str(error)}
