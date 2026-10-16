"""Capture current Kong scope and dated Finance deductions for a reviewed adapter."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path

from yearn_data.analytics_collect import Reader, fetch, rules, default_debts, FINANCE_CHAINS
from yearn_data.config import load_environment
from yearn_data.finance_membership import contributions, call
from yearn_data.tvl_sources import ArchiveReader


def collect(root, timestamp):
    policy=rules((root/'projects_yearn_index.js').read_text(),(root/'registries_curators.js').read_text())
    def chain_run(chain):
        candidates=fetch(f'https://kong.yearn.fi/api/rest/list/vaults/{chain}?origin=yearn').json()
        candidates=[v for v in candidates if v['chainId']==chain and v['address'].lower() not in policy['blacklist']]
        reader=Reader(chain); archive=ArchiveReader(chain)
        reader.block=reader.w3.eth.get_block(archive.block_at(timestamp))
        finance={v['address'].lower():v for v in candidates}
        balances=dict(zip(finance,reader.call([call(a,'totalAssets()') for a in finance])))
        active={a:v for a,v in finance.items() if balances[a] and balances[a][0]>0}
        debts=default_debts(reader,active)
        net,edges=contributions(reader,chain,active,balances,debts,policy)
        records=[dict(chainId=chain,address=a,rawAssets=str(balances[a][0]) if balances[a] else None,
                      adjustedRaw=str(net[a]) if net.get(a) is not None else ('0' if balances[a] and balances[a][0]==0 else None)) for a in finance]
        if chain==1:
            vs=sorted(policy['v1'])
            supply=reader.call([call(a,'totalSupply()') for a in vs])
            pps=reader.call([call(a,'getPricePerFullShare()') for a in vs])
            records += [dict(chainId=chain,address=a,adjustedRaw=str(x[0]*y[0]//10**18) if x and y else None) for a,x,y in zip(vs,supply,pps)]
        result=dict(chainId=chain,blockNumber=int(reader.block['number']),timestamp=timestamp,records=records,edges=edges)
        (root/f'finance-replay-{chain}.json').write_text(json.dumps(result,indent=2))
        (root/f'kong-{chain}.json').write_text(json.dumps(candidates))
        print(chain,len(records),'unavailable',sum(v['adjustedRaw'] is None for v in records),flush=True)
        return chain,candidates,result
    results=list(ThreadPoolExecutor(4).map(chain_run,FINANCE_CHAINS))
    (root/'kong.json').write_text(json.dumps({c:vs for c,vs,_ in results}))
    (root/'finance-replay.json').write_text(json.dumps([r for _,_,r in results]))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('artifacts',type=Path);parser.add_argument('--timestamp',type=int,required=True)
    args=parser.parse_args();load_environment();collect(args.artifacts,args.timestamp)
