"""Reconcile a saved TVL frame with pinned upstream inventory and archive evidence.

Usage: PYTHONPATH=src python scripts/review_tvl_comparison.py ARTIFACT_DIRECTORY
See docs/tvl-comparison.md for the input contract and publication workflow.
"""
import argparse
import json,re,requests,hashlib
from pathlib import Path
from decimal import Decimal as D
from concurrent.futures import ThreadPoolExecutor
from yearn_data.analytics_collect import js_array,rules,FINANCE_CHAINS,CURATION_CHAINS
parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('artifacts',type=Path);args=parser.parse_args();p=args.artifacts;f=json.loads((p/'frame.json').read_text());ts=f['timestamp'];rows={(r['chain_id'],r['vault'].lower()):r for r in f['rows']};catalog={(r['chain_id'],r['address'].lower()):r for r in f['catalog']};positions=f['positions'];kong=json.loads((p/'kong.json').read_text());finance={(int(c),r['address'].lower()) for c,vs in kong.items() for r in vs};source=(p/'projects_yearn_index.js').read_text();finance|={(1,a) for a in js_array(source,'v1Vaults')};cross={(int(c),a.lower(),s.lower()):v.lower() for c,a,s,v in re.findall(r"chainId:\s*(\d+),\s*parent:\s*'(0x\w+)',\s*strategy:\s*'(0x\w+)',\s*child:\s*'(0x\w+)'",source)}

SUPPORTED_SOURCE_HASHES={'projects_yearn_index.js': '1aba4b2c0de82565cdf75a060290596ef2a06c1156f0225e3a45e58ed28b76f9', 'projects_helper_curators_index.js': 'afa2e23eb42b674f35ef602d81106cdd3a5577945ee3497b93ae61a6a7df9131', 'registries_curators.js': '7f23eeb5c4c7e58c158efb12a5ed1afb7a2df305f9d2c0a4e35794b7b3dd291c', 'projects_helper_curators_configs.js': '4cd5d467119a176f1100b30f951428309fc16333133dc676e1b92649c122d4d8', 'morpho-bad-debt.cjs': 'da44a880194a8f576c0914acddb4428feed673fb9d72abee558fc6756d9979df'}
CURRENT_SOURCE_HASHES = dict(SUPPORTED_SOURCE_HASHES)
CURRENT_SOURCE_HASHES.update({'projects_yearn_index.js':'f40884966b5257067ad98076d95f3a283255778beb77bf58c7370c0fc6f29e96', 'registries_curators.js':'ba341896a16f189846fd315983b052c22bb19c0d65693bedd63634238bc1b67c'})
actual={name:hashlib.sha256((p/name).read_bytes()).hexdigest() for name in SUPPORTED_SOURCE_HASHES}
assert actual in (SUPPORTED_SOURCE_HASHES,CURRENT_SOURCE_HASHES), 'Adapter changed: review source before replay'
current=actual==CURRENT_SOURCE_HASHES
policy=rules(source,(p/'registries_curators.js').read_text())
finance_replay=json.loads((p/'finance-replay.json').read_text()) if current else []
assert all(v['timestamp']==ts for v in finance_replay), 'Finance replay timestamp mismatch'
net={(v['chainId'],r['address']):r['adjustedRaw'] for v in finance_replay for r in v['records']}
recognized={(v['chainId'],e['parent'],e.get('strategy'),e.get('childChainId',v['chainId']),e['child'],e['method']) for v in finance_replay for e in v['edges']}

assert all(r['timestamp']==ts for r in rows.values()), 'Mixed snapshot timestamps'
discovery=json.loads((p/'independent-discovery.json').read_text());cur={(v['chain'],v['address'].lower()) for scan in discovery for v in scan.get('vaults',[])}
# Verified creation-owner evidence supplements the explicitly incomplete HyperEVM factory scan.
for r in json.loads((p/'verified-membership.json').read_text()):
 if r['creation']['owner'].lower() in policy['owners'].get(r['chain'],set()):cur.add((r['chain'],r['address'].lower()))
