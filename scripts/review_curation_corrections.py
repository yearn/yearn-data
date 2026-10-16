"""Build a separate single-close review publication from saved, dated evidence.

No production writes or price substitutions. Additional V2 evidence must be
collected at the same block as its selected parent snapshot.
"""
import argparse
from decimal import Decimal
import hashlib
import json
from pathlib import Path

from yearn_data.storage import connect, init_db
from yearn_data.tvl_api import publish_tvl, TvlDataset


def review(frame_path, positions_path, hyper_path, out):
    out=Path(out)
    path=out/'review.sqlite'
    if path.exists():
        raise ValueError('review database already exists; choose a new output directory')
    inputs=[Path(frame_path),Path(positions_path),Path(hyper_path)]
    frame, additions, hyper=[json.loads(p.read_text()) for p in inputs]
    timestamp=frame['timestamp']
    rows={(r['chain_id'],r['vault'].lower()):r for r in frame['rows']}
    original_rows=list(rows.values())
    identity=lambda p:(p['chain_id'],p['parent'],p['strategy'],p['child'])
    positions={identity(p):p for p in frame['positions']}
    for item in additions:
        p=item['position']
        parent=rows[(p['chain_id'],p['parent'])]
        if p['timestamp']!=timestamp or p['block_number']!=parent['block_number'] or p['status']!='ok':
            raise ValueError('additional position is not verified at the selected close')
        positions[identity(p)]=p
        positions.pop((p['chain_id'],p['parent'],p['strategy'],p['strategy']),None)
    if hyper['snapshot']['timestamp']!=timestamp or hyper['position']['timestamp']!=timestamp:
        raise ValueError('HyperEVM evidence must use the selected close')
    rows[(999,hyper['snapshot']['vault'])]=hyper['snapshot']
    positions[identity(hyper['position'])]=hyper['position']
    out.mkdir(parents=True,exist_ok=True)
    conn=connect(path);init_db(conn)
    with conn:
        for meta in frame['catalog']+[hyper['catalog']]:
            fields=list(meta)
            conn.execute(f'INSERT INTO tvl_vaults ({",".join(fields)}) VALUES ({",".join("?" for _ in fields)})',tuple(meta.values()))
        conn.execute("INSERT INTO tvl_runs VALUES (1,1,1,'incomplete',?)",(json.dumps({'from':timestamp,'to':timestamp,'source':'curation-correction-review'}),))
        for r in rows.values():
            raw={k:v for k,v in r.items() if k not in ('overlap_usd','external_tvl_usd','bridge_excluded_usd','bridge_migration_timestamp','source_run_id')}
            conn.execute('INSERT INTO tvl_snapshots VALUES (1,?,?,?,?,?)',(r['chain_id'],r['vault'],timestamp,r['block_number'],json.dumps(raw)))
        for p in positions.values():
            raw={k:v for k,v in p.items() if k not in ('overlap_usd','source_run_id','accounting_excluded')}
            if raw.get('reason') in ('curation_separate_product','bridge_parent_excluded','endpoint_outside_selection'):
                raw.pop('reason')
            conn.execute('INSERT INTO tvl_positions VALUES (1,?,?,?,?,?,?)',(*identity(p),timestamp,json.dumps(raw)))
    conn.close()
    manifest=publish_tvl(path,out/'publication')
    dataset=TvlDataset(manifest)
    _,corrected,edges,diagnostics=dataset.frames((timestamp,))[0]
    def totals(points):
        result={}
        for r in points:
            if r['external_tvl_usd'] is not None:
                c=r.get('category',r['version'])
                result[c]=result.get(c,Decimal(0))+Decimal(r['external_tvl_usd'])
        return {c:str(v) for c,v in result.items()}
    before,after=totals(original_rows),totals(corrected)
    changed=[]
    new={(r['chain_id'],r['vault']):r for r in corrected}
    for old in original_rows:
        r=new.get((old['chain_id'],old['vault']))
        if r is None or r['external_tvl_usd']!=old['external_tvl_usd']:
            changed.append({'chainId':old['chain_id'],'vault':old['vault'],'name':old.get('name'),
                'before':old['external_tvl_usd'],'after':r['external_tvl_usd'] if r else None,
                'excluded':r is None})
    result={'timestamp':timestamp,'reviewDatasetId':dataset.id,'before':before,'after':after,
        'changes':changed,'diagnostics':diagnostics,'additionalV2Positions':[i['position'] for i in additions],
        'inputSha256':{str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in inputs},
        'scope':'Single-close local review; missing prices remain unavailable; no active publication changed'}
    (out/'validation.json').write_text(json.dumps(result,indent=2)+'\n')
    (out/'graph.json').write_text(json.dumps(dataset.graph(),indent=2)+'\n')
    (out/'summary.json').write_text(json.dumps(dataset.summary(),indent=2)+'\n')
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--frame',required=True)
    parser.add_argument('--positions',required=True)
    parser.add_argument('--hyper',required=True)
    parser.add_argument('--out',required=True)
    args=parser.parse_args()
    result=review(args.frame,args.positions,args.hyper,args.out)
    print(json.dumps({k:result[k] for k in ('reviewDatasetId','before','after')},indent=2))
