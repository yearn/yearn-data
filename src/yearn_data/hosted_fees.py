"""Compact published financial rows; ingestion validation runs only at publication."""
from contextlib import closing
import json

from .pairing import PairingDataset, FEE_FIELDS, EARNINGS_FIELDS, _json
from .storage import connect

SCHEMA = '''
CREATE TABLE IF NOT EXISTS api_fee_datasets (
 dataset_id TEXT PRIMARY KEY, metadata_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS api_fee_rows (
 dataset_id TEXT NOT NULL, kind TEXT NOT NULL, ordinal INTEGER NOT NULL,
 timestamp INTEGER NOT NULL, chain_id INTEGER NOT NULL, vault TEXT NOT NULL,
 row_json TEXT NOT NULL, PRIMARY KEY(dataset_id,kind,ordinal));
CREATE INDEX IF NOT EXISTS api_fee_rows_filter ON api_fee_rows(dataset_id,chain_id,vault,timestamp);
'''


def prepare(conn, dataset):
    """Publish compact rows atomically, preserving source order and decimal strings."""
    conn.executescript(SCHEMA)
    with conn:
        if conn.execute('SELECT 1 FROM api_fee_datasets WHERE dataset_id=?', (dataset.id,)).fetchone():
            return
        for kind, rows, fields in (('fees', dataset.fees, FEE_FIELDS),
                                   ('earnings', dataset.earnings, EARNINGS_FIELDS)):
            keep = set(fields.values()) | {'chain_id', 'vault_address', 'block_timestamp',
                                           'contract_family', 'version'}
            conn.executemany('INSERT INTO api_fee_rows VALUES (?,?,?,?,?,?,?)',
                [(dataset.id, kind, i, row['block_timestamp'], row['chain_id'],
                  row['vault_address'].lower(), _json({k:v for k,v in row.items() if k in keep}))
                 for i,row in enumerate(rows)])
        meta = {'cutoff':dataset.cutoff, 'chains':dataset.chains,
                'vaults':sorted(dataset.vaults), 'vaultNames':dataset.vault_names}
        conn.execute('INSERT INTO api_fee_datasets VALUES (?,?)', (dataset.id, _json(meta)))


class PublishedFees(PairingDataset):
    def __init__(self, database, identity):
        self.database, self.id = database, identity
        with closing(connect(database, readonly=True)) as conn:
            row = conn.execute('SELECT metadata_json FROM api_fee_datasets WHERE dataset_id=?',
                               (identity,)).fetchone()
        if row is None:
            raise ValueError('financial serving data must be prepared before publication')
        meta = json.loads(row[0])
        self.cutoff = meta['cutoff']
        self.chains = tuple(meta['chains'])
        self.vaults = frozenset(tuple(v) for v in meta['vaults'])
        self.vault_names = meta['vaultNames']

    def view_rows(self, since, end, chains, vault_address):
        clauses = ['dataset_id=?', 'timestamp<?']
        params = [self.id, end]
        if since is not None:
            clauses.append('timestamp>=?'); params.append(since)
        if chains:
            clauses.append('chain_id IN ('+','.join('?' for _ in chains)+')'); params.extend(chains)
        if vault_address is not None:
            clauses.append('vault=?'); params.append(vault_address)
        fees, earnings = [], []
        with closing(connect(self.database, readonly=True)) as conn:
            for row in conn.execute('SELECT kind,row_json FROM api_fee_rows WHERE '+
                                    ' AND '.join(clauses)+' ORDER BY kind,ordinal', params):
                (fees if row[0]=='fees' else earnings).append(json.loads(row[1]))
        return fees, earnings
