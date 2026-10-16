"""Closed-day boundaries and provenance of standard offline exports."""
import csv
import json

import pytest

from yearn_data.analysis import (create_analysis_run, complete_analysis_run,
                                 run_lifetime_yield, write_output)
from yearn_data.cli import main
from yearn_data.exports import export_analysis
from test_fee_valuation import db, seed, DAY, CUTOFF, ASSET, OTHER, outputs


def test_closed_day_earnings_include_retired_history_and_require_selected_prices(db):
    # The oldest retained report remains in scope, even after the vault retires.
    seed(db, 1, timestamp=DAY-100, gain='1000000')
    seed(db, 2, timestamp=CUTOFF-1, gain='1000000')
    seed(db, 3, timestamp=CUTOFF, gain='1000000')
    db.execute('''INSERT INTO vaults(chain_id,version,address,asset,asset_decimals,active,updated_at)
                  VALUES (1,'v3',?,?,6,0,1)''', (OTHER, ASSET))
    db.executemany('''INSERT INTO prices(chain_id,token_address,timestamp,source,price_usd,status)
                      VALUES (1,?,?,?,?, 'ok')''',
                   [(ASSET,DAY-100,'defillama',99), (ASSET,CUTOFF-1,'yearn-prices',2),
                    (ASSET,CUTOFF,'yearn-prices',3)])
    db.commit()
    run = run_lifetime_yield(db, fallback_price_source=None, before_timestamp=CUTOFF)
    rows = outputs(db, run, 'reports')
    assert len(rows) == 2
    assert {r['block_timestamp'] for r in rows} == {DAY-100, CUTOFF-1}
    summary = outputs(db, run, 'total_yield_summary')[0]
    assert summary['reports'] == 2
    assert summary['priced_reports'] == 1
    assert summary['unpriced_reports'] == 1
    assert summary['net_yield_usd'] == '2.0'
    assert db.execute('SELECT COUNT(*) FROM strategy_reports').fetchone()[0] == 3


def test_invalid_cutoff_creates_no_analysis_run(db):
    with pytest.raises(ValueError, match='closed UTC midnight'):
        run_lifetime_yield(db, before_timestamp=CUTOFF+1)
    assert db.execute('SELECT COUNT(*) FROM analysis_runs').fetchone()[0] == 0


def test_explicit_export_pins_csv_and_context_to_same_completed_run(db, tmp_path):
    first = create_analysis_run(db, 'lifetime-yield', {'before_timestamp':DAY, 'fallback_price_source':None})
    write_output(db, first, 'reports', {'amount':'1'})
    complete_analysis_run(db, first)
    second = create_analysis_run(db, 'lifetime-yield', {'before_timestamp':CUTOFF})
    write_output(db, second, 'reports', {'amount':'2'})
    complete_analysis_run(db, second)
    out = tmp_path/'first'
    path = db.execute('PRAGMA database_list').fetchone()[2]
    assert main(['--db',path,'export','lifetime-yield','--run-id',str(first),'--out',str(out)]) == 0
    context = json.loads((out/'context.json').read_text())
    assert context['id'] == first and context['name'] == 'lifetime-yield'
    assert context['params'] == {'before_timestamp':DAY, 'fallback_price_source':None}
    assert context['status'] == 'complete' and context['completed_at'] is not None
    assert context['files'] == ['reports.csv']
    with (out/'reports.csv').open() as fp:
        assert list(csv.DictReader(fp)) == [{'amount':'1'}]


def test_export_rejects_wrong_job_or_unfinished_run_before_writing(db, tmp_path):
    run = create_analysis_run(db, 'lifetime-yield')
    out = tmp_path/'invalid'
    with pytest.raises(ValueError, match='not a completed'):
        export_analysis(db, 'lifetime-yield', out, run_id=run)
    complete_analysis_run(db, run)
    with pytest.raises(ValueError, match='not a completed'):
        export_analysis(db, 'fee-usd', out, run_id=run)
    with pytest.raises(ValueError, match='not a completed'):
        export_analysis(db, 'lifetime-yield', out, run_id=0)
    assert not out.exists()
