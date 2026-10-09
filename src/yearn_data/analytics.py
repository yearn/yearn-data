"""Prepared Powerglove analytics. Collection and calculation happen off the read path."""
from collections import defaultdict
from contextlib import closing
from decimal import Decimal
from functools import lru_cache
import hashlib
import json
import math
import re
from statistics import median
import time
from urllib.parse import parse_qs, urlsplit

from .storage import connect, table_exists
from .tvl_api import CHAINS, encoded, key, money

DAY = 86400
YEAR = 365 * DAY
VERSION = 'powerglove-analytics-3'
SCHEMA = '''
CREATE TABLE IF NOT EXISTS analytics_inputs (
 id TEXT PRIMARY KEY, kind TEXT NOT NULL, observed_at INTEGER NOT NULL,
 status TEXT NOT NULL, body_json TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS analytics_inputs_kind_idx ON analytics_inputs(kind,observed_at);
CREATE TABLE IF NOT EXISTS analytics_publications (
 id TEXT PRIMARY KEY, created_at INTEGER NOT NULL, body_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS analytics_selection (
 name TEXT PRIMARY KEY, publication_id TEXT NOT NULL REFERENCES analytics_publications(id));
'''


def put_input(conn, kind, body, status='available', observed_at=None):
    observed_at = int(time.time()) if observed_at is None else observed_at
    previous = selected_input(conn,kind)
    if previous['id']:
        body = dict(body)
        field = 'snapshots' if kind=='defillama' else 'vaults'
        def identity(row):
            return row['protocol'] if kind=='defillama' else (row['chainId'],row['address'].lower())
        old = {identity(row):row for row in previous['body'].get(field,[])}
        merged = dict(old)
        for row in body.get(field,[]):
            key = identity(row)
            if kind=='membership' and row.get('included') is None and key in old and old[key].get('included') is not None:
                row = old[key] | {'retainedAfterUnavailableRetry':True,'retainedFromInputId':previous['id']}
            elif kind=='fee-config' and key in old:
                row = dict(row)
                for name in ('performanceFee','managementFee'):
                    if row.get(name) is None and old[key].get(name) is not None:
                        row[name]=old[key][name]
                        row['retainedAfterUnavailableRetry']=True
                        row['retainedFromInputId']=previous['id']
                        row['retainedObservedAt']=old[key].get('observedAt')
            merged[key]=row
        body[field]=list(merged.values())
    data = {'kind':kind,'observedAt':observed_at,'status':status,'body':body}
    identity = hashlib.sha256(encoded(data).encode()).hexdigest()
    with conn:
        conn.execute('INSERT INTO analytics_inputs VALUES (?,?,?,?,?) ON CONFLICT DO NOTHING',
                     (identity,kind,observed_at,status,encoded(body)))
    return identity


def selected_input(conn, kind):
    # Failed acquisitions remain diagnostic evidence; they cannot erase known inputs.
    row = conn.execute("SELECT * FROM analytics_inputs WHERE kind=? AND status IN ('available','partial') ORDER BY observed_at DESC,id DESC LIMIT 1",(kind,)).fetchone()
    if row is None:
        return {'id':None,'kind':kind,'observed_at':None,'status':'unavailable','body':{}}
    return dict(row)|{'body':json.loads(row['body_json'])}


def ratio(numerator, denominator):
    return numerator / denominator if numerator is not None and denominator is not None and denominator > 0 else None


def amount(rows, field):
    values = [Decimal(str(r[field])) for r in rows if r.get(field) is not None]
    return float(sum(values,Decimal(0))) if values else (0.0 if not rows else None)


def coverage(rows, field):
    known = sum(r.get(field) is not None for r in rows)
    return {'state':'available' if known==len(rows) else 'partial' if known else 'unavailable',
            'known':known,'expected':len(rows)}


