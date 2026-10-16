"""Read-only Powerglove views over explicitly selected completed analyses."""
from __future__ import annotations

from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import gzip
import json
from pathlib import Path
import re
import threading
import time
from urllib.parse import parse_qs, urlsplit

from .analysis import closed_cutoff
from .fee_valuation import POLICY
from .storage import connect, database_reference, DATABASE_ERRORS

FAMILIES = ('yearn-v2-vault', 'yearn-v3-allocator', 'yearn-v3-tokenized-strategy')
FEE_FIELDS = {
    'totalFeesPaidUsd': 'total_fees_paid_usd',
    'protocolFeesUsd': 'protocol_fee_usd',
    'managerFeesUsd': 'manager_fee_usd',
    'performanceFeesUsd': 'performance_fee_usd',
    'managementFeesUsd': 'management_fee_usd',
    'strategistFeesUsd': 'strategist_fee_usd',
    'totalRefundsUsd': 'total_refunds_usd',
    'lockerBonusUsd': 'locker_bonus_usd',
    'reportedTotalFeesUsd': 'reported_total_fees_usd',
}
EARNINGS_FIELDS = {
    'grossGainsUsd': 'gross_gain_usd', 'lossesUsd': 'loss_usd', 'netYieldUsd': 'net_yield_usd',
    'rawGrossGainsUsd': 'raw_gross_gain_usd', 'rawLossesUsd': 'raw_loss_usd',
    'rawNetYieldUsd': 'raw_net_yield_usd',
}


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _identity(row):
    return row['chain_id'], row['tx_hash'].lower(), row['log_index']


def _sum(rows, field, precision=400):
    values = [row[field] for row in rows if row.get(field) is not None]
    if not values:
        return None
    with localcontext() as ctx:
        ctx.prec = precision
        return format(sum((Decimal(str(value)) for value in values), Decimal(0)), 'f')


def _fees(rows):
    return {name: _sum(rows, field) for name, field in FEE_FIELDS.items()}


def _earnings(rows):
    # Preserve the existing lifetime-yield analysis' Decimal accumulation policy.
    return {name: _sum(rows, field, precision=28) for name, field in EARNINGS_FIELDS.items()}


def _load_rows(conn, run_id, name):
    digest = hashlib.sha256()
    rows = []
    seen = set()
    for result in conn.execute(
        'SELECT row_json FROM analysis_outputs WHERE run_id=? AND name=? ORDER BY rowid', (run_id, name)
    ):
        raw = result['row_json']
        digest.update(raw.encode() + b'\n')
        row = json.loads(raw)
        identity = _identity(row)
        if identity in seen:
            raise ValueError(f'duplicate {name} identity: {identity}')
        seen.add(identity)
        rows.append(row)
    if not rows:
        raise ValueError(f'analysis {run_id} has no {name} output')
    return rows, digest.hexdigest()


