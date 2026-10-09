#!/usr/bin/env python3
"""Run one explicitly bounded Postgres update and publish its completed outputs."""
import argparse
import json
from pathlib import Path
import subprocess
import sys


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db',choices=['neon','staging'],required=True)
    parser.add_argument('--env',type=Path,required=True)
    parser.add_argument('--chain',required=True)
    parser.add_argument('--from-block',type=int,required=True)
    parser.add_argument('--to-block',type=int,required=True)
    parser.add_argument('--cutoff',type=int,required=True)
    parser.add_argument('--publication-root',type=Path,required=True)
    parser.add_argument('--deferred-manifest',type=Path)
    args=parser.parse_args()
    if args.cutoff%86400 or args.from_block>args.to_block:
        parser.error('cutoff must be UTC midnight and block range must be ordered')
    from yearn_data.analysis import run_lifetime_yield
    from yearn_data.fee_valuation import run_fee_usd,closed_cutoff
    from yearn_data.config import load_environment
    from yearn_data.storage import connect
    from yearn_data.pairing import select_pairing,PairingDataset
    from yearn_data.tvl_api import publish_tvl,TvlDataset
    from yearn_data.tvl_history_cache import prepare
    from yearn_data.hosted_publications import publish
    from contextlib import closing
    load_environment([args.env])
    closed_cutoff(args.cutoff)
    base=[sys.executable,'-m','yearn_data.cli','--env',str(args.env),'--db',args.db]
    def run(*command):
        subprocess.run(base+list(command),check=True)
    run('catch-up','--chain',args.chain,'--from-block',str(args.from_block),'--to-block',str(args.to_block))
    run('index-fees','--canonical','--chains',args.chain,'--limit','500')
    run('price','--source','yearn-prices','--no-provider-fallback','--no-onchain-fallbacks','--limit','500')
    run('price-fees','--before-timestamp',str(args.cutoff),'--limit','500')
    with closing(connect(args.db)) as conn, conn.raw.pipeline():
        earnings=run_lifetime_yield(conn,price_source='yearn-prices',fallback_price_source=None,before_timestamp=args.cutoff)
        fees=run_fee_usd(conn,before_timestamp=args.cutoff,deferred_manifest=args.deferred_manifest)
    ids={'lifetime-yield':earnings,'fee-usd':fees}
    # Validate the completed pair before moving the serving publication pointer.
    PairingDataset(args.db,ids['lifetime-yield'],ids['fee-usd'])
    run('tvl','collect','--from-timestamp',str(args.cutoff-1),'--to-timestamp',str(args.cutoff-1))
    tvl=publish_tvl(args.db,args.publication_root/'.preparation',current_bridge_policy='retired-registry')
    prepare(TvlDataset(tvl),progress=lambda message:print(message,flush=True))
    publish_tvl(args.db,args.publication_root/'tvl',current_bridge_policy='retired-registry')
    pair=select_pairing(args.db,ids['lifetime-yield'],ids['fee-usd'],args.publication_root/'fees')
    release=publish(args.db,args.publication_root/'fees',args.publication_root/'tvl',prepare_analytics=True)
    print(json.dumps({'database':args.db,'cutoff':args.cutoff,'earningsRunId':ids['lifetime-yield'],'feesRunId':ids['fee-usd'],'feeDatasetId':pair['datasetId'],'tvlDatasetId':tvl['datasetId'],'releaseId':release},sort_keys=True))


if __name__=='__main__':main()
