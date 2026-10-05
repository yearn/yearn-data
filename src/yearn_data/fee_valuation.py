"""Yearn-only closed-day fee valuation; accounting is always read from current rows."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, localcontext
import hashlib
import json
import os
import time

from .analysis import closed_cutoff, create_analysis_run, complete_analysis_run, write_output
from . import pricing

POLICY = 'fee-usd-yearn-eod-1'
FIELDS = ('gross_gain_raw', 'loss_raw', 'total_fees_paid_raw', 'management_fee_raw',
          'performance_fee_raw', 'strategist_fee_raw', 'protocol_fee_raw',
          'manager_fee_raw', 'total_refunds_raw')


def endpoint_identity():
    # Prevent reuse across endpoint configurations; never persist credentials or URLs.
    url = os.environ.get('YEARN_PRICE_PROD_BASE_URL', pricing.YEARN_PRICES_DEFAULT_BASE).strip().rstrip('/')
    return hashlib.sha256(url.encode()).hexdigest()


def fee_events(conn, cutoff):
    rows = conn.execute('''SELECT r.*,f.status,f.reason,f.accounting_json,f.decision_json,
        f.contract_family FROM strategy_reports r LEFT JOIN canonical_fee_reports f
        ON f.chain_id=r.chain_id AND f.tx_hash=r.tx_hash AND f.report_log_index=r.log_index
        WHERE r.version IN ('v2','v3') AND r.block_timestamp<?
        ORDER BY r.chain_id,r.block_timestamp,r.tx_hash,r.log_index''', (cutoff,))
    for row in rows:
        amounts = json.loads(row['accounting_json']) if row['accounting_json'] else None
        decision = json.loads(row['decision_json'] or '{}')
        accepted = (row['status'] == 'ok' and amounts is not None
                    and decision.get('status', 'accepted') == 'accepted'
                    and (amounts.get('amount_source') != 'derived-contract'
                         or decision.get('status') == 'accepted'))
        yield {'chain_id': row['chain_id'], 'vault_address': row['vault_address'],
               'tx_hash': row['tx_hash'], 'log_index': row['log_index'],
               'block_timestamp': row['block_timestamp'], 'asset': row['asset'],
               'asset_decimals': row['asset_decimals'],
               'contract_family': row['contract_family'] or ('yearn-v2-vault' if row['version']=='v2' else 'yearn-v3-allocator'),
               'accounting_status': 'accepted' if accepted else (decision.get('status') or row['status'] or 'not_indexed'),
               'accounting_reason': row['reason'], 'accounting': amounts if accepted else None}
    for row in conn.execute('SELECT event_json FROM tokenized_fee_events ORDER BY chain_id,tx_hash,log_index'):
        event = json.loads(row['event_json'])
        if event['block_timestamp'] < cutoff:
            yield {key: event[key] for key in ('chain_id','vault_address','tx_hash','log_index',
                   'block_timestamp','asset','asset_decimals','contract_family','accounting')} | {
                   'accounting_status': 'accepted', 'accounting_reason': None}


def target_for(event):
    decimals = event['asset_decimals']
    if isinstance(decimals, bool) or not isinstance(decimals, int) or not 0 <= decimals <= 255:
        raise ValueError('invalid asset decimals')
    asset = event['asset']
    if not isinstance(asset, str):
        raise ValueError('missing asset')
    pricing.yearn_prices_token_key(event['chain_id'], asset)
    return event['chain_id'], asset.lower(), pricing.normalize_yearn_price_timestamp(event['block_timestamp'])


def cached_prices(conn):
    return {(r['chain_id'], r['asset'], r['eod_timestamp']): dict(r) for r in conn.execute(
        'SELECT * FROM fee_daily_prices WHERE policy=? AND endpoint_id=?', (POLICY, endpoint_identity()))}


def save_price(conn, target, result):
    price, status, evidence = result
    if status == 'ok':
        value = Decimal(str(price))
        if not value.is_finite() or value <= 0:
            raise ValueError('invalid positive USD price')
        if evidence.get('provider') != 'yearn-prices' or evidence.get('normalized_timestamp') != target[2]:
            raise ValueError('price evidence does not match requested provider/day')
        price = format(value, 'f')
    else:
        price = None
    conn.execute('''INSERT OR REPLACE INTO fee_daily_prices
        (chain_id,asset,eod_timestamp,policy,endpoint_id,price_usd,status,evidence_json,fetched_at)
        VALUES (?,?,?,?,?,?,?,?,?)''', (*target, POLICY, endpoint_identity(), price, status,
                                      json.dumps(evidence, sort_keys=True), int(time.time())))


def price_fees(conn, *, before_timestamp=None, limit=500, retry_missing=False, refresh=False, targets=None):
    """Bounded deduplicated requests. Failed batches never fan out to exact requests."""
    cutoff = closed_cutoff(before_timestamp)
    if limit <= 0:
        raise ValueError('price target limit must be positive')
    cache = cached_prices(conn)
    requested = targets
    targets = set()
    for event in fee_events(conn, cutoff):
        amounts = event['accounting']
        if not amounts or not any(int(amounts.get(f) or 0) != 0 for f in FIELDS):
            continue
        try:
            target = target_for(event)
        except ValueError:
            continue  # Export retains metadata gaps; do not guess an asset or decimals.
        if requested is not None and target not in requested:
            continue
        prior = cache.get(target)
        if refresh or prior is None or (retry_missing and prior['status'] != 'ok'):
            targets.add(target)
    # New targets first, then the least recently attempted failures. Persistent
    # missing prices must not starve later asset-days on repeated bounded runs.
    selected = sorted(targets, key=lambda target: (cache.get(target, {}).get('fetched_at', 0), target))[:limit]
    counts = Counter()
    for batch in pricing._yearn_prices_batches(selected):
        # Bound GET URL size as well as the service's per-token timestamp limit.
        for offset in range(0, len(batch), 100):
            chunk = batch[offset:offset+100]
            try:
                results = pricing.fetch_yearn_prices_batch(chunk)
            except pricing.YearnPricesRequestError as error:
                status = 'retryable' if error.retryable else 'invalid'
                results = {target: (None, status, {'provider':'yearn-prices',
                    'normalized_timestamp':target[2], 'adapter':'batchHistorical',
                    'failure_class':status}) for target in chunk}
            except ValueError:
                # Configuration/authentication errors must stop the run, not become missing prices.
                pricing._yearn_prices_config()
                results = {target: (None, 'invalid', {'provider':'yearn-prices',
                    'normalized_timestamp':target[2], 'adapter':'batchHistorical',
                    'failure_class':'invalid_response'}) for target in chunk}
            for target in chunk:
                result = results.get(target)
                if result is None:
                    try:
                        result = pricing.fetch_yearn_price(*target)
                    except ValueError:
                        result = (None, 'invalid', {'provider':'yearn-prices',
                            'normalized_timestamp':target[2], 'adapter':'historical',
                            'failure_class':'invalid_response'})
                save_price(conn, target, result)
                counts[result[1]] += 1
            conn.commit()  # Resume completed chunks without repeating acquisition.
    return {'selected_asset_days':len(selected), 'remaining_asset_days':len(targets)-len(selected),
            'results':dict(counts), 'before_timestamp':cutoff, 'policy':POLICY}


def usd_amount(raw, decimals, price):
    if raw is None:
        return None
    value = int(raw)
    if value < 0:
        raise ValueError('negative unsigned fee amount')
    if value == 0:
        return '0'
    if price is None:
        return None
    with localcontext() as ctx:
        ctx.prec = 400
        return format(Decimal(value) * Decimal(price) / (Decimal(10) ** decimals), 'f')


def value_event(event, cache):
    amounts = event['accounting']
    price = None
    evidence = None
    timestamp = pricing.normalize_yearn_price_timestamp(event['block_timestamp'])
    status = 'accounting_unavailable'
    target = None
    if amounts is not None:
        try:
            target = target_for(event)
        except ValueError:
            status = 'metadata_unavailable'
        else:
            evidence = cache.get(target)
            status = evidence['status'] if evidence else 'not_requested'
            if status == 'ok':
                price = evidence['price_usd']
        if int(amounts['total_fees_paid_raw']) == 0:
            status = 'zero_fee'
    output = {key:value for key,value in event.items() if key != 'accounting'}
    output.update(day=datetime.fromtimestamp(timestamp, timezone.utc).date().isoformat(),
                  eod_timestamp=timestamp, valuation_policy=POLICY, price_provider='yearn-prices',
                  price_usd=price, price_status=evidence['status'] if evidence else 'not_requested',
                  price_evidence_json=evidence['evidence_json'] if evidence else None,
                  price_fetched_at=evidence['fetched_at'] if evidence else None,
                  valuation_status=status)
    for field in FIELDS:
        raw = amounts.get(field) if amounts else None
        output[field] = raw
        output[field.replace('_raw','_usd')] = usd_amount(raw, event['asset_decimals'], price)
    return output


def read_deferrals(path):
    if path is None:
        return {}, None
    data = path.read_bytes()
    entries = json.loads(data)
    if not isinstance(entries, list):
        raise ValueError('deferred manifest must be a list')
    result = {}
    for entry in entries:
        key = (entry['chain_id'], entry['tx'].lower(), entry['log_index'])
        if (key in result or entry.get('accepted') is not False
                or entry.get('fee_value_raw') is not None
                or entry.get('disposition') != 'deferred_execution_evidence'
                or not entry.get('reason')):
            raise ValueError('invalid or duplicate deferred event')
        result[key] = entry
    return result, hashlib.sha256(data).hexdigest()


def run_fee_usd(conn, *, before_timestamp=None, deferred_manifest=None):
    """Offline event/day/vault/asset/chain/family reports; no persisted stale USD rows."""
    cutoff = closed_cutoff(before_timestamp)
    cache = cached_prices(conn)
    deferrals, manifest_hash = read_deferrals(deferred_manifest)
    seen_deferrals = set()
    run_id = create_analysis_run(conn, 'fee-usd', {'policy':POLICY, 'before_timestamp':cutoff,
        'provider':'yearn-prices', 'endpoint_id':endpoint_identity(),
        'deferred_manifest_sha256':manifest_hash, 'scope':'stored_reports_and_tokenized_events_only', 'historical_discovery_complete':False})
    groups = {name:{} for name in ('family','day','vault','asset','chain')}
    with localcontext() as ctx:
        ctx.prec = 400
        for event in fee_events(conn, cutoff):
            identity = (event['chain_id'], event['tx_hash'].lower(), event['log_index'])
            deferred = deferrals.get(identity)
            if deferred:
                if (event['accounting'] is not None
                        or event['vault_address'].lower() != deferred['vault'].lower()
                        or event['asset'].lower() != deferred['asset'].lower()
                        or event['asset_decimals'] != deferred['asset_decimals']
                        or event['block_timestamp'] != deferred['timestamp']):
                    raise ValueError('deferred manifest conflicts with current accounting or event identity')
                seen_deferrals.add(identity)
                event['accounting_status'] = 'deferred'
                event['accounting_reason'] = deferred['reason']
            row = value_event(event, cache)
            write_output(conn, run_id, 'fee_usd_events', row)
            family = row['contract_family']
            dimensions = {'family':(family,), 'day':(family,row['day']),
                          'vault':(family,row['chain_id'],row['vault_address']),
                          'asset':(family,row['chain_id'],(row['asset'] or '').lower(),row['asset_decimals']),
                          'chain':(family,row['chain_id'])}
            for name,key in dimensions.items():
                bucket = groups[name].setdefault(key, {'events':0,'accounting_unavailable_events':0,
                    'deferred_accounting_events':0,'metadata_unavailable_events':0,'unpriced_fee_events':0,'known_fee_events':0,
                    'zero_fee_events':0,'totals':{f:Decimal(0) for f in FIELDS},
                    'known':{f:0 for f in FIELDS},'statuses':Counter()})
                bucket['events'] += 1
                bucket['deferred_accounting_events'] += row['accounting_status']=='deferred'
                status = row['valuation_status'];bucket['statuses'][status] += 1
                bucket['accounting_unavailable_events'] += status == 'accounting_unavailable'
                bucket['metadata_unavailable_events'] += status == 'metadata_unavailable'
                fee = row['total_fees_paid_usd']
                bucket['known_fee_events'] += fee is not None
                bucket['zero_fee_events'] += status == 'zero_fee'
                bucket['unpriced_fee_events'] += (fee is None and status not in ('accounting_unavailable','metadata_unavailable'))
                for field in FIELDS:
                    amount = row[field.replace('_raw','_usd')]
                    if amount is not None:
                        bucket['totals'][field] += Decimal(amount)
                        bucket['known'][field] += 1
        if any(key not in seen_deferrals and entry['timestamp'] < cutoff for key,entry in deferrals.items()):
            raise ValueError('deferred manifest contains events absent from the report dataset')
        names = {'family':('contract_family',),'day':('contract_family','day'),
                 'vault':('contract_family','chain_id','vault_address'),
                 'asset':('contract_family','chain_id','asset','asset_decimals'),
                 'chain':('contract_family','chain_id')}
        for name,buckets in groups.items():
            for key,bucket in buckets.items():
                totals=bucket.pop('totals');known=bucket.pop('known');statuses=bucket.pop('statuses')
                output = dict(zip(names[name],key)) | bucket
                output.update(valuation_policy=POLICY, before_timestamp=cutoff, deferred_manifest_sha256=manifest_hash,
                    scope='stored_reports_and_tokenized_events_only', historical_discovery_complete=False,
                    accepted_fee_events=bucket['events']-bucket['accounting_unavailable_events'],
                    accounting_complete=bucket['accounting_unavailable_events']==0,
                    pricing_complete_for_accepted_fees=(bucket['metadata_unavailable_events']==0 and bucket['unpriced_fee_events']==0),
                    fee_usd_complete=bucket['known_fee_events']==bucket['events'],
                    valuation_status_counts_json=json.dumps(dict(statuses),sort_keys=True))
                for field in FIELDS:
                    usd = field.replace('_raw','_usd')
                    output['known_'+usd+'_subtotal'] = format(totals[field],'f') if known[field] else None
                    output[usd+'_known_events'] = known[field]
                    output[usd+'_unknown_events'] = bucket['events']-known[field]
                write_output(conn, run_id, 'fee_usd_by_'+name, output)
    complete_analysis_run(conn, run_id)
    return run_id