class PairingDataset:
    """Immutable, bounded result set; queries make no RPC or price requests."""

    def __init__(self, database, earnings_run_id, fees_run_id, *, vault_names=None):
        self.database = database_reference(database)
        with closing(connect(self.database, readonly=True)) as conn:
            runs = []
            for run_id, name in ((earnings_run_id, 'lifetime-yield'), (fees_run_id, 'fee-usd')):
                row = conn.execute(
                    "SELECT * FROM analysis_runs WHERE id=? AND name=? AND status='complete'", (run_id, name)
                ).fetchone()
                if row is None:
                    raise ValueError(f'analysis {run_id} is not a completed {name} run')
                runs.append(dict(row) | {'params': json.loads(row['params_json'])})
            ep, fp = (run['params'] for run in runs)
            self.cutoff = closed_cutoff(ep.get('before_timestamp'))
            if ep.get('before_timestamp') is None or fp.get('before_timestamp') != self.cutoff:
                raise ValueError('paired analyses require the same explicit closed UTC cutoff')
            if (ep.get('price_source') != 'yearn-prices' or ep.get('fallback_price_source') is not None
                    or fp.get('provider') != 'yearn-prices' or fp.get('policy') != POLICY):
                raise ValueError('paired analyses require Yearn-only EOD pricing without provider fallback')
            self.earnings, earnings_hash = _load_rows(conn, earnings_run_id, 'reports')
            self.fees, fees_hash = _load_rows(conn, fees_run_id, 'fee_usd_events')
            if vault_names is None:
                vault_names = {f"{row['chain_id']}:{row['address'].lower()}": row['name']
                               for row in conn.execute('SELECT chain_id,address,name FROM vaults')}
        self.vault_names = vault_names
        report_map = {_identity(row): row for row in self.earnings}
        allocator_fees = { _identity(row): row for row in self.fees
                           if row['contract_family'] in FAMILIES[:2] }
        if report_map.keys() != allocator_fees.keys():
            raise ValueError('earnings and allocator fees describe different stored report cohorts')
        selected_prices = {}
        shared_prices = set()
        diagnostics = Counter()
        for row in self.fees:
            if row['contract_family'] not in FAMILIES:
                raise ValueError('unsupported fee contract family')
            diagnostics['accounting:' + row['accounting_status']] += 1
            diagnostics['valuation:' + row['valuation_status']] += 1
        fee_prices = set()
        for kind, rows in enumerate((self.fees, self.earnings)):
            for row in rows:
                if not 0 <= row['block_timestamp'] < self.cutoff:
                    raise ValueError('analysis contains an event outside its declared cutoff')
                price = row.get('price_usd')
                if price is not None:
                    value = Decimal(str(price))
                    if not value.is_finite() or value <= 0:
                        raise ValueError('invalid selected USD price')
                    key = (row['chain_id'], row['asset'].lower(), row['block_timestamp'] // 86400)
                    if key in selected_prices:
                        if selected_prices[key] != value:
                            raise ValueError(f'selected price mismatch for asset/day {key}')
                        if kind == 1 and key in fee_prices:
                            shared_prices.add(key)
                    selected_prices[key] = value
                    if kind == 0:
                        fee_prices.add(key)
        for key, row in report_map.items():
            fee = allocator_fees[key]
            if any(row[field] != fee[field] for field in ('vault_address', 'block_timestamp', 'asset')):
                raise ValueError(f'paired report identity mismatch: {key}')
            if row['version'] not in ('v2', 'v3'):
                raise ValueError('unsupported earnings report family')
            diagnostics['earnings:' + row['valuation_status']] += 1
        self.chains = tuple(sorted({row['chain_id'] for row in self.fees + self.earnings}))
        self.vaults = frozenset((row['chain_id'], row['vault_address'].lower())
                               for row in self.fees + self.earnings)
        self.context = {
            'earningsRunId': earnings_run_id, 'feesRunId': fees_run_id, 'beforeTimestamp': self.cutoff,
            'runs': runs, 'earningsOutputSha256': earnings_hash, 'feesOutputSha256': fees_hash,
            'includedFeeFamilies': list(FAMILIES), 'earningsFamilies': list(FAMILIES[:2]),
            'pricingPolicy': 'yearn-only-utc-eod', 'historicalDiscoveryComplete': False,
            'supportedChains': list(self.chains), 'diagnostics': dict(diagnostics),
            'consistentPricedAssetDays': len(selected_prices),
            'sharedPricedAssetDays': len(shared_prices),
        }
        self.id = hashlib.sha256(_json(self.context | {'vaultNames': vault_names}).encode()).hexdigest()

    @lru_cache(maxsize=128)
    def view(self, name, since=None, until=None, chains=(), interval='monthly', vault_address=None):
        if name not in ('summary', 'history', 'vaults'):
            raise ValueError('unsupported pairing view')
        if any(chain not in self.chains for chain in chains):
            raise ValueError('unsupported chain for this dataset')
        if interval not in ('monthly', 'weekly'):
            raise ValueError('interval must be monthly or weekly')
        if vault_address is not None:
            if not re.fullmatch(r'0x[0-9a-fA-F]{40}', vault_address):
                raise ValueError('vaultAddress must be a 20-byte hex address')
            if len(chains) != 1:
                raise ValueError('vaultAddress requires exactly one chainId')
            vault_address = vault_address.lower()
            if (chains[0], vault_address) not in self.vaults:
                raise ValueError('vaultAddress is not in this selected dataset on the requested chain')
        if since is not None and since < 0 or until is not None and until < 0:
            raise ValueError('timestamps must be nonnegative')
        if since is not None and until is not None and since >= until:
            raise ValueError('since must be earlier than until')
        if since is not None and since >= self.cutoff:
            raise ValueError('requested range starts outside this dataset')
        end = min(self.cutoff, until if until is not None else self.cutoff)
        def included(row):
            return ((since is None or row['block_timestamp'] >= since) and row['block_timestamp'] < end
                    and (not chains or row['chain_id'] in chains)
                    and (vault_address is None or row['vault_address'].lower() == vault_address))
        fees = [row for row in self.fees if included(row)]
        earnings = [row for row in self.earnings if included(row)]

        def totals(fee_rows, report_rows):
            pnl = _earnings(report_rows)
            return _fees(fee_rows) | {
                'grossGainsUsd': pnl['grossGainsUsd'], 'lossesUsd': pnl['lossesUsd'],
                'netLifetimeEarningsUsd': pnl['netYieldUsd'], 'lifetimeEarnings': pnl,
                'byContractFamily': [
                    {'contractFamily': family} | _fees([row for row in fee_rows if row['contract_family'] == family])
                    for family in FAMILIES if any(row['contract_family'] == family for row in fee_rows)
                ],
            }
        if name == 'summary':
            result = totals(fees, earnings)
        elif name == 'history':
            buckets = defaultdict(lambda: ([], []))
            for kind, rows in enumerate((fees, earnings)):
                for row in rows:
                    date = datetime.fromtimestamp(row['block_timestamp'], timezone.utc)
                    if interval == 'weekly':
                        start = date.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=date.weekday())
                        period = start.strftime('%Y-%m-%d')
                    else:
                        period = date.strftime('%Y-%m')
                    buckets[period][kind].append(row)
            history = []
            for period in sorted(buckets):
                bucket = {'period': period}
                if interval == 'weekly':
                    start = int(datetime.fromisoformat(period).replace(tzinfo=timezone.utc).timestamp())
                    bucket |= {'startTimestamp': start, 'endTimestamp': start + 7 * 86400}
                history.append(bucket | totals(*buckets[period]))
            result = {'interval': interval, 'buckets': history}
        else:
            buckets = defaultdict(lambda: ([], []))
            for kind, rows in enumerate((fees, earnings)):
                for row in rows:
                    buckets[(row['chain_id'], row['vault_address'].lower())][kind].append(row)
            vaults = []
            for (chain_id, address), (fee_rows, report_rows) in sorted(buckets.items()):
                family = fee_rows[0]['contract_family'] if fee_rows else (
                    FAMILIES[0] if report_rows[0]['version'] == 'v2' else FAMILIES[1])
                vaults.append({'chainId': chain_id, 'address': address,
                               'name': self.vault_names.get(f'{chain_id}:{address}'), 'contractFamily': family}
                              | totals(fee_rows, report_rows))
            result = {'count': len(vaults), 'vaults': vaults}
        return result | {'datasetId': self.id}


