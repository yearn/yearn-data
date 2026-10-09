"""Consolidate published history without discarding any historical identity.

Publication IDs identify the current revision. Collection run IDs remain small
provenance/checkpoint records, not a retention boundary for historical dates.
The database transaction verifies every retained observation before committing.
"""
from contextlib import closing
import hashlib
import json
import time

from .storage import connect, database_reference, table_exists, table_columns
from .tvl_api import observation_selection


OBSERVATIONS = ('tvl_snapshots', 'tvl_positions')
DERIVED = {
    'tvl_chart_rows': ('dataset_id', 'tvlDatasetId'),
    'tvl_chart_totals': ('dataset_id', 'tvlDatasetId'),
    'tvl_chart_publications': ('dataset_id', 'tvlDatasetId'),
    'api_tvl_dates': ('dataset_id', 'tvlDatasetId'),
    'api_fee_rows': ('dataset_id', 'feesDatasetId'),
    'api_fee_datasets': ('dataset_id', 'feesDatasetId'),
    'analytics_publications': ('id', 'analyticsPublicationId'),
    'api_releases': ('id', 'releaseId'),
}
DERIVED_KEYS = {
    'tvl_chart_rows': 'dataset_id,timestamp,chain_id,vault',
    'tvl_chart_totals': 'dataset_id,timestamp,group_by,mode,series',
    'tvl_chart_publications': 'dataset_id', 'api_tvl_dates': 'dataset_id',
    'api_fee_rows': 'dataset_id,kind,ordinal', 'api_fee_datasets': 'dataset_id',
    'analytics_publications': 'id', 'api_releases': 'id',
}
LOCK_TABLES = (*OBSERVATIONS, 'tvl_runs', 'analysis_runs', 'analysis_outputs',
               *DERIVED, 'api_selection', 'api_manifests', 'analytics_selection')
SCHEMA = '''CREATE TABLE IF NOT EXISTS canonical_history_receipts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, completed_at INTEGER NOT NULL,
    release_id TEXT NOT NULL, receipt_json TEXT NOT NULL)'''


def _sha(conn, expression):
    if hasattr(conn, 'raw'):
        return f"encode(sha256(convert_to({expression},'UTF8')),'hex')"
    return f'canonical_sha256({expression})'


def _count(conn, table, where='1=1', params=()):
    return conn.execute(f'SELECT COUNT(*) FROM {table} WHERE {where}', params).fetchone()[0]


def _record_sha(conn, columns, alias=''):
    values = ','.join(alias+column for column in columns)
    body = f'CAST(json_build_array({values}) AS TEXT)' if hasattr(conn, 'raw') else f'json_array({values})'
    return _sha(conn, body)


def _stage_derived(conn, table, column, identity):
    keys = DERIVED_KEYS[table]
    conn.execute(f'''CREATE TEMP TABLE canonical_keep_{table} AS SELECT {keys},
        {_record_sha(conn, table_columns(conn, table))} AS value_sha256
        FROM {table} WHERE {column}=?''', (identity,))
    conn.execute(f'CREATE UNIQUE INDEX canonical_keep_{table}_key ON canonical_keep_{table} ({keys})')
    return {'before': _count(conn, table), 'retained_rows': _count(conn, 'canonical_keep_'+table)}


def _verify_derived(conn, table, expected_rows):
    keys = DERIVED_KEYS[table].split(',')
    joins = ' AND '.join(f's.{key}=k.{key}' for key in keys)
    mismatches = conn.execute(f'''SELECT COUNT(*) FROM canonical_keep_{table} k
        LEFT JOIN {table} s ON {joins} WHERE s.{keys[0]} IS NULL
        OR {_record_sha(conn, table_columns(conn, table), 's.')}<>k.value_sha256''').fetchone()[0]
    if mismatches or _count(conn, table) != expected_rows:
        raise ValueError(f'{table}: current publication key/value verification failed')
    return mismatches


