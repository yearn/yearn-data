"""Symmetric, evidence-backed scope and overlap comparison for a pinned TVL close."""
from decimal import Decimal


def discrepancy_details(rows, evidence, dataset_id):
    """Do not mix inventory/adapter evidence from another published dataset."""
    if not evidence or evidence.get('tvlDatasetId') != dataset_id:
        return {'state': 'unavailable', 'reason': 'Bidirectional evidence is not prepared for this TVL dataset'}
    local = {(r['chain_id'], r['vault'].lower()): r for r in rows}
    external = {(v['protocol'], v['chainId'], v['address'].lower()): v for v in evidence['vaults']}
    categories = []
    def value(number):
        return None if number is None else float(Decimal(str(number)))
    def entry(row, address=None):
        return {'chainId': row['chain_id'], 'vaultAddress': address or row['vault'],
                'name': row.get('name'), 'category': row.get('category'),
                'oursUsd': value(row.get('external_tvl_usd'))}
    for protocol, curation in [('yearn-finance', False), ('yearn-curating', True)]:
        ours_only, theirs_only, unresolved = [], [], []
        for identity, row in local.items():
            if (row.get('category') == 'curation') != curation:
                continue
            amount = value(row.get('external_tvl_usd'))
            if amount is None or amount <= 0 or (protocol, *identity) in external:
                continue
            record = entry(row)
            if curation and identity[0] in evidence.get('coverage', {}).get('incompleteChains', []):
                unresolved.append(record | {'reason': 'External factory inventory incomplete; exclusion unverified'})
            else:
                ours_only.append(record | {'defillamaUsd': 0, 'reason': 'Outside the independently checked adapter scope'})
        for (target, chain, address), observation in external.items():
            if target != protocol or Decimal(observation.get('units') or '0') <= 0:
                continue
            row = local.get((chain, address))
            # A zero net contribution is not a scope exclusion: the parent and child
            # attribution differs between the two implementations.
            if row is not None and row.get('tvl_usd') is not None:
                continue
            amount = value(observation.get('replayTvlUsd'))
            record = {'chainId': chain, 'vaultAddress': address, 'name': observation.get('name'),
                      'category': observation.get('category'), 'oursUsd': None if row else 0,
                      'defillamaUsd': amount,
                      'reason': 'Missing local price' if row else 'Missing from our snapshot',
                      'valuationBasis': 'Adapter replay with a historical DefiLlama token quote',
                      'priceTimestamp': (observation.get('quote') or {}).get('timestamp')}
            if amount is None:
                unresolved.append(record | {'reason': 'In adapter scope; a priced external contribution is not verified'})
            elif amount > 0:
                theirs_only.append(record)
        checks = [r for r in evidence.get('overlapChecks', []) if r['protocol'] == protocol]
        missed = sorted((r | {'ourDeductionUsd': value(r['ourDeductionUsd'])} for r in checks if r['status'] == 'missed'),
                        key=lambda r: -r['ourDeductionUsd'])
        handled = [r | {'ourDeductionUsd': value(r['ourDeductionUsd'])} for r in checks if r['status'] == 'handled']
        categories.append({'protocol': protocol,
                           'oursOnly': sorted(ours_only, key=lambda r: -r['oursUsd']),
                           'theirsOnly': sorted(theirs_only, key=lambda r: -r['defillamaUsd']),
                           'missedOverlaps': missed, 'handledOverlaps': handled,
                           'unresolvedVaults': unresolved,
                           'unverifiedOverlaps': [r for r in checks if r['status'] == 'unverified']})
    return {'state': 'partial', 'datasetId': dataset_id, 'asOfTimestamp': evidence['asOfTimestamp'],
            'categories': categories, 'coverage': evidence['coverage'], 'sources': evidence['sources']}
