"""Build a local, single-close review dataset from a pinned publication.

Reads the source database and archive RPCs only. No source observations,
publications or services are modified. The review SQLite file is newly created.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from yearn_data.config import load_environment
from yearn_data.storage import connect, init_db
from yearn_data.tvl_api import TvlDataset
from yearn_data.tvl_sources import ArchiveReader, REGISTRY, _position


def review(publication, out):
    out=Path(out)
    path=out/'review.sqlite'
    if path.exists():raise ValueError('review database already exists; use a new output directory')
    root=Path(publication)
    identity=json.loads((root/'current.json').read_text())['datasetId']
    manifest=json.loads((root/'datasets'/f'{identity}.json').read_text())
    dataset=TvlDataset(manifest)
    timestamp,rows,positions,before=dataset.frames((dataset.as_of(),))[0]
    points={(r['chain_id'],r['vault']):r for r in rows}
    edges={(p['chain_id'],p['parent'],p['strategy'],p['child']):p for p in positions}
    candidates={}
    for p in positions:
        if p['method']=='vault-asset':
            candidates[(p['chain_id'],p['parent'],p['strategy'],p['child'])]=p
    for entry in REGISTRY['targets']:
        if not entry.get('vaultGetter'):
            continue
        chain,parent,strategy,child=(entry['chainId'],entry['sourceVaultAddress'].lower(),
                                    entry['strategyAddress'].lower(),entry['targetVaultAddress'].lower())
        if (chain,parent) in points and (chain,child) in points:
            candidates[(chain,parent,strategy,child)]={'chain_id':chain,'parent':parent,'strategy':strategy,
                'child':child,'method':'service-registry','vault_getter':entry['vaultGetter']}
    for entry in REGISTRY.get('crossChainStrategies',[]):
        chain,parent,strategy,child=(entry['chainId'],entry['sourceVaultAddress'].lower(),
                                    entry['strategyAddress'].lower(),entry['targetVaultAddress'].lower())
        if (chain,parent) in points and (entry['targetChainId'],child) in points:
            candidates[(chain,parent,strategy,child)]={'chain_id':chain,'parent':parent,'strategy':strategy,
                'child':child,'child_chain_id':entry['targetChainId'],'remote_holder':entry['remoteStrategyAddress'].lower(),
                'method':'cross-chain-shares'}
    chains={p['chain_id'] for p in candidates.values()}|{p.get('child_chain_id',p['chain_id']) for p in candidates.values()}
    readers={chain:ArchiveReader(chain) for chain in sorted(chains)}
    verified=[]
    for key,p in candidates.items():
        # Rebuild inputs rather than carrying a prior deduction/status into acquisition.
        edge={k:p[k] for k in ('chain_id','parent','strategy','child','method','vault_getter','child_chain_id','remote_holder') if k in p}
        result=_position(edge,timestamp,readers[p['chain_id']],points,readers)
        previous=edges.get(key)
        edges[key]=result
        fallback=(p['chain_id'],p['parent'],p['strategy'],p['strategy'])
        if fallback!=key and edges.get(fallback,{}).get('method')=='strategy-allocation':
            del edges[fallback]
        verified.append({'edge':result,'previousOverlapUsd':previous.get('overlap_usd') if previous else None})
    out.mkdir(parents=True,exist_ok=True)
    with closing(connect(path)) as conn:
        init_db(conn)
        with conn:
            for meta in manifest['catalog']:
                fields=list(meta)
                conn.execute(f'INSERT INTO tvl_vaults ({",".join(fields)}) VALUES ({",".join("?" for _ in fields)})',tuple(meta.values()))
            conn.execute('INSERT INTO tvl_runs VALUES (1,1,1,?,?)',('incomplete',json.dumps({'from':timestamp,'to':timestamp,'sourceDatasetId':identity})))
            for p in rows:
                raw={k:v for k,v in p.items() if k not in ('overlap_usd','external_tvl_usd','bridge_excluded_usd','bridge_migration_timestamp','source_run_id')}
                conn.execute('INSERT INTO tvl_snapshots VALUES (1,?,?,?,?,?)',(p['chain_id'],p['vault'],timestamp,p['block_number'],json.dumps(raw)))
            for p in edges.values():
                raw={k:v for k,v in p.items() if k not in ('overlap_usd','source_run_id','accounting_excluded')}
                conn.execute('INSERT INTO tvl_positions VALUES (1,?,?,?,?,?,?)',(p['chain_id'],p['parent'],p['strategy'],p['child'],timestamp,json.dumps(raw)))
    # Freeze the original catalog, allocator evidence and bridge/quote policy in
    # this review manifest, replacing only the database and source-run membership.
    from yearn_data.tvl_api import encoded
    import hashlib
    review_manifest=manifest | {'database':str(path.resolve()),'runs':[{'id':1,'params_json':json.dumps({'from':timestamp,'to':timestamp})}]}
    review_manifest.pop('diagnostics',None)
    review_manifest['datasetId']=hashlib.sha256(encoded(review_manifest).encode()).hexdigest()
    publication=out/'publication';(publication/'datasets').mkdir(parents=True,exist_ok=True)
    (publication/'datasets'/f"{review_manifest['datasetId']}.json").write_text(encoded(review_manifest)+'\n')
    (publication/'current.json').write_text(encoded({'datasetId':review_manifest['datasetId']})+'\n')
    revised=TvlDataset(review_manifest)
    _,_,after_edges,after=revised.frames((timestamp,))[0]
    after_by={(p['chain_id'],p['parent'],p['strategy'],p['child']):p for p in after_edges}
    for item in verified:
        p=item['edge'];item['revisedOverlapUsd']=after_by[(p['chain_id'],p['parent'],p['strategy'],p['child'])]['overlap_usd']
    result={'sourceDatasetId':identity,'reviewDatasetId':revised.id,'timestamp':timestamp,
            'before':before,'after':after,'verifiedPositions':verified}
    (out/'validation.json').write_text(json.dumps(result,indent=2)+'\n')
    (out/'graph.json').write_text(json.dumps(revised.graph(),indent=2)+'\n')
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--publication',required=True)
    parser.add_argument('--out',required=True)
    parser.add_argument('--env',default='.env')
    args=parser.parse_args();load_environment([Path(args.env)])
    result=review(args.publication,args.out)
    print(json.dumps({'sourceDatasetId':result['sourceDatasetId'],'reviewDatasetId':result['reviewDatasetId'],
                      'timestamp':result['timestamp'],'positions':len(result['verifiedPositions']),
                      'before':result['before']['known_external_tvl_usd'],'after':result['after']['known_external_tvl_usd']},indent=2))
