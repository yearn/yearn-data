# One database for TVL, fees and earnings

Use `data/yearn.sqlite` for all three pipelines. TVL owns `tvl_*` tables;
fee/earnings tables keep their existing names, so selected analyses and consumers
continue to work. The pipelines share the database file, not mutable catalogs or
price caches.

| Records | Tables |
| --- | --- |
| Fee/earnings inventory and source evidence | `vaults`, `events_raw`, `strategy_reports`, `history_*` |
| Fee calculations and selected earnings/fee analyses | `canonical_fee_reports`, `fee_*`, `analysis_runs`, `analysis_outputs` |
| Fee/earnings cached prices | `prices`, `fee_daily_prices` |
| TVL inventory and source evidence | `tvl_vaults`, `tvl_catalog_evidence`, `tvl_events_raw`, `tvl_inventory_events`, `tvl_history_*` |
| Dated TVL and nested positions | `tvl_runs`, `tvl_snapshots`, `tvl_positions`, `tvl_strategies`, `tvl_targets` |
| TVL cached prices and backfill checkpoints | `tvl_prices`, `tvl_backfill_*` |

TVL can read fee/earnings strategy evidence to expand its candidate mappings. It
writes its own strategy mappings and coverage; TVL discovery does not change the
fee/earnings catalog, pricing decisions or analysis outputs. Run IDs stay local to
their respective run tables.

## Combine existing databases

Check available disk space before writing:

```bash
yearn-data merge-databases \
  --fees-db /path/to/fees-and-earnings.sqlite \
  --tvl-db /path/to/tvl-backfill.sqlite \
  --out /path/to/yearn-shared.sqlite \
  --check-only
```

Repeat without `--check-only` to create the shared file. The merge accepts the
previous standalone TVL schema and the new namespaced schema. It preserves all
stored history, including superseded repair runs, missing values, checkpoints
and reserved run IDs. It does not recalculate or fetch historical values.

The command creates a new database and keeps both inputs unchanged. It refuses an
existing output or fee input that already contains TVL records, because unrelated
run IDs cannot safely be combined. Disk planning includes the additional database,
working overhead and a default **4 GiB reserve**. The reserve is checked throughout
the copy. Inserts use small committed batches to limit journal growth.

The file is published only after table counts, SQLite integrity, foreign-key
references and unchanged inputs are checked. Interrupted merges remove their
unfinished staging file. A process killed before cleanup can leave a `.partial`
file; inspect that file before removing it or choosing another output.

After validating existing consumers, point all jobs at the shared file. Keep the
originals as rollback copies. A canonical `data/yearn.sqlite` symlink lets existing
commands keep the same path; stop database users before switching that path.

## Continue using the shared file

```bash
yearn-data --db data/yearn.sqlite tvl discover
yearn-data --db data/yearn.sqlite tvl collect \
  --from-timestamp 1791071999 --to-timestamp 1791071999
yearn-data --db data/yearn.sqlite tvl export --out exports/tvl
```

Use the same `--db` path for fee/earnings commands and for the monthly TVL backfill.
Legacy standalone TVL files should be merged before using the new table layout;
discovering into a legacy file does not migrate its previous catalog or history.
