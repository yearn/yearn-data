"""Combine immutable fee/earnings and TVL inputs in a newly staged database."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import shutil
import sqlite3
import time

from .storage import init_db

GIB = 1024**3
# Legacy TVL used generic catalogs/caches. All imports have dedicated names.
TVL_TABLES = (
    ('history_sync_runs','tvl_history_sync_runs'),
    ('vaults','tvl_vaults'), ('prices','tvl_prices'),
    ('events_raw','tvl_events_raw'), ('vault_inventory_events','tvl_inventory_events'),
    ('history_coverage','tvl_history_coverage'),
    ('tvl_discovery_runs','tvl_discovery_runs'), ('tvl_catalog_evidence','tvl_catalog_evidence'),
    ('tvl_strategies','tvl_strategies'), ('tvl_targets','tvl_targets'),
    ('tvl_runs','tvl_runs'), ('tvl_snapshots','tvl_snapshots'), ('tvl_positions','tvl_positions'),
    ('tvl_backfill_batches','tvl_backfill_batches'),
    ('tvl_backfill_births','tvl_backfill_births'), ('tvl_backfill_blocks','tvl_backfill_blocks'),
)


def read_database(path):
    conn = sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA query_only=ON')
    return conn


def _tables(conn):
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _size(conn):
    return conn.execute('PRAGMA page_count').fetchone()[0]*conn.execute('PRAGMA page_size').fetchone()[0]


def _parent(path):
    parent = path.parent
    while not parent.exists():
        parent = parent.parent
    return parent


def merge_plan(fees_db,tvl_db,out,*,reserve_gib=4):
    fees_db,tvl_db,out = (Path(p).resolve() for p in (fees_db,tvl_db,out))
    if len({fees_db,tvl_db,out}) != 3:
        raise ValueError('inputs and output must be different database files')
    if out.exists() or out.with_name(out.name+'.partial').exists():
        raise ValueError('output or staging file already exists; choose a new output')
    if not math.isfinite(reserve_gib) or reserve_gib<0:
        raise ValueError('invalid disk reserve')
    fees,tvl = read_database(fees_db),read_database(tvl_db)
    try:
        source_tables,fee_tables = _tables(tvl),_tables(fees)
        if not {'analysis_runs','analysis_outputs','vaults','prices'} <= fee_tables:
            raise ValueError('fee/earnings input is missing its required tables')
        if not {'tvl_runs','tvl_snapshots','tvl_positions'} <= source_tables:
            raise ValueError('TVL input is missing collected history')
        namespaced = ('tvl_vaults' in source_tables and
                      (tvl.execute('SELECT EXISTS(SELECT 1 FROM tvl_vaults)').fetchone()[0]
                       or 'vaults' not in source_tables
                       or not tvl.execute('SELECT EXISTS(SELECT 1 FROM vaults)').fetchone()[0]))
        copies = []
        for legacy,target in TVL_TABLES:
            source = target if namespaced else legacy
            if source not in source_tables:
                continue
            if target in fee_tables and fees.execute(f'SELECT EXISTS(SELECT 1 FROM "{target}")').fetchone()[0]:
                raise ValueError(f'fee/earnings input already contains {target}; run IDs cannot be combined blindly')
            copies.append({'source':source,'target':target,
                           'rows':tvl.execute(f'SELECT count(*) FROM "{source}"').fetchone()[0]})
        if not any(row['target']=='tvl_vaults' for row in copies):
            raise ValueError('TVL input is missing its catalog')
        # Both originals remain. Allow overhead and bounded journal/cache work.
        estimate = math.ceil((_size(fees)+_size(tvl))*1.1)+256*1024**2
        reserve = math.ceil(reserve_gib*GIB)
        free = shutil.disk_usage(_parent(out)).free
        return {'fees_db':str(fees_db),'tvl_db':str(tvl_db),'out':str(out),
                'estimated_additional_bytes':estimate,'reserve_bytes':reserve,'free_bytes':free,
                'space_sufficient':free>=estimate+reserve,'tables':copies}
    finally:
        fees.close();tvl.close()


def merge_databases(fees_db,tvl_db,out,*,reserve_gib=4,chunk_rows=20_000,progress=None):
    if chunk_rows<1:
        raise ValueError('chunk_rows must be positive')
    plan = merge_plan(fees_db,tvl_db,out,reserve_gib=reserve_gib)
    if not plan['space_sufficient']:
        raise ValueError('insufficient disk space for a staged copy and the requested reserve')
    out = Path(plan['out']);out.parent.mkdir(parents=True,exist_ok=True)
    staging = out.with_name(out.name+'.partial')
    # Exclusive creation ensures cleanup can only remove a file owned by this run.
    with staging.open('xb'):
        pass
    fees = tvl = dest = None
    started = int(time.time())
    def space_check():
        if shutil.disk_usage(out.parent).free < plan['reserve_bytes']:
            raise ValueError('disk reserve reached; inputs remain unchanged')
    try:
        fees = read_database(plan['fees_db'])
        tvl = read_database(plan['tvl_db'])
        dest = sqlite3.connect(staging,uri=True);dest.row_factory = sqlite3.Row
        versions = [c.execute('PRAGMA data_version').fetchone()[0] for c in (fees,tvl)]
        if progress:progress('Copying the fee/earnings database')
        fees.backup(dest,pages=1024,progress=lambda *_:space_check())
        init_db(dest)
        dest.commit()
        # Small committed batches bound rollback-journal growth. No large WAL or
        # temporary sorting database is required while both originals are retained.
        dest.execute('PRAGMA journal_mode=DELETE')
        dest.execute('PRAGMA foreign_keys=ON')
        dest.execute('PRAGMA temp_store=MEMORY')
        dest.execute('PRAGMA cache_size=-65536')
        uri = Path(plan['tvl_db']).as_uri()+'?mode=ro'
        dest.execute('ATTACH DATABASE ? AS tvl_source',(uri,))
        for table in plan['tables']:
            source,target = table['source'],table['target']
            columns = [r[1] for r in dest.execute(f'PRAGMA tvl_source.table_info("{source}")')]
            target_columns = [r[1] for r in dest.execute(f'PRAGMA main.table_info("{target}")')]
            if not target_columns:
                # Backfill checkpoints are optional until a backfill is run.
                from .tvl import BACKFILL_SCHEMA
                dest.executescript(BACKFILL_SCHEMA)
                target_columns = [r[1] for r in dest.execute(f'PRAGMA main.table_info("{target}")')]
            if not set(columns)<=set(target_columns):
                raise ValueError(f'unsupported columns in {source}')
            fields = ','.join('"'+name.replace('"','""')+'"' for name in columns)
            cursor,copied = None,0
            while True:
                lower = '' if cursor is None else 'WHERE rowid>?'
                bounds = () if cursor is None else (cursor,)
                ids = dest.execute(f'SELECT rowid FROM tvl_source."{source}" {lower} ORDER BY rowid LIMIT ?',
                                   (*bounds,chunk_rows)).fetchall()
                if not ids:break
                space_check();last = ids[-1][0]
                with dest:
                    interval = 'rowid<=?' if cursor is None else 'rowid>? AND rowid<=?'
                    dest.execute(f'INSERT INTO main."{target}" ({fields}) SELECT {fields} FROM tvl_source."{source}" WHERE {interval}',
                                 (*bounds,last))
                copied += len(ids);cursor = last
                if progress:progress(f'{target}: {copied}/{table["rows"]} rows copied')
            actual = dest.execute(f'SELECT count(*) FROM main."{target}"').fetchone()[0]
            if actual != table['rows'] or copied != table['rows']:
                raise ValueError(f'row count changed while copying {source}')
            # Preserve deleted high IDs as well as existing run IDs.
            seq = tvl.execute('SELECT seq FROM sqlite_sequence WHERE name=?',(source,)).fetchone() if 'sqlite_sequence' in _tables(tvl) else None
            if seq:
                with dest:
                    changed = dest.execute('UPDATE sqlite_sequence SET seq=max(seq,?) WHERE name=?',(seq[0],target)).rowcount
                    if not changed:dest.execute('INSERT INTO sqlite_sequence(name,seq) VALUES (?,?)',(target,seq[0]))
        if progress:progress('Checking SQLite page integrity')
        if dest.execute('PRAGMA quick_check').fetchall()[0][0] != 'ok':
            raise ValueError('shared database failed its integrity check')
        if progress:progress('Checking saved run references')
        if dest.execute('PRAGMA foreign_key_check').fetchone() is not None:
            raise ValueError('shared database has broken foreign-key references')
        if versions != [c.execute('PRAGMA data_version').fetchone()[0] for c in (fees,tvl)]:
            raise ValueError('an input changed during the merge; retry with idle inputs')
        dest.execute('CREATE TABLE IF NOT EXISTS database_merge_receipts (started_at INTEGER,completed_at INTEGER,details_json TEXT NOT NULL)')
        result = plan | {'completed_at':int(time.time()),'integrity':'ok','foreign_keys':'ok',
                         'database_bytes':staging.stat().st_size}
        with dest:dest.execute('INSERT INTO database_merge_receipts VALUES (?,?,?)',(started,result['completed_at'],json.dumps(result,sort_keys=True)))
        dest.close();dest = None
        space_check()
        os.link(staging,out)  # Publish atomically without overwriting another file.
        staging.unlink()
        result['database_bytes'] = out.stat().st_size
        return result
    finally:
        if dest is not None:dest.close()
        if fees is not None:fees.close()
        if tvl is not None:tvl.close()
        staging.unlink(missing_ok=True)
        for suffix in ('-wal','-shm','-journal'):
            Path(str(staging)+suffix).unlink(missing_ok=True)
