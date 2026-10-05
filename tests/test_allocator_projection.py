"""Family-scoped allocator projection and offline source binding."""
import pytest
from test_canonical_fees import CASES, seed, db
from yearn_data.fees import index_canonical_fees
from yearn_data.fee_recompute import recompute_canonical_fees


def no_rpc(*args):
    pytest.fail('allocator projection requested a receipt')


def test_allocator_scope_leaves_v2_unprojected(db):
    seed(db)
    expected=db.execute("SELECT count(*) FROM strategy_reports WHERE version='v3'").fetchone()[0]
    assert index_canonical_fees(db,version='v3',receipt_fetcher=no_rpc)==expected
    assert db.execute('SELECT count(*) FROM canonical_fee_reports').fetchone()[0]==expected
    before=[tuple(r) for r in db.execute('SELECT accounting_json,evidence_json FROM canonical_fee_reports ORDER BY chain_id,tx_hash,report_log_index')]
    for mode in ('policy','formulas'):
        assert recompute_canonical_fees(db,mode)['accepted']==expected
    db.execute('DELETE FROM canonical_fee_reports')
    index_canonical_fees(db,version='v3',receipt_fetcher=no_rpc)
    assert [tuple(r) for r in db.execute('SELECT accounting_json,evidence_json FROM canonical_fee_reports ORDER BY chain_id,tx_hash,report_log_index')]==before


@pytest.mark.parametrize('field,value', [('total_fees_raw','123'),('protocol_fees_raw','123'),
    ('total_refunds_raw','123'),('asset_decimals',7),('asset','0x'+'ab'*20)])
def test_allocator_recompute_rejects_changed_inputs(db,field,value):
    seed(db,[next(c for c in CASES if c['event']['contract_family']=='yearn-v3-allocator')])
    index_canonical_fees(db,version='v3',receipt_fetcher=no_rpc)
    db.execute('UPDATE strategy_reports SET '+field+'=?',(value,))
    result=recompute_canonical_fees(db,'formulas')
    assert result['unavailable']==1
    row=db.execute('SELECT accounting_json,reason FROM canonical_fee_reports').fetchone()
    assert tuple(row)==(None,'cached_allocator_inputs_missing_or_changed')


def test_allocator_scope_rejects_filtered_v2_option(db):
    with pytest.raises(ValueError):index_canonical_fees(db,version='v3',filtered_evidence_only=True)


def test_direct_stage_schema_upgrade_preserves_bound_allocator_amounts(db):
    """A database created by the direct-fee PR must support P5 recomputation."""
    import json
    from yearn_data.fees import normalize_allocator_fees
    from yearn_data.storage import init_db
    seed(db, [next(c for c in CASES if c['event']['contract_family'] == 'yearn-v3-allocator')])
    report = db.execute('SELECT r.*,v.api_version FROM strategy_reports r JOIN vaults v '
                        'ON r.chain_id=v.chain_id AND r.vault_address=v.address').fetchone()
    # P4 schema has no candidate/decision columns; its persisted source bindings
    # are independently checked by test_direct_fee_bindings at the P4 head.
    db.execute('DROP TABLE canonical_fee_reports')
    db.execute('''CREATE TABLE canonical_fee_reports (
        chain_id INTEGER NOT NULL, tx_hash TEXT NOT NULL, report_log_index INTEGER NOT NULL,
        event_log_index INTEGER, contract_family TEXT NOT NULL, api_version TEXT,
        method_version TEXT NOT NULL, status TEXT NOT NULL, reason TEXT,
        accounting_json TEXT, evidence_json TEXT NOT NULL,
        PRIMARY KEY(chain_id,tx_hash,report_log_index))''')
    fields = ('chain_id', 'tx_hash', 'log_index', 'vault_address', 'strategy_address',
              'block_number', 'gain_raw', 'loss_raw', 'api_version')
    inputs = {key: report[key] for key in fields}
    evidence = {'source': 'strategy_reports', 'report_log_index': report['log_index'],
                'report_inputs': inputs, 'allocator_inputs': {**inputs, **{key: report[key] for key in
                ('asset', 'asset_decimals', 'total_fees_raw', 'protocol_fees_raw', 'total_refunds_raw')}}}
    expected = normalize_allocator_fees(report)
    db.execute('INSERT INTO canonical_fee_reports VALUES (?,?,?,?,?,?,?,?,?,?,?)',
        (report['chain_id'], report['tx_hash'], report['log_index'], report['log_index'],
         'yearn-v3-allocator', report['api_version'], 'canonical-fees-6', 'ok', None,
         json.dumps(expected), json.dumps(evidence)))
    db.commit(); init_db(db)
    for mode in ('policy', 'formulas'):
        assert recompute_canonical_fees(db, mode)['accepted'] == 1
        assert json.loads(db.execute('SELECT accounting_json FROM canonical_fee_reports').fetchone()[0]) == expected
    db.execute("UPDATE strategy_reports SET total_fees_raw='999'")
    assert recompute_canonical_fees(db, 'policy')['unavailable'] == 1
    assert db.execute('SELECT accounting_json FROM canonical_fee_reports').fetchone()[0] is None