scopes={'yearn-finance':finance,'yearn-curating':cur};all_external=finance|cur
assert not all_external-set(rows), 'Need archive collection for independently discovered missing rows'
slug=FINANCE_CHAINS|CURATION_CHAINS;coins={f"{slug[c]}:{r['asset'].lower()}" for (c,a),r in rows.items() if (c,a) in all_external and r.get('tvl_usd') is None and D(r.get('asset_units') or 0)>0};coins=sorted(coins)
def prices(batch):
 url=f'https://coins.llama.fi/prices/historical/{ts}/'+','.join(batch);q=requests.get(url,timeout=60);q.raise_for_status();return q.json().get('coins',{})
pricefile=p/'prices.json'
if pricefile.exists():quotes=json.loads(pricefile.read_text())
else:
 quotes={k:v for result in ThreadPoolExecutor(5).map(prices,[coins[i:i+35] for i in range(0,len(coins),35)]) for k,v in result.items()};pricefile.write_text(json.dumps(quotes))
# Replay finance's recognized debt deductions in underlying units. This is not membership exclusion.
finance_debt={}
for v in positions:
 parent=(v['chain_id'],v['parent']);child=(v.get('child_chain_id',v['chain_id']),v['child']);strategy=v['strategy'];raw=v.get('debt_raw')
 if parent not in finance or child not in finance or parent[0]!=child[0] or raw is None:continue
 registered=cross.get((parent[0],parent[1],strategy))==child[1]
 direct=strategy==child[1] and rows[parent]['asset'].lower()==rows[child]['asset'].lower()
 if (registered or direct) and v['method'] in ('direct-strategy','service-registry'):
  identity=(parent,strategy)
  finance_debt[identity]=max(finance_debt.get(identity,D(0)),D(raw))
records=[]
for protocol,scope in scopes.items():
 for key in sorted(scope):
  r=rows[key];units=D(r['asset_units'] or 0)
  if protocol=='yearn-finance':units=max(D(0),units-sum((v/D(10)**r['asset_decimals'] for (parent,strategy),v in finance_debt.items() if parent==key),D(0)))
  if protocol=='yearn-finance' and current:
   raw=net.get(key)
   units=D(raw)/D(10)**r['asset_decimals'] if raw is not None and r.get('asset_decimals') is not None else None
  q=quotes.get(f"{slug[key[0]]}:{r['asset'].lower()}")
  valid=q and q.get('price',0)>0 and q.get('confidence',0)>=.5 and abs(q.get('timestamp',0)-ts)<=3600
  price=D(str(q['price'])) if valid else None
  records.append({'protocol':protocol,'chainId':key[0],'address':key[1],'name':r['name'],'category':r['category'],'units':str(units) if units is not None else None,'replayTvlUsd':str(units*price) if price is not None and units is not None else None,'quote':q if valid else None})
# Known overlap checks: finance's address/registry recognition; curator helper's adapter resolution replay.
replays=[]
for name in ['adapter-replay.json','adapter-replay-hyper.json','adapter-replay-katana.json']:
 v=json.loads((p/name).read_text());replays+=v if isinstance(v,list) else [v]
cur_checks={(v['chain'],v['vault'].lower(),v.get('adapter','').lower(),v.get('child','').lower()) for v in replays if v.get('child')}
record_by_key={(v['protocol'],v['chainId'],v['address']):v for v in records}
for v in replays:
 if not v.get('child') or v.get('convertedAssets') is None:continue
 parent=(v['chain'],v['vault'].lower());child=(v['chain'],v['child'].lower())
 if parent not in cur or child not in cur:continue
 child_record=record_by_key[('yearn-curating',*child)]
 units=max(D(0),D(child_record['units'])-D(v['convertedAssets'])/D(10)**rows[child]['asset_decimals'])
 child_record['units']=str(units)
 if child_record.get('quote'):child_record['replayTvlUsd']=str(units*D(str(child_record['quote']['price'])))