def _selection(conn):
    if not table_exists(conn, 'api_selection'):
        raise ValueError('publish a coherent hosted release before consolidating history')
    rows = list(conn.execute('SELECT * FROM api_selection'))
    if len(rows) != 1 or rows[0]['name'] != 'powerglove':
        raise ValueError('consolidation requires one canonical powerglove selection')
    release = rows[0]['release_id']
    row = conn.execute('SELECT body_json FROM api_releases WHERE id=?', (release,)).fetchone()
    if row is None:
        raise ValueError('selected release is missing')
    selection = json.loads(row[0]) | {'releaseId': release}
    manifests = {}
    for kind, field in (('fees', 'feesDatasetId'), ('tvl', 'tvlDatasetId')):
        row = conn.execute('SELECT body_json FROM api_manifests WHERE kind=? AND id=?',
                           (kind, selection[field])).fetchone()
        if row is None:
            raise ValueError('selected manifest is missing')
        manifests[kind] = json.loads(row[0])
    marker = conn.execute('SELECT version,row_count FROM tvl_chart_publications WHERE dataset_id=?',
                          (selection['tvlDatasetId'],)).fetchone()
    if marker is None or not marker['version'].endswith('+totals'):
        raise ValueError('selected TVL history and totals must be prepared')
    if marker['row_count'] != _count(conn, 'tvl_chart_rows', 'dataset_id=?', (selection['tvlDatasetId'],)):
        raise ValueError('selected chart row count does not match preparation')
    if not _count(conn, 'api_fee_datasets', 'dataset_id=?', (selection['feesDatasetId'],)):
        raise ValueError('selected financial serving rows are missing')
    row = conn.execute('SELECT body_json FROM analytics_publications WHERE id=?',
                       (selection['analyticsPublicationId'],)).fetchone()
    if row is None or any(json.loads(row[0])['meta'][k] != selection[k]
                          for k in ('feesDatasetId', 'tvlDatasetId')):
        raise ValueError('selected analytics do not match the canonical release')
    return selection, manifests


def _stage_observations(conn, table, run_ids):
    dimensions, quality = observation_selection(table)
    marks = ','.join('?' for _ in run_ids)
    # Refuse to erase an independently prepared update. Failed-only keys are
    # retained, with their failed run provenance, without becoming published.
    if conn.execute(f'''SELECT 1 FROM {table} s JOIN tvl_runs r ON r.id=s.run_id
        WHERE r.status IN ('complete','incomplete') AND r.completed_at IS NOT NULL
        AND s.run_id NOT IN ({marks}) LIMIT 1''', run_ids).fetchone():
        raise ValueError('finished observations exist outside the selected TVL publication')
    extra = ',block_number' if table == 'tvl_snapshots' else ''
    expected = 'canonical_keep_' + table
    conn.execute(f'''CREATE TEMP TABLE {expected} AS
        SELECT {dimensions},run_id{extra},{_sha(conn, 'data_json')} AS value_sha256
        FROM (SELECT *,ROW_NUMBER() OVER (PARTITION BY {dimensions}
            ORDER BY CASE WHEN run_id IN ({marks}) THEN 1 ELSE 0 END DESC,
            ({quality}) DESC,run_id DESC) AS canonical_rank FROM {table}) ranked
        WHERE canonical_rank=1''', run_ids)
    conn.execute(f'CREATE UNIQUE INDEX {expected}_key ON {expected} ({dimensions})')
    return {'before': _count(conn, table), 'canonical_keys': _count(conn, expected)}


def _retained_analyses(conn, financial):
    # Preserve one completed result for each supported analysis job, including
    # raw canonical-fee exports. Published fee/earnings runs take precedence.
    keep = {r['name']: r['id'] for r in conn.execute(
        "SELECT name,MAX(id) id FROM analysis_runs WHERE status='complete' GROUP BY name")}
    for field, name in (('earningsRunId', 'lifetime-yield'), ('feesRunId', 'fee-usd')):
        run_id = financial[field]
        row = conn.execute('SELECT name,status FROM analysis_runs WHERE id=?', (run_id,)).fetchone()
        if row is None or row['name'] != name or row['status'] != 'complete':
            raise ValueError('selected financial analysis is not complete')
        keep[name] = run_id
    return tuple(sorted(keep.values()))


