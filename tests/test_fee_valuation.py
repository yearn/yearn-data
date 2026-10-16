import json
from decimal import Decimal

import pytest

from yearn_data import fee_valuation as valuation, pricing
from yearn_data.cli import main
from yearn_data.exports import export_analysis
from yearn_data.storage import connect, init_db

ASSET = '0x'+'11'*20
OTHER = '0x'+'22'*20
DAY = 1704067200
CUTOFF = DAY+86400
FAMILY = 'yearn-v3-allocator'


@pytest.fixture
def db(tmp_path):
    conn = connect(tmp_path/'fees.sqlite');init_db(conn)
    yield conn
    conn.close()


def seed(conn, number, fee='1000000', *, asset=ASSET, decimals=6, timestamp=DAY+100,
         accepted=True, tokenized=False, chain=1, gain='0'):
    tx='0x'+f'{number:064x}'
    amounts={'total_fees_paid_raw':fee,'gross_gain_raw':gain,'loss_raw':'0',
             'protocol_fee_raw':None,'amount_source':'observed-event'}
    if tokenized:
        event={'chain_id':chain,'vault_address':OTHER,'tx_hash':tx,'log_index':0,
               'block_timestamp':timestamp,'asset':asset,'asset_decimals':decimals,
               'contract_family':'yearn-v3-tokenized-strategy','accounting':amounts}
        conn.execute('INSERT INTO tokenized_fee_events VALUES (?,?,?,?)',(chain,tx,0,json.dumps(event)))
    else:
        conn.execute('''INSERT INTO strategy_reports(chain_id,version,vault_address,strategy_address,
            tx_hash,log_index,block_number,block_timestamp,asset,asset_decimals,gain_raw,loss_raw,net_raw)
            VALUES (?,'v3',?,?,?,0,1,?,?,?,?,'0',?)''',
            (chain,OTHER,OTHER,tx,timestamp,asset,decimals,gain,gain))
        conn.execute('''INSERT INTO canonical_fee_reports(chain_id,tx_hash,report_log_index,
            contract_family,method_version,status,reason,accounting_json,evidence_json,decision_json)
            VALUES (?,?,0,?,'test',?,?,?,?,?)''',
            (chain,tx,FAMILY,'ok' if accepted else 'unresolved',None if accepted else 'missing_execution',
             json.dumps(amounts) if accepted else None,'{}',json.dumps({'status':'accepted' if accepted else 'unavailable'})))
    conn.commit()


def point(target, price='2.5'):
    return price,'ok',{'provider':'yearn-prices','normalized_timestamp':target[2],
                       'adapter':'batchHistorical','upstream_source':'test','confidence':1}


def outputs(conn, run, name):
    return [json.loads(r[0]) for r in conn.execute('SELECT row_json FROM analysis_outputs WHERE run_id=? AND name=?',(run,name))]


def test_deduplicates_across_families_and_resumes_without_requests(db, monkeypatch):
    seed(db,1);seed(db,2,tokenized=True);seed(db,3,fee='0',asset=OTHER)
    calls=[]
    monkeypatch.setattr(pricing,'fetch_yearn_prices_batch',lambda targets: calls.append(targets) or {t:point(t) for t in targets})
    assert valuation.price_fees(db,before_timestamp=CUTOFF)['selected_asset_days']==1
    assert calls==[[(1,ASSET,CUTOFF-1)]]
    assert valuation.price_fees(db,before_timestamp=CUTOFF)['selected_asset_days']==0
    assert len(calls)==1
    run=valuation.run_fee_usd(db,before_timestamp=CUTOFF)
    groups=outputs(db,run,'fee_usd_by_family')
    assert len(groups)==2
    assert all(g['known_total_fees_paid_usd_subtotal']=='2.5' for g in groups)
    assert all(g['fee_usd_complete'] and not g['historical_discovery_complete'] for g in groups)
    assert all(g['protocol_fee_usd_unknown_events']==g['events'] for g in groups)


def test_completeness_distinguishes_accounting_metadata_prices_and_zero(db):
    seed(db,1);seed(db,2,accepted=False);seed(db,3,asset=OTHER)
    seed(db,4,decimals=None);seed(db,5,fee='0',asset=None,decimals=None)
    valuation.save_price(db,(1,ASSET,CUTOFF-1),point((1,ASSET,CUTOFF-1)))
    run=valuation.run_fee_usd(db,before_timestamp=CUTOFF)
    bucket=outputs(db,run,'fee_usd_by_family')[0]
    assert bucket['events']==5 and bucket['known_fee_events']==2
    assert bucket['accounting_unavailable_events']==1
    assert bucket['metadata_unavailable_events']==1
    assert bucket['unpriced_fee_events']==1 and bucket['zero_fee_events']==1
    assert bucket['known_total_fees_paid_usd_subtotal']=='2.5'
    assert not bucket['accounting_complete'] and not bucket['fee_usd_complete']
    events=outputs(db,run,'fee_usd_events')
    assert [e['total_fees_paid_usd'] for e in events]==['2.5',None,None,None,'0']