def select_pairing(database, earnings_run_id, fees_run_id, publication):
    """Validate first, then atomically select; prior selections remain available."""
    dataset = PairingDataset(database, earnings_run_id, fees_run_id)
    root = Path(publication).resolve()
    snapshots = root / 'datasets'
    snapshots.mkdir(parents=True, exist_ok=True)
    manifest = dataset.context | {'datasetId': dataset.id, 'database': str(dataset.database),
                                  'vaultNames': dataset.vault_names, 'selectedAt': int(time.time())}
    target = snapshots / f'{dataset.id}.json'
    if not target.exists():
        temp = snapshots / f'.{dataset.id}.tmp'
        temp.write_text(_json(manifest) + '\n')
        temp.replace(target)
    temp = root / '.current.tmp'
    temp.write_text(_json({'datasetId': dataset.id}) + '\n')
    temp.replace(root / 'current.json')
    return manifest


class PairingStore:
    def __init__(self, publication):
        self.root = Path(publication).resolve()
        self.lock = threading.Lock()

    @lru_cache(maxsize=2)
    def _load(self, dataset_id):
        manifest = json.loads((self.root / 'datasets' / f'{dataset_id}.json').read_text())
        dataset = PairingDataset(manifest['database'], manifest['earningsRunId'], manifest['feesRunId'],
                                 vault_names=manifest['vaultNames'])
        if dataset.id != dataset_id:
            raise ValueError('selected analysis outputs have changed since publication')
        return dataset

    def get(self, dataset_id=None):
        if dataset_id is None:
            dataset_id = json.loads((self.root / 'current.json').read_text())['datasetId']
        if not isinstance(dataset_id, str) or not re.fullmatch('[a-f0-9]{64}', dataset_id):
            raise ValueError('invalid dataset selection')
        with self.lock:
            return self._load(dataset_id)


