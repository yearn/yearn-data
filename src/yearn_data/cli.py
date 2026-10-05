"""Command-line interface."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from .analysis import run_lifetime_yield, run_vault_fees, run_vault_volume
from .config import CHAINS, DEFAULT_CHAINS, get_event_source, load_environment, normalize_chain_key
from .discovery import discover
from .envio import discover_from_envio, import_reports_from_envio
from .exports import export_analysis
from .headline import LIFETIME_YIELD_HEADLINE_KEY, publish_lifetime_yield_headline
from .indexing import index_all_reports, index_all_volume, index_v2_fee_mints_from_reports
from .pricing import price_unpriced_reports, price_unpriced_volume
from .storage import DEFAULT_DB_PATH, connect, init_db, seed_chains


def progress(message: str) -> None:
    print(message, flush=True)


def _chains(values: list[str] | None) -> list[str]:
    if not values:
        return list(DEFAULT_CHAINS)
    return [normalize_chain_key(value) for value in values]


def _run_analysis(conn, job: str) -> int:
    if job == "lifetime-yield":
        return run_lifetime_yield(conn)
    if job == "vault-volume":
        return run_vault_volume(conn)
    if job == "vault-fees":
        return run_vault_fees(conn)
    raise ValueError(f"unsupported analysis job {job!r}")


def open_db(path: str | Path):
    conn = connect(path)
    init_db(conn)
    seed_chains(conn, CHAINS)
    return conn


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="yearn-data")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="SQLite database path")
    parser.add_argument("--env", action="append", default=[], help="Extra .env file to load before defaults")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db")

    discover_p = sub.add_parser("discover")
    discover_p.add_argument("--chains", nargs="+", help="Chains to discover")
    discover_p.add_argument(
        "--find-deployment",
        action="store_true",
        help="Find deployment blocks now. Slower, but reduces later scan ranges.",
    )
    discover_p.add_argument(
        "--include-non-yearn",
        action="store_true",
        help="Also discover public V3 VaultFactory vaults not managed by Yearn role managers.",
    )
    discover_p.add_argument(
        "--skip-v2",
        action="store_true",
        help="Skip V2 registry discovery when refreshing V3-only vault sets.",
    )
    discover_p.add_argument("--include-retired", action="store_true", help="Retain historical V3 role-manager members without marking them active")
    discover_p.add_argument("--to-block", type=int, help="Stop Envio inventory discovery at this block")
    discover_p.add_argument(
        "--include-experimental-v2",
        action="store_true",
        help="Include V2 experimental registry deployments (excluded by default for RPC parity).",
    )

    index_p = sub.add_parser("index-events")
    index_p.add_argument("--chains", nargs="+", help="Chains to index")
    index_p.add_argument("--versions", nargs="+", choices=["v2", "v3"], help="Vault versions to index")
    index_p.add_argument("--to-block", type=int, help="Stop block for every selected chain")
    index_p.add_argument("--include-inactive", action="store_true", help="Include historical Yearn vaults in Envio report import")
    index_p.add_argument("--from-block", type=int, help="Bounded Envio historical import; requires --to-block and preserves forward cursors")
    index_p.add_argument("--vaults", nargs="+", help="Limit bounded Envio historical import to these addresses")
    index_p.add_argument("--chunk-size", type=int, default=50_000)

    volume_index_p = sub.add_parser("index-volume")
    volume_index_p.add_argument("--chains", nargs="+", help="Chains to index")
    volume_index_p.add_argument("--versions", nargs="+", choices=["v2", "v3"], help="Vault versions to index")
    volume_index_p.add_argument("--to-block", type=int, help="Stop block for every selected chain")
    volume_index_p.add_argument("--chunk-size", type=int, default=50_000)

    fee_index_p = sub.add_parser("index-fees")
    fee_index_p.add_argument("--chains", nargs="+", help="Chains to index")

    price_p = sub.add_parser("price")
    price_p.add_argument("--limit", type=int, help="Maximum distinct token/timestamp prices to fetch")
    price_p.add_argument("--source", choices=["defillama"], default="defillama")
    price_p.add_argument("--retry-missing", action="store_true", help="Retry existing non-ok price rows")
    price_p.add_argument("--chains", nargs="+", help="Restrict report pricing to chains")
    price_p.add_argument("--no-onchain-fallbacks", action="store_true", help="Use only the selected offchain price source")

    volume_price_p = sub.add_parser("price-volume")
    volume_price_p.add_argument("--limit", type=int, help="Maximum distinct token/timestamp prices to fetch")
    volume_price_p.add_argument("--source", choices=["defillama"], default="defillama")
    volume_price_p.add_argument("--retry-missing", action="store_true", help="Retry existing non-ok price rows")
    volume_price_p.add_argument("--chains", nargs="+", help="Restrict volume pricing to chains")
    volume_price_p.add_argument("--no-onchain-fallbacks", action="store_true", help="Use only the selected offchain price source")

    analyze_p = sub.add_parser("analyze")
    analyze_p.add_argument("job", choices=["lifetime-yield", "vault-volume", "vault-fees"])

    export_p = sub.add_parser("export")
    export_p.add_argument("job", choices=["lifetime-yield", "vault-volume", "vault-fees"])
    export_p.add_argument("--out", default="exports")

    publish_p = sub.add_parser("publish")
    publish_sub = publish_p.add_subparsers(dest="publish_job", required=True)
    headline_p = publish_sub.add_parser("lifetime-yield")
    headline_p.add_argument("--redis-url", help="Redis URL. Defaults to REDIS_URL from the environment")
    headline_p.add_argument("--key", default=LIFETIME_YIELD_HEADLINE_KEY)
    headline_p.add_argument("--ttl", type=int, help="Optional Redis EX TTL in seconds")
    headline_p.add_argument("--analysis-run-id", type=int, help="Specific completed lifetime-yield analysis run to publish")
    headline_p.add_argument("--run-id", help="Override payload run_id")
    headline_p.add_argument("--dry-run", action="store_true", help="Print the JSON payload without writing Redis")

    run_p = sub.add_parser("run")
    run_p.add_argument("job", choices=["lifetime-yield", "vault-volume"])
    run_p.add_argument("--chains", nargs="+", help="Chains to discover/index")
    run_p.add_argument("--to-block", type=int)
    run_p.add_argument("--chunk-size", type=int, default=50_000)
    run_p.add_argument("--price-limit", type=int)
    run_p.add_argument("--price-source", choices=["defillama"], default="defillama")
    run_p.add_argument("--no-onchain-fallbacks", action="store_true", help="Use only the selected offchain price source")
    run_p.add_argument("--find-deployment", action="store_true")
    run_p.add_argument("--out", default="exports")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    load_environment(args.env)
    conn = open_db(args.db)
    event_source = get_event_source()

    if args.command == "init-db":
        print(f"initialized {args.db}")
        return 0

    if args.command == "discover":
        if event_source == "envio":
            if args.include_non_yearn:
                raise ValueError("--include-non-yearn is only available with YEARN_DATA_EVENT_SOURCE=rpc")
            count = discover_from_envio(
                conn,
                _chains(args.chains),
                skip_v2=args.skip_v2,
                include_experimental_v2=args.include_experimental_v2,
                include_retired=args.include_retired,
                to_block=args.to_block,
                progress=progress,
            )
        else:
            if args.include_retired:
                raise ValueError("--include-retired requires Envio discovery")
            if args.include_experimental_v2:
                raise ValueError("--include-experimental-v2 is only available with YEARN_DATA_EVENT_SOURCE=envio")
            count = discover(
                conn,
                _chains(args.chains),
                find_deployment=args.find_deployment,
                include_non_yearn=args.include_non_yearn,
                skip_v2=args.skip_v2,
            )
        print(f"discovered/upserted {count} vault rows")
        return 0

    if args.command == "index-events":
        if event_source == "envio":
            count = import_reports_from_envio(
                conn,
                _chains(args.chains),
                versions=args.versions,
                to_block=args.to_block,
                progress=progress,
                include_inactive=args.include_inactive,
                from_block=args.from_block,
                vault_addresses=args.vaults,
            )
        else:
            if args.include_inactive or args.from_block is not None or args.vaults:
                raise ValueError("historical selection flags require Envio import")
            count = index_all_reports(
                conn,
                _chains(args.chains),
                versions=args.versions,
                to_block=args.to_block,
                chunk_size=args.chunk_size,
                progress=progress,
            )
        print(f"indexed {count} strategy report logs")
        return 0

    if args.command == "index-volume":
        count = index_all_volume(
            conn,
            _chains(args.chains),
            versions=args.versions,
            to_block=args.to_block,
            chunk_size=args.chunk_size,
            progress=progress,
        )
        print(f"indexed {count} volume logs/rows")
        return 0

    if args.command == "index-fees":
        count = index_v2_fee_mints_from_reports(conn, _chains(args.chains), progress=progress)
        print(f"indexed {count} fee events")
        return 0

    if args.command == "price":
        chain_ids = {CHAINS[chain].chain_id for chain in _chains(args.chains)} if args.chains else None
        count = price_unpriced_reports(
            conn,
            limit=args.limit,
            source=args.source,
            fallback=None,
            retry_missing=args.retry_missing,
            chain_ids=chain_ids,
            onchain_fallbacks=not args.no_onchain_fallbacks,
        )
        print(f"priced/recorded {count} token timestamp rows")
        return 0

    if args.command == "price-volume":
        chain_ids = {CHAINS[chain].chain_id for chain in _chains(args.chains)} if args.chains else None
        count = price_unpriced_volume(
            conn,
            limit=args.limit,
            source=args.source,
            fallback=None,
            retry_missing=args.retry_missing,
            chain_ids=chain_ids,
            onchain_fallbacks=not args.no_onchain_fallbacks,
        )
        print(f"priced/recorded {count} volume token timestamp rows")
        return 0

    if args.command == "analyze":
        run_id = _run_analysis(conn, args.job)
        print(f"analysis run {run_id} complete")
        return 0

    if args.command == "export":
        paths = export_analysis(conn, args.job, args.out)
        for path in paths:
            print(path)
        return 0

    if args.command == "publish":
        if args.publish_job == "lifetime-yield":
            _, payload_json = publish_lifetime_yield_headline(
                conn,
                redis_url=args.redis_url or os.environ.get("REDIS_URL"),
                key=args.key,
                ttl=args.ttl,
                analysis_run_id=args.analysis_run_id,
                run_id=args.run_id,
                dry_run=args.dry_run,
            )
            print(payload_json)
            return 0
        raise AssertionError(args.publish_job)

    if args.command == "run":
        chains = _chains(args.chains)
        if args.job == "lifetime-yield" and event_source == "envio":
            count = discover_from_envio(
                conn,
                chains,
                to_block=args.to_block,
                progress=progress,
            )
        else:
            count = discover(conn, chains, find_deployment=args.find_deployment)
        print(f"discovered/upserted {count} vault rows")
        if args.job == "lifetime-yield":
            if event_source == "envio":
                count = import_reports_from_envio(
                    conn,
                    chains,
                    to_block=args.to_block,
                    progress=progress,
                )
            else:
                count = index_all_reports(
                    conn,
                    chains,
                    to_block=args.to_block,
                    chunk_size=args.chunk_size,
                    progress=progress,
                )
            print(f"indexed {count} strategy report logs")
        else:
            count = index_all_volume(conn, chains, to_block=args.to_block, chunk_size=args.chunk_size, progress=progress)
            print(f"indexed {count} volume logs/rows")
        if args.job == "lifetime-yield":
            count = price_unpriced_reports(
                conn,
                limit=args.price_limit,
                source=args.price_source,
                fallback=None,
                onchain_fallbacks=not args.no_onchain_fallbacks,
            )
        else:
            count = price_unpriced_volume(
                conn,
                limit=args.price_limit,
                source=args.price_source,
                fallback=None,
                onchain_fallbacks=not args.no_onchain_fallbacks,
            )
        print(f"priced/recorded {count} token timestamp rows")
        run_id = _run_analysis(conn, args.job)
        print(f"analysis run {run_id} complete")
        for path in export_analysis(conn, args.job, args.out):
            print(path)
        return 0

    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