def test_no_known_value_is_null_not_zero(db):
    seed(db,1)
    row=outputs(db,valuation.run_fee_usd(db,before_timestamp=CUTOFF),'fee_usd_by_family')[0]
    assert row['known_total_fees_paid_usd_subtotal'] is None
    assert row['accounting_complete'] and not row['fee_usd_complete']


def test_exact_omission_check_and_retry_missing_are_explicit(db, monkeypatch):
    seed(db,1);calls=[]
    monkeypatch.setattr(pricing,'fetch_yearn_prices_batch',lambda targets:{})
    monkeypatch.setattr(pricing,'fetch_yearn_price',lambda *t: calls.append(t) or (None,'missing',{'provider':'yearn-prices','normalized_timestamp':t[2]}))
    result=valuation.price_fees(db,before_timestamp=CUTOFF)
    assert result['results']=={'missing':1} and len(calls)==1
    assert valuation.price_fees(db,before_timestamp=CUTOFF)['selected_asset_days']==0
    monkeypatch.setattr(pricing,'fetch_yearn_prices_batch',lambda targets:{t:point(t) for t in targets})
    assert valuation.price_fees(db,before_timestamp=CUTOFF,retry_missing=True)['results']=={'ok':1}


def test_batch_outage_does_not_fan_out_to_exact_or_another_provider(db, monkeypatch):
    seed(db,1);seed(db,2,asset=OTHER)
    def fail(*a,**kw):raise pricing.YearnPricesRequestError('unavailable')
    monkeypatch.setattr(pricing,'fetch_yearn_prices_batch',fail)
    monkeypatch.setattr(pricing,'fetch_yearn_price',lambda *a:pytest.fail('exact fanout'))
    monkeypatch.setattr(pricing,'fetch_defillama_prices_batch',lambda *a, **kw:pytest.fail('fallback'))
    monkeypatch.setattr(pricing,'_row_level_fallback_price',lambda *a, **kw:pytest.fail('on-chain fallback'))
    assert valuation.price_fees(db,before_timestamp=CUTOFF)['results']=={'retryable':2}


def test_authentication_failure_stops_without_caching_missing(db, monkeypatch):
    seed(db,1)
    def fail(*a):raise pricing.YearnPricesAuthenticationError('denied')
    monkeypatch.setattr(pricing,'fetch_yearn_prices_batch',fail)
    with pytest.raises(pricing.YearnPricesAuthenticationError):valuation.price_fees(db,before_timestamp=CUTOFF)
    assert db.execute('SELECT COUNT(*) FROM fee_daily_prices').fetchone()[0]==0


def test_changed_accounting_withdraws_usd_and_changed_endpoint_does_not_reuse_cache(db, monkeypatch):
    seed(db,1);target=(1,ASSET,CUTOFF-1);valuation.save_price(db,target,point(target))
    run=valuation.run_fee_usd(db,before_timestamp=CUTOFF)
    assert outputs(db,run,'fee_usd_events')[0]['total_fees_paid_usd']=='2.5'
    monkeypatch.setenv('YEARN_PRICE_PROD_BASE_URL','https://another.invalid')
    run=valuation.run_fee_usd(db,before_timestamp=CUTOFF)
    assert outputs(db,run,'fee_usd_events')[0]['total_fees_paid_usd'] is None
    monkeypatch.delenv('YEARN_PRICE_PROD_BASE_URL')
    db.execute("UPDATE canonical_fee_reports SET status='unresolved',accounting_json=NULL")
    run=valuation.run_fee_usd(db,before_timestamp=CUTOFF)
    assert outputs(db,run,'fee_usd_events')[0]['valuation_status']=='accounting_unavailable'


def test_precision_and_component_zero_do_not_require_a_price():
    assert valuation.usd_amount('1000000000000000001',18,'1.23456789')=='1.23456789000000000123456789'
    assert valuation.usd_amount('0',None,None)=='0'
    assert valuation.usd_amount('1',None,None) is None
    assert valuation.usd_amount(None,18,'1') is None
    assert valuation.usd_amount(str(2**256-1),0,'1')==str(2**256-1)


def test_cutoff_excludes_open_day_and_preserves_decimals_zero(db):
    seed(db,1,fee='3',decimals=0)
    seed(db,2,timestamp=CUTOFF)
    target=(1,ASSET,CUTOFF-1);valuation.save_price(db,target,point(target))
    rows=outputs(db,valuation.run_fee_usd(db,before_timestamp=CUTOFF),'fee_usd_events')
    assert len(rows)==1 and rows[0]['total_fees_paid_usd']=='7.5'
    with pytest.raises(ValueError):valuation.closed_cutoff(CUTOFF+1,now=CUTOFF+86400)
    with pytest.raises(ValueError):valuation.closed_cutoff(CUTOFF+86400,now=CUTOFF)