def pairing_response(store, url):
    request = urlsplit(url)
    paths = {'/api/fees': 'summary', '/api/fees/': 'summary',
             '/api/fees/history': 'history', '/api/fees/vaults': 'vaults'}
    if request.path not in paths:
        return 404, {'error': 'Not found'}
    try:
        query = parse_qs(request.query, keep_blank_values=True)
        if set(query) - {'since', 'until', 'chainId', 'interval', 'datasetId', 'vaultAddress'}:
            raise ValueError('unsupported filter')
        if any(len(values) != 1 for values in query.values()):
            raise ValueError('filters must appear only once')
        def value(key):
            return query.get(key, [None])[0]
        def timestamp(key):
            raw = value(key)
            if raw is None:
                return None
            if not re.fullmatch(r'\d+', raw):
                raise ValueError(f'{key} must be a nonnegative Unix timestamp')
            return int(raw)
        if value('interval') not in (None, 'monthly', 'weekly'):
            raise ValueError('interval must be monthly or weekly')
        raw_chains = value('chainId')
        if raw_chains is not None and not re.fullmatch(r'[1-9]\d*(,[1-9]\d*)*', raw_chains):
            raise ValueError('chainId must contain positive integers')
        chains = tuple(sorted({int(chain) for chain in raw_chains.split(',')})) if raw_chains else ()
        dataset = store.get(value('datasetId'))
        return 200, dataset.view(paths[request.path], timestamp('since'), timestamp('until'), chains,
                                 value('interval') or 'monthly', value('vaultAddress'))
    except ValueError as error:
        return 400, {'error': str(error)}


def serve_pairing(publication, *, host='127.0.0.1', port=3490, cors_origin=None,
                  tvl_publication=None, current_bridge_policy='retired-registry', comparison_database=None,
                  published_tvl_only=False, analytics=False):
    store = PairingStore(publication)
    dataset = store.get()  # Fail at startup if selection is missing or incompatible.
    tvl_store = None
    if tvl_publication is not None:
        from .tvl_api import TvlStore, tvl_response
        tvl_store = TvlStore(dataset.database, tvl_publication, current_bridge_policy=current_bridge_policy,
                             comparison_database=comparison_database, auto_publish=not published_tvl_only)

    analytics_store = None
    if analytics:
        from .analytics import AnalyticsStore, analytics_response
        analytics_store = AnalyticsStore(dataset.database)
        analytics_store.get()  # Require a validated prepared publication before cutover.

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            try:
                if analytics_store is not None and self.path.split('?')[0] in ('/api/analytics/publication','/api/fees/stack','/api/profitability','/api/comparison','/api/comparison/defillama-comparable'):
                    status, payload = analytics_response(analytics_store, self.path)
                elif tvl_store is not None and (self.path.startswith('/api/tvl') or self.path.startswith('/api/audit/tree') or self.path.split('?')[0]=='/api/comparison'):
                    status, payload = tvl_response(tvl_store, self.path)
                else:
                    status, payload = pairing_response(store, self.path)
            except (OSError, *DATABASE_ERRORS, KeyError, TypeError, json.JSONDecodeError) as error:
                self.log_error('Pairing read failed: %s', error)
                status, payload = 503, {'error': 'Data could not be loaded'}
            body = _json(payload).encode()
            accept_encoding=self.headers.get('Accept-Encoding','')
            gzip_allowed=False
            for encoding in accept_encoding.split(','):
                parts=encoding.strip().split(';')
                if parts[0]=='gzip':
                    quality=next((part.strip()[2:] for part in parts[1:] if part.strip().startswith('q=')),'1')
                    try:gzip_allowed=0<float(quality)<=1
                    except ValueError:pass
            compressed=gzip_allowed and len(body)>1024
            if compressed:body=gzip.compress(body,compresslevel=3,mtime=0)
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Vary','Accept-Encoding')
            if compressed:self.send_header('Content-Encoding','gzip')
            self.send_header('Cache-Control', 'no-cache')
            if cors_origin:
                self.send_header('Access-Control-Allow-Origin', cors_origin)
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer((host, port), Handler)
    print(f'Powerglove pairing API on http://{host}:{server.server_port}', flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