def fee_stack(rows, positions, configurations):
    by_key = {key(r):r for r in rows}
    by_parent = defaultdict(list)
    issues = []
    for edge in positions:
        parent = (edge['chain_id'],edge['parent'].lower())
        if edge.get('accounting_excluded'):continue
        child = (edge['chain_id'],(edge.get('child') or edge['strategy']).lower())
        value = edge.get('debt_usd',edge.get('allocated_usd'))
        if edge.get('status')!='ok' or value is None:
            issues.append({'parent':list(parent),'strategy':edge['strategy'],'reason':'unavailable allocation'})
        elif float(value)>0 and child in by_key:
            by_parent[parent].append((child,float(value),edge['strategy'].lower()))
    configs = {(r['chainId'],r['address'].lower()):r for r in configurations.get('vaults',[])}
    def build(identity, capital, visited):
        row=by_key[identity]; cfg=configs.get(identity,{})
        perf,mgmt=cfg.get('performanceFee'),cfg.get('managementFee')
        complete = not any(x['parent']==list(identity) for x in issues)
        if identity in visited or len(visited)>=10:
            complete=False
            children=[]
        else:
            gross=float(row['tvl_usd'] or 0)
            share=min(capital/gross,1) if gross>0 else 0
            remaining=capital; children=[]
            for child,debt,strategy in sorted(by_parent[identity]):
                child_gross=by_key[child]['tvl_usd']
                if child_gross is None:complete=False;continue
                allocated=min(remaining,debt*share,float(child_gross))
                if allocated<=0:continue
                remaining-=allocated
                node=build(child,allocated,visited|{identity})
                node['viaStrategy']=strategy
                children.append(node)
                complete=complete and node['availability']['state']=='available'
        if perf is None or mgmt is None or not cfg.get('fixedRateSupported',True):complete=False
        return {'vault':{'address':identity[1],'chainId':identity[0],'name':row.get('name')},
                'capitalUsd':capital,'perfFee':perf,'mgmtFee':mgmt,'children':children,
                'availability':{'state':'available' if complete else 'partial'}}
    def effective(root):
        leaves=[]
        def walk(node,product,management):
            product = product*(1-node['perfFee']/10000) if product is not None and node['perfFee'] is not None else None
            management = management+node['mgmtFee'] if management is not None and node['mgmtFee'] is not None else None
            residual=max(0,node['capitalUsd']-sum(c['capitalUsd'] for c in node['children']))
            if residual:leaves.append((residual, None if product is None else (1-product)*10000,management))
            for child in node['children']:walk(child,product,management)
        walk(root,1,0)
        def weighted(index):
            if root['availability']['state']!='available' or not leaves or any(r[index] is None for r in leaves):return None
            return sum(r[0]*r[index] for r in leaves)/sum(r[0] for r in leaves)
        return weighted(1),weighted(2)
    def depth(node):return 1+max((depth(c) for c in node['children']),default=0)
    chains=[]
    for identity in sorted(by_key):
        row=by_key[identity]
        if row.get('tvl_usd') is None or float(row['tvl_usd'])<=0 or not by_parent[identity]:continue
        root=build(identity,float(row['tvl_usd']),set());perf,mgmt=effective(root)
        chains.append({'root':root,'maxDepth':depth(root),'effectivePerfFee':perf,'effectiveMgmtFee':mgmt})
    rates=[c['effectivePerfFee'] for c in chains if c['effectivePerfFee'] is not None]
    # External fractions attribute the same capital only once even with nested roots.
    identities={(c['root']['vault']['chainId'],c['root']['vault']['address']) for c in chains}
    capital=money([by_key[i] for i in identities],'external_tvl_usd')
    return {'chains':chains,'maxDepth':max((c['maxDepth'] for c in chains),default=0),
            'maxEffectivePerfFee':max(rates) if rates else None,'avgEffectivePerfFee':sum(rates)/len(rates) if rates else None,
            'totalStackedCapital':capital,'availability':{'state':'available' if len(rates)==len(chains) and not issues else 'partial',
             'knownStacks':len(rates),'expectedStacks':len(chains),'unresolvedAllocations':issues}}


