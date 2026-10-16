"""Direct fee snapshots carry enough source identity for later offline recomputation."""
import json

from test_canonical_fees import CASES, db, seed
from yearn_data.fees import index_canonical_fees


def test_direct_allocator_projection_retains_source_snapshot(db):
    case = next(c for c in CASES if c['event']['contract_family'] == 'yearn-v3-allocator')
    seed(db, [case])
    index_canonical_fees(db, version='v3')
    report = db.execute('SELECT r.*,v.api_version FROM strategy_reports r JOIN vaults v '
                        'ON r.chain_id=v.chain_id AND r.vault_address=v.address').fetchone()
    evidence = json.loads(db.execute('SELECT evidence_json FROM canonical_fee_reports').fetchone()[0])
    fields = ('chain_id', 'tx_hash', 'log_index', 'vault_address', 'strategy_address',
              'block_number', 'gain_raw', 'loss_raw', 'api_version')
    assert evidence['report_inputs'] == {key: report[key] for key in fields}
    assert evidence['allocator_inputs'] == {
        **evidence['report_inputs'], **{key: report[key] for key in
        ('asset', 'asset_decimals', 'total_fees_raw', 'protocol_fees_raw', 'total_refunds_raw')}}