def test_limit_and_price_only_refresh_leave_accounting_unchanged(db, monkeypatch):
    seed(db,1);seed(db,2,asset=OTHER)
    before=[tuple(r) for r in db.execute('SELECT * FROM canonical_fee_reports')]
    monkeypatch.setattr(pricing,'fetch_yearn_prices_batch',lambda targets:{t:point(t) for t in targets})
    assert valuation.price_fees(db,before_timestamp=CUTOFF,limit=1)['remaining_asset_days']==1
    assert valuation.price_fees(db,before_timestamp=CUTOFF,limit=1)['remaining_asset_days']==0
    monkeypatch.setattr(pricing,'fetch_yearn_prices_batch',lambda targets:{t:point(t,'3') for t in targets})
    assert valuation.price_fees(db,before_timestamp=CUTOFF,refresh=True)['selected_asset_days']==2
    assert [tuple(r) for r in db.execute('SELECT * FROM canonical_fee_reports')]==before
    rows=outputs(db,valuation.run_fee_usd(db,before_timestamp=CUTOFF),'fee_usd_by_family')
    assert rows[0]['known_total_fees_paid_usd_subtotal']=='6'




def test_cli_offline_analysis_and_csv_exports(db,tmp_path):
    seed(db,1)
    path=db.execute('PRAGMA database_list').fetchone()[2]
    assert main(['--db',path,'analyze','fee-usd','--before-timestamp',str(CUTOFF)])==0
    assert main(['--db',path,'export','fee-usd','--out',str(tmp_path/'export')])==0
    assert (tmp_path/'export/fee_usd_events.csv').exists()
    assert len(list((tmp_path/'export').glob('*.csv')))==6
    with pytest.raises(ValueError,match='Yearn Prices only'):
        main(['--db',path,'analyze','fee-usd','--price-source','defillama'])


def test_deferred_manifest_preserves_unknown_and_rejects_stale_scope(db,tmp_path):
    seed(db,1,accepted=False)
    entry={'chain_id':1,'tx':'0x'+f'{1:064x}','log_index':0,'vault':OTHER,'asset':ASSET,
           'asset_decimals':6,'timestamp':DAY+100,'accepted':False,'fee_value_raw':None,
           'disposition':'deferred_execution_evidence','reason':'legacy_execution_unavailable',
           'reference_fee_raw_unverified':'999999999999'}
    path=tmp_path/'deferred.json';path.write_text(json.dumps([entry]))
    run=valuation.run_fee_usd(db,before_timestamp=CUTOFF,deferred_manifest=path)
    row=outputs(db,run,'fee_usd_events')[0]
    assert row['accounting_status']=='deferred' and row['total_fees_paid_usd'] is None
    assert outputs(db,run,'fee_usd_by_family')[0]['deferred_accounting_events']==1
    path.write_text(json.dumps([{**entry,'vault':ASSET}]))
    with pytest.raises(ValueError,match='conflicts'):valuation.run_fee_usd(db,before_timestamp=CUTOFF,deferred_manifest=path)


def test_zero_fee_still_prices_nonzero_gain_and_tracks_component_gaps(db,monkeypatch):
    seed(db,1,fee='0',gain='1000000')
    calls=[]
    monkeypatch.setattr(pricing,'fetch_yearn_prices_batch',lambda targets:calls.append(targets) or {t:point(t) for t in targets})
    valuation.price_fees(db,before_timestamp=CUTOFF)
    assert len(calls)==1
    row=outputs(db,valuation.run_fee_usd(db,before_timestamp=CUTOFF),'fee_usd_events')[0]
    assert row['total_fees_paid_usd']=='0' and row['gross_gain_usd']=='2.5'


def test_invalid_endpoint_evidence_cannot_enter_usd_totals(db, monkeypatch):
    # The pricing adapter tests each malformed response. Here check that its
    # rejection reaches the database and exports without becoming a USD amount.
    seed(db, 1)
    monkeypatch.setenv('YEARN_PRICE_PROD_KEY', 'test-only')
    raw = {'timestamp': CUTOFF-1-86400, 'price': 2.5, 'source': 'oracle'}
    monkeypatch.setattr(pricing, '_yearn_prices_get',
                        lambda *a, **kw: {'coins': {'ethereum:'+ASSET: {'prices': [raw]}}})
    result = valuation.price_fees(db, before_timestamp=CUTOFF)
    assert result['results'] == {'invalid': 1}
    row = outputs(db, valuation.run_fee_usd(db, before_timestamp=CUTOFF), 'fee_usd_events')[0]
    assert row['total_fees_paid_usd'] is None and row['valuation_status'] == 'invalid'



def test_sync_target_scope_and_retry_order_do_not_starve_new_days(db, monkeypatch):
    seed(db, 1)
    seed(db, 2, asset=OTHER)
    first = (1, ASSET, CUTOFF-1)
    second = (1, OTHER, CUTOFF-1)
    valuation.save_price(db, first, (None, 'missing', {'provider':'yearn-prices'}))
    db.commit()
    calls = []
    monkeypatch.setattr(pricing, 'fetch_yearn_prices_batch', lambda targets: calls.extend(targets) or {t:point(t) for t in targets})
    valuation.price_fees(db, before_timestamp=CUTOFF, retry_missing=True, limit=1)
    assert calls == [second]
    calls.clear()
    valuation.price_fees(db, before_timestamp=CUTOFF, retry_missing=True, targets={second})
    assert calls == []
