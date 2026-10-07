import importlib.util
import json
import sqlite3
from unittest.mock import Mock

import pytest

spec=importlib.util.spec_from_file_location('backfill','scripts/backfill_tvl.py')
b=importlib.util.module_from_spec(spec);spec.loader.exec_module(b)


def ts(s):return int(b.datetime.strptime(s,'%Y-%m-%d').replace(tzinfo=b.UTC).timestamp())+86399


def test_monthly_ranges_are_complete_disjoint_and_newest_first():
    ranges=list(b.monthly_ranges(ts('2020-02-27'),ts('2020-04-02')))
    assert ranges==[(ts('2020-04-01'),ts('2020-04-02')),(ts('2020-03-01'),ts('2020-03-31')),(ts('2020-02-27'),ts('2020-02-29'))]
    assert sorted(t for lo,hi in ranges for t in range(lo,hi+1,86400))==list(range(ts('2020-02-27'),ts('2020-04-02')+1,86400))


def test_exact_calldata_address_padding():
    assert b.call_data('totalSupply()')=='0x18160ddd'
    arg='0x'+'ab'*20
    assert b.call_data('balanceOf(address)',arg)=='0x70a08231'+'0'*24+'ab'*20


def test_direct_batch_matches_ids_even_when_out_of_order(monkeypatch):
    reader=object.__new__(b.BatchedReader)
    reader.w3=Mock();reader.w3.provider.endpoint_uri='https://rpc.test'
    response=Mock();response.json.return_value=[{'id':1,'result':'0x'+'00'*31+'02'},{'id':0,'result':'0x'+'00'*31+'01'}]
    post=Mock(return_value=response);monkeypatch.setattr(b.requests,'post',post)
    calls=[('0x'+'11'*20,'0x18160ddd'),('0x'+'22'*20,'0x18160ddd')]
    result=reader.transport(calls,123,False)
    assert int.from_bytes(result[calls[0]],'big')==1
    assert int.from_bytes(result[calls[1]],'big')==2
    assert post.call_args.kwargs['json'][0]['params'][1]=='0x7b'


def test_invalid_ids_cannot_become_success(monkeypatch):
    reader=object.__new__(b.BatchedReader);reader.w3=Mock()
    response=Mock();response.json.return_value=[{'id':9,'result':'0x1234'}]
    monkeypatch.setattr(b.requests,'post',Mock(return_value=response));monkeypatch.setattr(b.time,'sleep',lambda _:None)
    calls=[('0x'+'11'*20,'0x18160ddd')]
    assert isinstance(reader.transport(calls,123,False)[calls[0]],Exception)


def test_price_omissions_verified_and_persisted(monkeypatch):
    c=sqlite3.connect(':memory:');c.row_factory=sqlite3.Row
    c.execute('CREATE TABLE prices(chain_id,token_address,timestamp,block_number,source,price_usd,status,raw_json)')
    key=(1,'0x'+'11'*20,ts('2020-03-01'))
    monkeypatch.setattr(b,'fetch_yearn_prices_batch',lambda *_a,**_k:{})
    exact=Mock(return_value=(None,'missing',{'provider':'yearn-prices'}));monkeypatch.setattr(b,'fetch_yearn_price',exact)
    prices=b.Prices(c);prices.prime([key]);assert prices.get(*key)[1]=='missing'
    prices.cache.clear();assert prices.get(*key)[0] is None;assert exact.call_count==1
    assert c.execute('select status from prices').fetchone()[0]=='missing'


def test_birth_hint_checked_against_previous_block():
    r=Mock();r.head={'number':10}
    r.exists.side_effect=lambda _a,n:n>=4
    r.w3.eth.get_block.return_value={'timestamp':123}
    assert b.deployment(r,{'address':'0xabc','deployment_block':7})==(4,123)


def test_atomic_status_is_valid_json(tmp_path):
    p=tmp_path/'status.json';b.atomic_json(p,{'phase':'running','snapshots':7})
    assert json.loads(p.read_text())['snapshots']==7
    assert not p.with_suffix('.tmp').exists()