def comparable(rows, membership):
    observations={(r['chainId'],r['address'].lower()):r for r in membership.get('vaults',[])}
    missing=[]; verified=[]; unknown=[]
    for row in rows:
        value=row.get('external_tvl_usd')
        if value is None or float(value)<=0:continue
        identity=key(row); match=observations.get(identity,{})
        entry={'vaultAddress':identity[1],'chainId':identity[0],'chainName':CHAINS.get(identity[0],str(identity[0])),
               'name':row.get('name'),'category':row.get('category'),'countedTvlUsd':float(value)}
        if match.get('included') is True:verified.append(entry)
        elif match.get('included') is False:missing.append(entry)
        else:unknown.append(entry|{'reason':match.get('reason','membership not observed')})
    return {'diff':{'missingFromDefillama':missing},'includedVaults':verified,
            'availability':{'state':'available' if not unknown else 'partial','unknownVaults':unknown,
                            'known':len(missing)+len(verified),'expected':len(missing)+len(verified)+len(unknown)}}


def capital_period(points, start, end):
    """Equal duration UTC daily observations, no gap filling, no prehistory zeros."""
    by_day={t//DAY:float(value) for t,value in points if start<=t<end and value is not None and float(value)>=0}
    if not by_day:return {'average':None,'days':0,'expectedDays':math.ceil((end-start)/DAY),'eligible':False}
    first=max(start,min(by_day)*DAY)
    expected=max(1,math.ceil((end-first)/DAY));days=len(by_day)
    return {'average':sum(by_day.values())/days,'days':days,'expectedDays':expected,
            'eligible':days>=90 and days/expected>=0.8}


def profitability(financial, rows, history, configurations, cutoff, chain_id=None):
    start=cutoff-YEAR; middle=start+(cutoff-start)//2
    current={(r['chain_id'],r['vault'].lower()):r for r in rows}
    cfg={(r['chainId'],r['address'].lower()):r for r in configurations.get('vaults',[])}
    fees=defaultdict(list);gains=defaultdict(list)
    for data,target in ((financial.fees,fees),(financial.earnings,gains)):
        for row in data:
            if start<=row['block_timestamp']<cutoff and (chain_id is None or row['chain_id']==chain_id):
                target[(row['chain_id'],row['vault_address'].lower())].append(row)
    keys=set(fees)|set(gains)|{k for k in current if chain_id is None or k[0]==chain_id}
    vaults=[]
    for identity in sorted(keys):
        fee_rows=fees[identity];gain_rows=gains[identity];points=history.get(identity,[])
        cap=capital_period(points,start,cutoff)
        eligibility=cap['eligible'] and cap['average'] is not None and cap['average']>0
        earliest=min((r['block_timestamp'] for r in fee_rows+gain_rows),default=cutoff)
        first=min((t for t,v in points if start<=t<cutoff and v is not None),default=cutoff)
        if earliest<first-DAY:eligibility=False
        fee=amount(fee_rows,'total_fees_paid_usd') if fee_rows else None;gain=amount(gain_rows,'gross_gain_usd') if gain_rows else None
        fcoverage=coverage(fee_rows,'total_fees_paid_usd');gcoverage=coverage(gain_rows,'gross_gain_usd')
        if not fee_rows:fcoverage={'state':'unavailable','known':0,'expected':0,'reason':'no fee observations in calculation period'}
        if not gain_rows:gcoverage={'state':'unavailable','known':0,'expected':0,'reason':'no gain observations in calculation period'}
        annual=fee*365/cap['expectedDays'] if eligibility and fcoverage['state']=='available' else None
        fee_yield=ratio(annual,cap['average'])
        gain_yield=ratio(gain*365/cap['expectedDays'],cap['average']) if eligibility and gain is not None and gcoverage['state']=='available' else None
        def period_yield(a,b):
            capital=capital_period(points,a,b);reports=[r for r in fee_rows if a<=r['block_timestamp']<b]
            value=amount(reports,'total_fees_paid_usd')
            if not capital['eligible'] or not reports or coverage(reports,'total_fees_paid_usd')['state']!='available':return None
            return ratio(value*365/capital['expectedDays'],capital['average']) if value is not None else None
        previous=period_yield(start,middle);recent=period_yield(middle,cutoff)
        delta=recent-previous if previous is not None and recent is not None else None
        trend='insufficient_data' if delta is None or len(gain_rows)<3 else 'improving' if delta>0.005 else 'declining' if delta < -0.005 else 'stable'
        priced=fcoverage['known']/fcoverage['expected'] if fcoverage['expected'] else 0
        row=current.get(identity,{});config=cfg.get(identity,{})
        vaults.append({'address':identity[1],'chainId':identity[0],'name':row.get('name') or financial.vault_names.get(f'{identity[0]}:{identity[1]}'),
            'category':row.get('category','v3' if any(r.get('version')=='v3' for r in gain_rows) else 'v2'),
            'tvlUsd':None if row.get('tvl_usd') is None else float(row['tvl_usd']),
            'timeWeightedTvlUsd':cap['average'],'annualizedFeeRevenue':annual,'feeYield':fee_yield,
            'feeCapture':ratio(fee,gain) if fcoverage['state']==gcoverage['state']=='available' else None,
            'gainYield':gain_yield,'trend':trend,'trendDelta':delta,
            'pricingConfidence':'high' if priced>=0.8 else 'medium' if priced>=0.4 else 'low',
            'reportCount':len(gain_rows),'avgHarvestFrequencyDays':cap['expectedDays']/len(gain_rows) if gain_rows else None,
            'performanceFee':config.get('performanceFee'),'managementFee':config.get('managementFee'),
            'totalGainUsd':gain,'totalFeeRevenue':fee,'quadrant':None,'currentPeriodFeeYield':recent,
            'previousPeriodFeeYield':previous,'availability':{'state':'available' if eligibility and fcoverage['state']==gcoverage['state']=='available' else 'partial',
                'tvlDays':cap['days'],'expectedTvlDays':cap['expectedDays'],'fees':fcoverage,'gains':gcoverage}})
    eligible=[v for v in vaults if v['feeYield'] is not None and v['timeWeightedTvlUsd'] is not None]
    displayed=[v for v in eligible if v['tvlUsd'] is not None and 1e4<=v['tvlUsd']<=1e8]
    tvl_median=median(v['tvlUsd'] for v in displayed) if displayed else None
    yield_median=median(v['feeYield'] for v in displayed) if displayed else None
    names=['high_tvl_high_yield','high_tvl_low_yield','low_tvl_high_yield','low_tvl_low_yield']
    quadrants={name:[] for name in names}
    for v in displayed:
        v['quadrant']=('high_tvl' if v['tvlUsd']>=tvl_median else 'low_tvl')+('_high_yield' if v['feeYield']>yield_median else '_low_yield')
        quadrants[v['quadrant']].append(v)
    def group(field):
        result=[]
        for identity in sorted({v[field] for v in vaults}):
            items=[v for v in vaults if v[field]==identity];valid=[v for v in items if v['annualizedFeeRevenue'] is not None]
            capital=sum(v['timeWeightedTvlUsd'] for v in valid);annual=sum(v['annualizedFeeRevenue'] for v in valid)
            result.append(({'chainId':identity,'chain':CHAINS.get(identity,str(identity))} if field=='chainId' else {'category':identity})|
                          {'tvl':amount(items,'tvlUsd'),'fees':amount(items,'totalFeeRevenue'),'feeYield':ratio(annual,capital),'vaultCount':len(items)})
        return result
    annual=sum(v['annualizedFeeRevenue'] for v in eligible) if eligible else None
    gross_capital=sum(v['timeWeightedTvlUsd'] for v in eligible) if eligible else None
    return {'vaults':vaults,'protocolFeeYield':ratio(annual,gross_capital),'feeCaptureRate':ratio(amount(vaults,'totalFeeRevenue'),amount(vaults,'totalGainUsd')) if all(v['availability']['fees']['state']==v['availability']['gains']['state']=='available' for v in vaults) else None,
            'medianVaultFeeYield':yield_median,'totalAnnualizedFees':annual,'totalTvl':amount(vaults,'tvlUsd'),
            'vaultCount':len(vaults),'lastUpdated':__import__('datetime').datetime.fromtimestamp(cutoff,__import__('datetime').timezone.utc).isoformat(),'byChain':group('chainId'),'byCategory':group('category'),
            'quadrants':quadrants,'quadrantThresholds':{'tvlUsd':tvl_median,'feeYield':yield_median,'cohortSize':len(displayed),'capitalBasis':'current gross TVL','minTvlUsd':1e4,'maxTvlUsd':1e8,'highYieldRule':'strictly greater than median'},'period':{'startTimestamp':start,'endTimestamp':cutoff,'trendSplitTimestamp':middle},
            'capitalBasis':'gross contract capital; nested contract yield is not consolidated economic yield',
            'dataQuality':{'highConfidenceCount':sum(v['pricingConfidence']=='high' for v in vaults),
                'mediumConfidenceCount':sum(v['pricingConfidence']=='medium' for v in vaults),
                'lowConfidenceCount':sum(v['pricingConfidence']=='low' for v in vaults),
                'reportsWithPricingSource':sum(r.get('price_usd') is not None for data in gains.values() for r in data),
                'totalReports':sum(len(data) for data in gains.values())}}


def dated_rows(tvl,dates):
    if tvl.history_prepared():
        from .tvl_history_cache import rows
        yield from rows(tvl,dates)
    else:
        for offset in range(0,len(dates),31):
            yield from tvl.history_rows(dates[offset:offset+31],None)


def prepare(database, financial, tvl):
    with closing(connect(database)) as conn:
        conn.executescript(SCHEMA)
        inputs={kind:selected_input(conn,kind) for kind in ('defillama','membership','fee-config')}
    cutoff=min(financial.cutoff,(tvl.as_of()//DAY+1)*DAY)
    context={'methodologyVersion':VERSION,'tvlDatasetId':tvl.id,'feesDatasetId':financial.id,
             'inputIds':{k:v['id'] for k,v in inputs.items()},'cutoffTimestamp':cutoff,'tvlAsOfTimestamp':tvl.as_of()}
    identity=hashlib.sha256(encoded(context).encode()).hexdigest()
    with closing(connect(database,readonly=True)) as conn:
        if conn.execute('SELECT 1 FROM analytics_publications WHERE id=?',(identity,)).fetchone():
            existing=True
        else:existing=False
    if not existing:
        _,rows,positions,_=tvl.frames((tvl.as_of(),))[0]
        dates=tuple(t for t in tvl.dates() if cutoff-YEAR<=t<cutoff)
        history=defaultdict(list)
        for timestamp,points in dated_rows(tvl,dates):
            for row in points:history[key(row)].append((timestamp,row['tvl_usd']))
        configs=inputs['fee-config']['body']
        profits={str(c):profitability(financial,rows,history,configs,cutoff,c) for c in [None,*sorted({r['chain_id'] for r in rows})]}
        references=inputs['defillama']['body'].get('snapshots',[])
        publication={'meta':context|{'publicationId':identity,'publishedAt':int(time.time()),
                        'inputs':{k:{'id':v['id'],'observedAt':v['observed_at'],'availability':v['status']} for k,v in inputs.items()}},
            'stack':fee_stack(rows,positions,configs),'comparable':comparable(rows,inputs['membership']['body']),
            'profitability':profits,'comparison':owned_comparison(tvl,rows,references)}
        with closing(connect(database)) as conn,conn:
            conn.execute('INSERT INTO analytics_publications VALUES (?,?,?) ON CONFLICT DO NOTHING',(identity,int(time.time()),encoded(publication)))
    with closing(connect(database)) as conn,conn:
        conn.execute("INSERT INTO analytics_selection VALUES ('powerglove',?) ON CONFLICT(name) DO UPDATE SET publication_id=excluded.publication_id",(identity,))
    return identity


def owned_comparison(tvl, rows, snapshots):
    # Keep the aggregate consumer shape; missing external totals remain unavailable.
    refs={r['protocol']:r for r in snapshots}
    totals=[refs.get(p,{}).get('total') for p in ('yearn-finance','yearn-curating')]
    available=all(v is not None for v in totals)
    total=sum(totals) if available else None
    ours=money(rows,'external_tvl_usd')
    difference=ours-total if ours is not None and total is not None else None
    by_chain=[]
    for chain in sorted(set(CHAINS.values())|{name for r in snapshots for name in r.get('byChain',{})}):
        local=money([r for r in rows if CHAINS.get(r['chain_id'])==chain],'external_tvl_usd')
        external=sum(r.get('byChain',{}).get(chain,0) for r in snapshots) if available else None
        by_chain.append({'chain':chain,'ours':local,'defillama':external,'difference':local-external if local is not None and external is not None else None})
    categories=[]
    for label,protocol,curation in [('V1 + V2 + V3','yearn-finance',False),('Curation','yearn-curating',True)]:
        local=money([r for r in rows if (r.get('category')=='curation')==curation],'external_tvl_usd')
        external=refs.get(protocol,{}).get('total')
        categories.append({'category':label,'defillamaProtocol':protocol,'ours':local,'defillama':external,'difference':local-external if local is not None and external is not None else None})
    summary=tvl.summary()
    return {'ourTotal':ours,'defillamaTotal':total,'difference':difference,'differencePercent':ratio(difference,total)*100 if ratio(difference,total) is not None else None,
        'retiredTvl':summary['retiredVaultTvl'],'overlapDeducted':summary['overlapExcluded'],'crossChainOverlap':summary['vaultBridgeExcluded'],
        'grossTvl':money(rows,'tvl_usd'),'gapComponents':[],'retiredTvlByChain':{},'notes':[], 'byChain':by_chain,'byCategory':categories,
        'datasetId':tvl.id,'availability':{'state':'available' if available else 'unavailable','references':snapshots}}


class AnalyticsStore:
    def __init__(self,database):self.database=database
    @lru_cache(maxsize=4)
    def load(self,identity):
        with closing(connect(self.database,readonly=True)) as conn:
            row=conn.execute('SELECT body_json FROM analytics_publications WHERE id=?',(identity,)).fetchone()
        if row is None:raise ValueError('unknown analytics publicationId')
        return json.loads(row['body_json'])
    def get(self,identity=None):
        if identity is None:
            with closing(connect(self.database,readonly=True)) as conn:
                if not table_exists(conn,'analytics_selection'):raise ValueError('analytics have not been published')
                row=conn.execute("SELECT publication_id FROM analytics_selection WHERE name='powerglove'").fetchone()
            if row is None:raise ValueError('analytics have not been published')
            identity=row[0]
        if not re.fullmatch('[a-f0-9]{64}',identity):raise ValueError('invalid publicationId')
        return self.load(identity)


def analytics_response(store,url):
    request=urlsplit(url)
    paths={'/api/analytics/publication':'selection','/api/fees/stack':'stack','/api/profitability':'profitability','/api/comparison/defillama-comparable':'comparable','/api/comparison':'comparison'}
    if request.path not in paths:return 404,{'error':'Not found'}
    try:
        query=parse_qs(request.query,keep_blank_values=True)
        allowed={'publicationId'}|({'chainId'} if request.path=='/api/profitability' else {'includeVaultBreakdown'} if paths[request.path]=='comparable' else {'datasetId'} if paths[request.path]=='comparison' else set())
        if set(query)-allowed or any(len(v)!=1 for v in query.values()):raise ValueError('unsupported or repeated analytics filter')
        publication=store.get(query.get('publicationId',[None])[0])
        if paths[request.path]=='selection':return 200,publication['meta']
        payload=publication[paths[request.path]]
        if paths[request.path]=='profitability':
            chain=query.get('chainId',['None'])[0]
            if chain!='None' and not re.fullmatch('[1-9][0-9]*',chain):raise ValueError('chainId must be a positive integer')
            if chain not in payload:raise ValueError('chainId is not in the selected publication')
            payload=payload[chain]
        if 'includeVaultBreakdown' in query and query['includeVaultBreakdown'][0] not in ('true','false'):raise ValueError('includeVaultBreakdown must be true or false')
        if 'datasetId' in query and query['datasetId'][0]!=publication['meta']['tvlDatasetId']:raise ValueError('datasetId does not match selected analytics publication')
        return 200,payload|{'meta':publication['meta'],'publicationId':publication['meta']['publicationId']}
    except ValueError as error:return 400,{'error':str(error)}
