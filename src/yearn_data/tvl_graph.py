"""Powerglove flow graph from dated accounting, without a second TVL ledger."""
from collections import defaultdict
from decimal import Decimal, localcontext

from .tvl import account, decimal


def node_id(chain, address):
    return f'{chain}:{address.lower()}'


def amount(value):
    return float(decimal(value)) if value is not None else None


def build_graph(dataset, rows, positions, diagnostics, *, chain_id=None, category=None):
    with localcontext() as ctx:
        ctx.prec = 78
        return _build_graph(dataset,rows,positions,diagnostics,chain_id=chain_id,category=category)


def _build_graph(dataset, rows, positions, diagnostics, *, chain_id, category):
    # Flows into curation exist even when that layer is intentionally counted
    # separately. Keep physical flow and headline deduction as distinct fields.
    _, flows, _ = account(rows, [p for p in positions if not p.get('accounting_excluded')], include_curation=True)
    deductions = {(p['chain_id'],p['parent'],p['strategy'],p['child']):p.get('overlap_usd') for p in positions}
    nodes, bridges, unavailable = {}, [], []
    gross_by_id = {node_id(r['chain_id'],r['vault']):decimal(r.get('tvl_usd')) for r in rows}
    for row in rows:
        identity = node_id(row['chain_id'],row['vault'])
        meta = dataset.catalog.get((row['chain_id'],row['vault'].lower()),{})
        if row.get('bridge_excluded_usd') is not None:
            bridges.append({'sourceNodeId':identity,'chainId':row['chain_id'],'address':row['vault'],
                            'name':row.get('name'),'category':row.get('category',row['version']),
                            'targetChainId':row['bridge_target_chain'],'tvlUsd':amount(row['bridge_excluded_usd']),
                            'label':'Dated pre-deposit migration' if row.get('bridge_migration_timestamp') is not None else 'Retired bridge registry'})
            continue
        if row.get('tvl_usd') is None:
            unavailable.append({'nodeId':identity,'reason':row.get('reason','missing_valuation')})
            continue
        nodes[identity] = {'id':identity,'chainId':row['chain_id'],'address':row['vault'],
                           'name':row.get('name') or row['vault'],'nodeType':'vault',
                           'category':row.get('category',row['version']),
                           'vaultType':1 if (row['chain_id'],row['vault'].lower()) in dataset.allocators else None,
                           'apiVersion':meta.get('api_version'),'rawTvlUsd':amount(row['tvl_usd']),
                           'externalTvlUsd':amount(row['external_tvl_usd']),
                           'upstreamOwnedTvlUsd':amount(row['overlap_usd']),
                           'isRetired':not meta.get('active',1)}

    unavailable_ids = {r['nodeId'] for r in unavailable}
    allocations = {}
    reserved = defaultdict(lambda: Decimal(0))
    direct_flows = defaultdict(lambda: Decimal(0))
    edge_values = defaultdict(lambda: [Decimal(0),Decimal(0)])
    edge_metadata = {}

    def add(source, target, holder, kind, value, deducted, value_source):
        if source == target or value is None or decimal(value) == 0:
            return
        identity = f'{kind}:{source}:{target}:{holder.lower()}'
        edge_values[identity][0] += decimal(value)
        edge_values[identity][1] += decimal(deducted) or Decimal(0)
        edge_metadata[identity] = {'id':identity,'sourceNodeId':source,'targetNodeId':target,
                                   'holderAddress':holder,'kind':kind,'valueSource':value_source}

    for position in flows:
        chain, parent, holder, child = (position[k] for k in ('chain_id','parent','strategy','child'))
        source = node_id(chain,parent)
        target = node_id(position.get('child_chain_id',chain),child)
        strategy = node_id(chain,holder)
        if source not in nodes or position.get('status') != 'ok':
            continue
        debt = position.get('debt_usd')
        if debt is None:
            continue
        deducted = deductions.get((chain,parent,holder,child))
        flow = position.get('overlap_usd')
        if strategy == source:
            if target in nodes:
                add(source,target,holder,'auto',flow,deducted,'dated-vault-shares')
                direct_flows[source] += decimal(flow) or Decimal(0)
        elif strategy == target and target in nodes:
            add(source,target,holder,'auto',flow,deducted,'dated-strategy-debt')
            direct_flows[source] += decimal(flow) or Decimal(0)
        else:
            if strategy in unavailable_ids:
                continue
            # One parent debt, however many downstream children it funds.
            allocations[(source,strategy,holder)] = debt
            if target in nodes and flow is not None:
                add(strategy,target,position.get('remote_holder',holder),'registry',flow,deducted,'dated-held-shares')
                reserved[(source,strategy,holder)] += decimal(flow)

    synthetic_values = defaultdict(lambda: Decimal(0))
    by_source = defaultdict(list)
    for identity in allocations:
        by_source[identity[0]].append(identity)
    for source,identities in by_source.items():
        # Retired V2 contracts can retain strategy debt above the vault's assets.
        # Preserve verified downstream flow first, then share the remaining TVL
        # budget among ordinary allocations. Retain uncapped debt as evidence.
        available = max(Decimal(0),gross_by_id[source]-direct_flows[source])
        required = sum((reserved[k] for k in identities),Decimal(0))
        excess = {k:max(Decimal(0),decimal(allocations[k])-reserved[k]) for k in identities}
        total_excess = sum(excess.values(),Decimal(0))
        remaining = max(Decimal(0),available-required)
        for identity in identities:
            _,strategy,holder=identity
            value=reserved[identity]+excess[identity]*min(Decimal(1),remaining/total_excess) if total_excess else reserved[identity]
            value_source = ('parent-tvl-capped-debt-estimate' if value < decimal(allocations[identity])
                            else 'dated-strategy-debt')
            add(source,strategy,holder,'strategy',value,'0',value_source)
            edge_id=f'strategy:{source}:{strategy}:{holder.lower()}'
            if edge_id in edge_metadata:
                edge_metadata[edge_id]['recordedDebtUsd']=amount(allocations[identity])
            synthetic_values[strategy] += value
    for identity,value in synthetic_values.items():
        if identity in nodes or value == 0:
            continue
        chain,address = identity.split(':',1)
        nodes[identity] = {'id':identity,'chainId':int(chain),'address':address,'name':address,
                          'nodeType':'strategy','category':'strategy','vaultType':None,'apiVersion':None,
                          'rawTvlUsd':float(value),'externalTvlUsd':0,'upstreamOwnedTvlUsd':float(value),'isRetired':False}
    edges = [edge_metadata[k] | {'tvlUsd':float(v[0]),'deductedTvlUsd':float(v[1])}
             for k,v in edge_values.items()
             if edge_metadata[k]['sourceNodeId'] in nodes and edge_metadata[k]['targetNodeId'] in nodes]

    def in_scope(node):
        return (chain_id is None or node['chainId']==chain_id) and (category is None or node['category']==category)

    # Category/chain choose roots and totals. Retain connected upstream context,
    # including V2 routers and cross-chain senders for V3/Katana views.
    included = {k for k,n in nodes.items() if n['nodeType']=='vault' and in_scope(n)}
    adjacent = defaultdict(set)
    for edge in edges:
        adjacent[edge['sourceNodeId']].add(edge['targetNodeId'])
        adjacent[edge['targetNodeId']].add(edge['sourceNodeId'])
    pending = list(included)
    while pending:
        for identity in adjacent[pending.pop()] - included:
            included.add(identity)
            pending.append(identity)
    selected = [n for n in nodes.values() if n['id'] in included]
    edges = [e for e in edges if e['sourceNodeId'] in included and e['targetNodeId'] in included]
    bridges = [b for b in bridges if in_scope(b)]
    scope_rows = [r for r in rows if (chain_id is None or r['chain_id']==chain_id)
                  and (category is None or r.get('category',r['version'])==category)]

    def total(field):
        values = [decimal(r[field]) for r in scope_rows if r.get(field) is not None]
        return float(sum(values,Decimal(0))) if values or not scope_rows else None

    raw, external = total('tvl_usd'), total('external_tvl_usd')
    return {'datasetId':dataset.id,'asOfTimestamp':dataset.as_of(),'snapshotAt':dataset.as_of(),
            'totalTvlUsd':external,'totalRawTvlUsd':raw,
            'totalOverlapTvlUsd':raw-external if raw is not None and external is not None else None,
            'nodes':sorted(selected,key=lambda n:(-n['rawTvlUsd'],n['id'])),
            'edges':sorted(edges,key=lambda e:(-e['tvlUsd'],e['id'])),
            'bridgeExclusions':sorted(bridges,key=lambda b:b['sourceNodeId']),
            'diagnostics':{'status':diagnostics['status'],'mappingScope':'known candidates',
                           'unavailableNodes':unavailable,'unresolvedPositions':diagnostics['unresolved_positions']}}
