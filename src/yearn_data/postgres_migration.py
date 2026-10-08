"""Atomic, streaming SQLite import into an empty Postgres schema.

No dump file, historical refetch, destructive overwrite, or SQLite writes. The
source read transaction pins one consistent snapshot for the entire import.
"""
from __future__ import annotations

from contextlib import closing
import hashlib
from pathlib import Path
import re

from psycopg import sql

from .postgres import schema_statements, create_indexes
from .storage import connect, database_reference, is_postgres


def _tables(conn):
    return list(conn.execute("SELECT name,sql FROM sqlite_master WHERE type='table' "
                             "AND name NOT LIKE 'sqlite_%' ORDER BY name"))


def _empty_target(conn):
    if conn.execute('SELECT 1 FROM information_schema.tables WHERE table_schema=current_schema() LIMIT 1').fetchone():
        raise ValueError('Postgres target schema must be empty; existing tables are never overwritten')


def _source(path):
    if is_postgres(path):
        raise ValueError('migration source must be a SQLite file')
    source = connect(path, readonly=True)
    source.execute('BEGIN')
    return source


def _description(source, target):
    tables = _tables(source)
    if not {'analysis_outputs', 'tvl_snapshots'} <= {row['name'] for row in tables}:
        raise ValueError('source must be the shared fee/earnings and TVL database')
    unsupported = source.execute("SELECT name FROM sqlite_master WHERE type IN ('view','trigger') LIMIT 1").fetchone()
    if unsupported:
        raise ValueError('source views/triggers need an explicit migration')
    counts = {row['name']: source.execute('SELECT count(*) FROM "'+row['name']+'"').fetchone()[0] for row in tables}
    return {'target': database_reference(target), 'tables': counts, 'total_rows': sum(counts.values()),
            'sqlite_bytes': source.execute('PRAGMA page_count').fetchone()[0] * source.execute('PRAGMA page_size').fetchone()[0],
            'history_refetch_required': False}


def migration_plan(source_path, target='neon', *, schema=None):
    if not is_postgres(target):
        raise ValueError('migration target must be Postgres')
    with closing(_source(source_path)) as source, closing(connect(target, readonly=True, schema=schema, direct=True)) as dest:
        _empty_target(dest)
        return _description(source, target) | {'check_only': True}


def _output_hashes(conn, *, postgres=False):
    hashes = {}
    statement = 'SELECT run_id,name,row_json FROM analysis_outputs ORDER BY rowid'
    # A server cursor keeps the production output set out of client memory.
    cursor = conn.raw.cursor(name='migration_outputs') if postgres else conn.cursor()
    if postgres:
        cursor.itersize = 10_000
    try:
        cursor.execute(statement)
        for row in cursor:
            identity = (row[0], row[1])
            hashes.setdefault(identity, hashlib.sha256()).update(row[2].encode() + b'\n')
    finally:
        cursor.close()
    return {key: digest.hexdigest() for key, digest in hashes.items()}


def migrate_sqlite(source_path, target='neon', *, schema=None, batch_size=10_000, progress=None):
    if batch_size < 1:
        raise ValueError('batch_size must be positive')
    if not is_postgres(target):
        raise ValueError('migration target must be Postgres')
    notify = progress or (lambda message: None)
    with closing(_source(source_path)) as source, closing(connect(target, schema=schema, direct=True)) as dest:
        _empty_target(dest)
        plan = _description(source, target)
        indexes = [row[0] for row in source.execute("SELECT sql FROM sqlite_master WHERE type='index' AND sql IS NOT NULL ORDER BY name")]
        statements = schema_statements(';\n'.join([row['sql'] for row in _tables(source)] + indexes) + ';')
        # The entire import is one transaction: failure leaves no partial tables.
        with dest.raw.transaction():
            if not dest.raw.execute('SELECT pg_try_advisory_xact_lock(743219888)').fetchone()[0]:
                raise ValueError('another database migration is running')
            _empty_target(dest)
            for statement in statements:
                if re.match('CREATE TABLE ', statement, re.I):
                    dest.raw.execute(statement)
            copied = {}
            table_definitions = {row['name']: row for row in _tables(source)}
            ordered_names = [re.match(r'CREATE TABLE (?:IF NOT EXISTS )?["`]?([\w]+)', statement, re.I)[1]
                             for statement in statements if re.match('CREATE TABLE ', statement, re.I)]
            for name in ordered_names:
                row = table_definitions[name]
                columns = [column['name'] for column in source.execute(f'PRAGMA table_info("{name}")')]
                if name == 'analysis_outputs':
                    columns.insert(0, 'rowid')
                names = ','.join('"'+column+'"' for column in columns)
                reader = source.execute(f'SELECT {names} FROM "{name}"')
                count = 0
                with dest.raw.cursor() as cursor, cursor.copy(sql.SQL('COPY {} ({}) FROM STDIN').format(
                        sql.Identifier(name), sql.SQL(',').join(map(sql.Identifier, columns)))) as copy:
                    while rows := reader.fetchmany(batch_size):
                        for record in rows:
                            copy.write_row(tuple(record))
                        count += len(rows)
                copied[name] = count
                if count != plan['tables'][name]:
                    raise ValueError('source row count changed for '+name)
                notify(f'copied {name}: {count:,} rows')
            for statement in statements:
                if not re.match('CREATE TABLE ', statement, re.I):
                    dest.raw.execute(statement)
            create_indexes(dest)
            # Include reserved SQLite IDs, even if their rows have been deleted.
            for row in _tables(source):
                name = row['name']
                identity = 'rowid' if name == 'analysis_outputs' else ('id' if 'AUTOINCREMENT' in row['sql'].upper() else None)
                if identity is None:
                    continue
                reserved = 0
                if identity == 'id':
                    saved = source.execute('SELECT seq FROM sqlite_sequence WHERE name=?', (name,)).fetchone()
                    reserved = saved[0] if saved else 0
                maximum = dest.raw.execute(sql.SQL('SELECT MAX({}) FROM {}').format(sql.Identifier(identity), sql.Identifier(name))).fetchone()[0]
                next_id = max(reserved, maximum or 0, 0) + 1
                dest.raw.execute('SELECT setval(pg_get_serial_sequence(%s,%s),%s,false)', (name,identity,next_id))
            # Count destination rows as well as sent rows; verify all stored
            # analysis fingerprints so immutable fee/earnings selections survive.
            for name, count in copied.items():
                actual = dest.raw.execute(sql.SQL('SELECT count(*) FROM {}').format(sql.Identifier(name))).fetchone()[0]
                if actual != count:
                    raise ValueError('destination row count mismatch for '+name)
            if _output_hashes(source) != _output_hashes(dest, postgres=True):
                raise ValueError('analysis output text/order changed during migration')
            notify('validated table counts, foreign keys and analysis output fingerprints')
        return plan | {'check_only': False, 'status': 'complete', 'source': str(Path(source_path).resolve()),
                       'analysis_fingerprints_preserved': True, 'source_unchanged': True}