checks=[]
for v in positions:
 amount=D(v.get('overlap_usd') or 0)
 if amount<=0:continue
 parent=(v['chain_id'],v['parent']);child=(v.get('child_chain_id',v['chain_id']),v['child']);strategy=v['strategy'];method=v['method'];status='unverified';reason='No matching adapter replay evidence';protocol='yearn-curating' if rows[parent]['category']=='curation' else 'yearn-finance'
 if protocol=='yearn-finance' and parent in finance and child in finance:
  registered=cross.get((parent[0],parent[1],strategy))==child[1]
  direct=parent[0]==child[0] and strategy==child[1] and rows[parent]['asset'].lower()==rows[child]['asset'].lower()
  if registered or direct:status='handled';reason='Detected by direct-vault or explicit registry deduction'
  elif method=='service-registry' and (parent[0],strategy) not in finance:status='missed';reason='Intermediary strategy is not a counted vault and is absent from the adapter registry'
  elif method=='vault-asset':status='missed';reason='Vault holds another vault’s shares as its asset; the adapter has no ownership deduction for this wrapper'
  elif method=='cross-chain-shares':status='missed';reason='Cross-chain ownership is outside the adapter’s same-chain nested-debt detection'
 if current and protocol=='yearn-finance' and parent in finance and child in finance:
  matched=any((c,a,cc,ch)==(parent[0],parent[1],child[0],child[1]) and (st==strategy or kind=='vault-asset') for c,a,st,cc,ch,kind in recognized)
  if matched:status='handled';reason='Verified by the current adapter route, ownership cap, wrapper or remote deduction'
  elif registered or direct or method in ('vault-asset','cross-chain-shares'):
   status='unverified';reason='Current adapter supports this route type; dated deduction was not verified'
 if protocol=='yearn-curating' and parent in cur and child in cur and (parent[0],parent[1],strategy,child[1]) in cur_checks:status='handled';reason='Curator helper resolves this adapter and subtracts its converted shares'
 checks.append({'protocol':protocol,'parentChainId':parent[0],'parentAddress':parent[1],'parentName':rows[parent]['name'],'childChainId':child[0],'childAddress':child[1],'childName':rows[child]['name'],'strategyAddress':strategy,'ourDeductionUsd':str(amount),'status':status,'reason':reason,'method':method,'blockNumber':v['block_number']})
body={'schemaVersion':1,'tvlDatasetId':f['selection']['tvlDatasetId'],'asOfTimestamp':ts,'sources':json.loads((p/'sources.json').read_text()),'vaults':records,'overlapChecks':checks,'coverage':{'finance':f'Independent Kong inventory plus explicit V1 list; {sum(len(v) for v in kong.values())} Kong candidates','curation':'Independent factory discovery plus verified creation evidence; incomplete scans listed by chain','incompleteChains':sorted({v['factory']['chain_id'] for v in discovery if v['state']!='complete'}),'valuation':'Same-date adapter-scope replay. Reverse price gaps use historical DefiLlama quotes; overlap values use our capped dated deductions, not published per-vault DefiLlama values.'}}

# The reverse-price rows must have run the same pinned bad-debt helper. Refuse to
# treat a changed or absent haircut replay as a fully checked contribution.
bad={(v['chainId'],v['address']):v for v in json.loads((p/'reverse-bad-debt.json').read_text())}
for v in records:
 if v['protocol']=='yearn-curating' and v.get('replayTvlUsd') is not None:
  b=bad.get((v['chainId'],v['address']))
  assert b and b['before']==b['after']==rows[(v['chainId'],v['address'])]['total_assets_raw'], 'Missing or changed bad-debt replay requires a new reconciliation'
body['sources']['evidenceHashes']={name:hashlib.sha256((p/name).read_bytes()).hexdigest() for name in ['frame.json','kong.json','independent-discovery.json','verified-membership.json','adapter-replay.json','adapter-replay-hyper.json','adapter-replay-katana.json','prices.json','reverse-bad-debt.json']}
if current:body['sources']['evidenceHashes']['finance-replay.json']=hashlib.sha256((p/'finance-replay.json').read_bytes()).hexdigest()
(p/'evidence.json').write_text(json.dumps(body,indent=2));print({'externalVaults':len(records),'missingOverlap':len([c for c in checks if c['status']=='missed']),'missedUsd':sum(float(c['ourDeductionUsd']) for c in checks if c['status']=='missed'),'reversePriced':[(v['name'],v['replayTvlUsd']) for v in records if v['replayTvlUsd'] and float(v['replayTvlUsd'])>1]})