def _stage_analyses(conn, keep):
    marks = ','.join('?' for _ in keep)
    # Event outputs use either log_index or report_log_index. Comparing every
    # event identity, not just totals or dates, prevents a shorter new analysis
    # from silently erasing older coverage. Include output name to keep the
    # independent earnings, fee and share-distribution projections distinct.
    identity = """r.name AS analysis,o.name AS output,
        json_extract(row_json,'$.chain_id') AS chain_id,
        lower(json_extract(row_json,'$.tx_hash')) AS tx_hash,
        COALESCE(json_extract(row_json,'$.log_index'),
                 json_extract(row_json,'$.report_log_index')) AS log_index"""
    source = 'analysis_outputs o JOIN analysis_runs r ON r.id=o.run_id'
    events = "json_extract(row_json,'$.tx_hash') IS NOT NULL"
    missing = conn.execute(f'''SELECT COUNT(*) FROM (
        SELECT {identity} FROM {source} WHERE {events}
        EXCEPT SELECT {identity} FROM {source} WHERE {events} AND o.run_id IN ({marks})
        ) missing''', keep).fetchone()[0]
    if missing:
        raise ValueError(f'canonical analyses would lose {missing} historical event identities')
    conn.execute(f'''CREATE TEMP TABLE canonical_keep_outputs AS
        SELECT rowid AS output_id,run_id,name,{_sha(conn, 'row_json')} AS value_sha256
        FROM analysis_outputs WHERE run_id IN ({marks})''', keep)
    conn.execute('CREATE UNIQUE INDEX canonical_keep_outputs_key ON canonical_keep_outputs(output_id)')
    return {'before': _count(conn, 'analysis_outputs'),
            'retained_rows': _count(conn, 'canonical_keep_outputs'), 'run_ids': list(keep),
            'missing_event_keys': missing}


def _verify_observations(conn, table, expected_rows):
    dimensions, _ = observation_selection(table)
    joins = ' AND '.join(f's.{key}=k.{key}' for key in dimensions.split(','))
    extra = ' OR s.block_number IS DISTINCT FROM k.block_number' if table == 'tvl_snapshots' else ''
    mismatches = conn.execute(f'''SELECT COUNT(*) FROM canonical_keep_{table} k
        LEFT JOIN {table} s ON {joins} AND s.run_id=k.run_id
        WHERE s.run_id IS NULL OR {_sha(conn, 's.data_json')}<>k.value_sha256{extra}''').fetchone()[0]
    actual = _count(conn, table)
    if mismatches or actual != expected_rows:
        raise ValueError(f'{table}: canonical key/value verification failed')
    return {'after': actual, 'missing_or_changed_keys': mismatches}


def _delete_obsolete(conn, selection, keep):
    for table in OBSERVATIONS:
        dimensions, _ = observation_selection(table)
        joins = ' AND '.join(f'k.{key}={table}.{key}' for key in dimensions.split(','))
        conn.execute(f'''DELETE FROM {table} WHERE NOT EXISTS (
            SELECT 1 FROM canonical_keep_{table} k WHERE {joins} AND k.run_id={table}.run_id)''')
    marks = ','.join('?' for _ in keep)
    conn.execute(f'DELETE FROM analysis_outputs WHERE run_id NOT IN ({marks})', keep)
    conn.execute(f"UPDATE analysis_runs SET status='superseded' WHERE status='complete' AND id NOT IN ({marks})", keep)
    # The same canonical analytics powers both the file-backed and hosted API.
    conn.execute("INSERT INTO analytics_selection VALUES ('powerglove',?) "
                 "ON CONFLICT(name) DO UPDATE SET publication_id=excluded.publication_id",
                 (selection['analyticsPublicationId'],))
    if conn.execute("SELECT 1 FROM analytics_selection WHERE name<>'powerglove' LIMIT 1").fetchone():
        raise ValueError('additional analytics selections require explicit consolidation')
    for table, (column, field) in DERIVED.items():
        conn.execute(f'DELETE FROM {table} WHERE {column}<>?', (selection[field],))
    conn.execute("DELETE FROM api_manifests WHERE NOT ((kind='fees' AND id=?) OR (kind='tvl' AND id=?))",
                 (selection['feesDatasetId'], selection['tvlDatasetId']))


