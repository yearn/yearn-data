"""Prepared chart rows for an immutable TVL publication.

Run the existing accounting once, then serve slim dated rows instead of repeatedly
loading raw snapshots and positions. The completion marker and rows commit together.
"""
from contextlib import closing
import time
from collections import defaultdict
from decimal import Decimal,localcontext

from .storage import connect, table_exists

VERSION = 'dated-accounting-1'
FIELDS = ('chain_id','vault','name','category','version','asset_units','tvl_usd','external_tvl_usd')
SCHEMA = '''
CREATE TABLE IF NOT EXISTS tvl_chart_publications (
    dataset_id TEXT PRIMARY KEY, version TEXT NOT NULL,
    row_count INTEGER NOT NULL, prepared_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS tvl_chart_rows (
    dataset_id TEXT NOT NULL, timestamp INTEGER NOT NULL,
    chain_id INTEGER NOT NULL, vault TEXT NOT NULL, name TEXT,
    category TEXT, version TEXT NOT NULL, asset_units TEXT, tvl_usd TEXT,
    external_tvl_usd TEXT,
    PRIMARY KEY(dataset_id,timestamp,chain_id,vault));
CREATE INDEX IF NOT EXISTS tvl_chart_rows_vault_idx
ON tvl_chart_rows(dataset_id,chain_id,vault,timestamp);
CREATE TABLE IF NOT EXISTS tvl_chart_totals (
    dataset_id TEXT NOT NULL, timestamp INTEGER NOT NULL,
    group_by TEXT NOT NULL, mode TEXT NOT NULL, series TEXT NOT NULL,
    value TEXT NOT NULL, PRIMARY KEY(dataset_id,timestamp,group_by,mode,series));
'''


def ready(dataset):
    with closing(connect(dataset.database,readonly=True)) as conn:
        return table_exists(conn,'tvl_chart_publications') and conn.execute(
            'SELECT 1 FROM tvl_chart_publications WHERE dataset_id=? AND version IN (?,?)',
            (dataset.id,VERSION,VERSION+'+totals')).fetchone() is not None


def totals_ready(dataset):
    with closing(connect(dataset.database,readonly=True)) as conn:
        return table_exists(conn,'tvl_chart_publications') and conn.execute(
            'SELECT 1 FROM tvl_chart_publications WHERE dataset_id=? AND version=?',
            (dataset.id,VERSION+'+totals')).fetchone() is not None


def totals(dataset,timestamps,group,mode,chain_id=None):
    if not timestamps:return []
    marks=','.join('?' for _ in timestamps)
    statement=f'SELECT timestamp,series,value FROM tvl_chart_totals WHERE dataset_id=? AND timestamp IN ({marks}) AND group_by=? AND mode=?'
    args=(dataset.id,*timestamps,group,mode)
    if chain_id is not None:
        from .tvl_api import CHAINS
        statement+=' AND series=?';args+=(CHAINS.get(chain_id,f'Chain {chain_id}'),)
    statement+=' ORDER BY timestamp,series'
    with closing(connect(dataset.database,readonly=True)) as conn:
        return list(conn.execute(statement,args))


def prepare_totals(dataset):
    if totals_ready(dataset):return
    from .tvl_api import CHAINS
    dates=tuple(date for date in dataset.dates() if date<=dataset.as_of())
    with closing(connect(dataset.database,direct=True)) as conn:
        conn.executescript(SCHEMA)
        with conn:
            conn.execute('DELETE FROM tvl_chart_totals WHERE dataset_id=?',(dataset.id,))
            for offset in range(0,len(dates),31):
                batch=[]
                for timestamp,points in rows(dataset,dates[offset:offset+31]):
                    groups=defaultdict(list)
                    for row in points:
                        for mode,field in (('raw','tvl_usd'),('external','external_tvl_usd')):
                            if row[field] is None:continue
                            value=Decimal(row[field])
                            groups[('chain',mode,CHAINS.get(row['chain_id'],f"Chain {row['chain_id']}"))].append(value)
                            groups[('category',mode,row['category'])].append(value)
                    with localcontext() as ctx:
                        ctx.prec=78
                        batch.extend((dataset.id,timestamp,*identity,str(sum(values,Decimal(0))))
                                     for identity,values in groups.items())
                conn.executemany('INSERT INTO tvl_chart_totals VALUES (?,?,?,?,?,?)',batch)
            conn.execute('UPDATE tvl_chart_publications SET version=? WHERE dataset_id=?',(VERSION+'+totals',dataset.id))