def consolidate(database, *, apply=False, progress=None):
    """Plan by default; apply atomically only after validating all preserved keys.

    Source events, receipts, traces, prices, coverage and run/checkpoint metadata
    are retained. The only removed data are superseded observations and derived
    outputs. No compaction or external calls occur inside this transaction.
    """
    notify = progress or (lambda message: None)
    with closing(connect(database, direct=True)) as conn:
        if hasattr(conn, 'raw'):
            conn.raw.execute('BEGIN ISOLATION LEVEL REPEATABLE READ')
            conn.raw.execute("SET LOCAL lock_timeout='5s'")
            if not conn.execute('SELECT pg_try_advisory_xact_lock(743219890)').fetchone()[0]:
                raise ValueError('another canonical consolidation is running')
            if apply:
                tables = sorted(t for t in set(LOCK_TABLES) if table_exists(conn, t))
                conn.raw.execute('LOCK TABLE '+','.join(tables)+' IN SHARE ROW EXCLUSIVE MODE')
        else:
            conn.create_function('canonical_sha256', 1,
                                 lambda text: hashlib.sha256(text.encode()).hexdigest(), deterministic=True)
            conn.execute('BEGIN IMMEDIATE' if apply else 'BEGIN')
        try:
            for table in ('tvl_runs', 'analysis_runs'):
                if conn.execute(f"SELECT 1 FROM {table} WHERE status='running' LIMIT 1").fetchone():
                    raise ValueError('finish or resolve running jobs before consolidation')
            selection, manifests = _selection(conn)
            run_ids = tuple(r['id'] for r in manifests['tvl']['runs'])
            if not run_ids:
                raise ValueError('selected TVL publication has no historical runs')
            report = {'database': database_reference(database), 'applied': apply,
                      'selection': selection, 'observations': {}, 'derived': {}}
            for table in OBSERVATIONS:
                notify('Inventorying canonical keys: '+table)
                report['observations'][table] = _stage_observations(conn, table, run_ids)
            keep = _retained_analyses(conn, manifests['fees'])
            notify('Verifying analysis event coverage')
            report['analysis_outputs'] = _stage_analyses(conn, keep)
            for table, (column, field) in DERIVED.items():
                notify('Inventorying current publication: '+table)
                report['derived'][table] = _stage_derived(conn, table, column, selection[field])
            if not apply:
                conn.rollback()
                return report
            notify('Removing superseded records inside the verification transaction')
            _delete_obsolete(conn, selection, keep)
            for table in OBSERVATIONS:
                expected = report['observations'][table]['canonical_keys']
                report['observations'][table].update(_verify_observations(conn, table, expected))
            mismatches = conn.execute(f'''SELECT COUNT(*) FROM canonical_keep_outputs k
                LEFT JOIN analysis_outputs o ON o.rowid=k.output_id
                WHERE o.rowid IS NULL OR o.run_id<>k.run_id OR o.name<>k.name
                OR {_sha(conn, 'o.row_json')}<>k.value_sha256''').fetchone()[0]
            if mismatches or _count(conn, 'analysis_outputs') != report['analysis_outputs']['retained_rows']:
                raise ValueError('analysis output order/value verification failed')
            report['analysis_outputs']['missing_or_changed_rows'] = mismatches
            for table, (column, field) in DERIVED.items():
                report['derived'][table]['missing_or_changed_keys'] = _verify_derived(
                    conn, table, report['derived'][table]['retained_rows'])
            if _selection(conn)[0] != selection:
                raise ValueError('canonical publication changed during consolidation')
            conn.execute(SCHEMA)
            conn.execute('INSERT INTO canonical_history_receipts(completed_at,release_id,receipt_json) VALUES (?,?,?)',
                         (int(time.time()), selection['releaseId'], json.dumps(report, sort_keys=True)))
            conn.commit()
            notify('Committed: every canonical historical key and value preserved')
            return report
        except BaseException:
            conn.rollback()
            raise