def rows(dataset,timestamps,chain_id=None):
    if not timestamps:return
    marks = ','.join('?' for _ in timestamps)
    statement = f'SELECT timestamp,{",".join(FIELDS)} FROM tvl_chart_rows WHERE dataset_id=? AND timestamp IN ({marks})'
    args = (dataset.id,*timestamps)
    if chain_id is not None:
        statement += ' AND chain_id=?';args += (chain_id,)
    statement += ' ORDER BY timestamp,chain_id,vault'
    with closing(connect(dataset.database,readonly=True)) as conn, conn:
        records = conn.stream(statement,args) if hasattr(conn,'raw') else conn.execute(statement,args)
        from itertools import groupby
        for timestamp,group in groupby(records,lambda row:row['timestamp']):
            yield timestamp,tuple({field:row[field] for field in FIELDS} for row in group)


def references(dataset,window):
    if not window:return
    marks=','.join('?' for _ in window)
    statement=f'''WITH candidates AS (
        SELECT timestamp,chain_id,vault,asset_units,tvl_usd,
            ROW_NUMBER() OVER (PARTITION BY chain_id,vault ORDER BY timestamp) n
        FROM tvl_chart_rows WHERE dataset_id=? AND timestamp IN ({marks})
        AND CAST(asset_units AS REAL)>0 AND CAST(tvl_usd AS REAL)>0)
        SELECT timestamp,chain_id,vault,asset_units,tvl_usd FROM candidates
        WHERE n<=7 ORDER BY timestamp,chain_id,vault'''
    with closing(connect(dataset.database,readonly=True)) as conn:
        yield from (dict(row) for row in conn.execute(statement,(dataset.id,*window)))


def prepare(dataset,progress=None):
    if ready(dataset):
        prepare_totals(dataset)
        return {'datasetId':dataset.id,'status':'already-prepared'}
    notify=progress or (lambda message:None)
    count=0
    with closing(connect(dataset.database,direct=True)) as conn:
        conn.executescript(SCHEMA)
        with conn:
            if hasattr(conn,'raw'):
                # One builder; release on transaction rollback or commit.
                if not conn.execute('SELECT pg_try_advisory_xact_lock(743219889)').fetchone()[0]:
                    raise ValueError('TVL chart preparation is already running')
            conn.execute('DELETE FROM tvl_chart_rows WHERE dataset_id=?',(dataset.id,))
            conn.execute('DELETE FROM tvl_chart_publications WHERE dataset_id=?',(dataset.id,))
            dates=tuple(date for date in dataset.dates() if date<=dataset.as_of())
            for offset in range(0,len(dates),31):
                batch=[]
                for timestamp,points,_,_ in dataset.iter_frames(dates[offset:offset+31]):
                    for row in points:
                        values=[row.get(field) for field in FIELDS]
                        # Persist Decimal quantities exactly, including unknowns.
                        values[5:]=[None if value is None else str(value) for value in values[5:]]
                        values[3]=row.get('category',row['version'])
                        batch.append((dataset.id,timestamp,*values))
                if hasattr(conn,'raw'):
                    with conn.raw.cursor() as cursor, cursor.copy('COPY tvl_chart_rows FROM STDIN') as copy:
                        for record in batch:copy.write_row(record)
                else:
                    conn.executemany('INSERT INTO tvl_chart_rows VALUES (?,?,?,?,?,?,?,?,?,?)',batch)
                count+=len(batch)
                notify(f'Prepared {min(offset+31,len(dates))}/{len(dates)} dates; {count:,} vault rows')
            conn.execute('INSERT INTO tvl_chart_publications VALUES (?,?,?,?)',(dataset.id,VERSION,count,int(time.time())))
        if hasattr(conn,'raw'):
            conn.raw.execute('ANALYZE tvl_chart_rows')
    dataset._prepared=True
    prepare_totals(dataset)
    return {'datasetId':dataset.id,'status':'prepared','rows':count,'dates':len(dates)}
