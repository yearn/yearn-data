import copy
import json
from pathlib import Path
import re

import pytest

from yearn_data.fees import run_canonical_fees
from yearn_data.storage import connect, init_db
from yearn_data.tokenized_fees import TOPICS, decode_tokenized_fee, index_tokenized_fees

ROOT=Path(__file__).parent/'fixtures/fees'
CASES=[c for c in json.loads((ROOT/'cases.json').read_text()) if c['event']['contract_family']=='yearn-v3-tokenized-strategy']
INVENTORY={(v['chain_id'],v['address']):v for v in json.loads((ROOT/'inventory.json').read_text())}
EXPECTED={e['event_id']:e['accounting'] for e in json.loads((ROOT/'expected.json').read_text())}


def inputs(case):
    event=case['event']
    vault=INVENTORY[event['chain_id'],event['vault_address']]
    header={'number':event['block_number'],'hash':event['block_hash'],'timestamp':event['block_timestamp']}
    return vault,copy.deepcopy(case['raw_log']),header


@pytest.mark.parametrize('case',CASES,ids=lambda c:str(c['event']['id']))
def test_real_tokenized_accounting_matches_pinned_reference(case):
    result=decode_tokenized_fee(*inputs(case))
    expected={re.sub(r'(?<!^)(?=[A-Z])','_',k).lower():v for k,v in EXPECTED[case['event']['id']].items()}
    assert result['accounting']==expected
    assert result['event_name']=='Reported'


def test_accrued_uses_same_components_but_preserves_event_identity():
    vault,log,header=inputs(CASES[1])
    log['topics']=[next(k for k,v in TOPICS.items() if v=='Accrued')]
    result=decode_tokenized_fee(vault,log,header)
    assert result['event_name']=='Accrued'
    assert result['accounting']['total_fees_paid_raw']=='1950'
    assert result['accounting']['protocol_fee_raw']=='390'
    assert result['accounting']['performance_fee_raw']=='1560'
    for bad in ({**log,'removed':True},{**log,'data':'0x00'},{**log,'blockHash':'0x00'}):
        with pytest.raises(ValueError):decode_tokenized_fee(vault,bad,header)


@pytest.fixture
def db(tmp_path):
    c=connect(tmp_path/'fees.sqlite');init_db(c)
    yield c
    c.close()


class Client:
    def __init__(self,case):
        self.vault,self.log,self.header=inputs(case)
        self.eth=self
        self.chain_id=self.vault['chain_id']
        self.scans=0
    def get_block(self,number):
        return self.header
    def get_logs(self,query):
        self.scans+=1
        return [self.log,self.log]  # Duplicate provider delivery must not duplicate revenue.


def test_bounded_ingestion_resume_and_separate_aggregation(db):
    client=Client(CASES[1]);v=client.vault;h=client.header
    args=(db,[v],'polygon',h['number'],h['number'],h['timestamp']+1)
    assert index_tokenized_fees(*args,client=client)['events']==1
    assert index_tokenized_fees(*args,client=client)['reused_chunks']==1
    assert client.scans==1
    assert db.execute('SELECT count(*) FROM strategy_reports').fetchone()[0]==0
    run_id=run_canonical_fees(db)
    rows=[(r['name'],json.loads(r['row_json'])) for r in db.execute('SELECT * FROM analysis_outputs WHERE run_id=?',(run_id,))]
    assert not any(name=='canonical_fees_by_asset' for name,row in rows)
    bucket=next(row for name,row in rows if name=='tokenized_fees_by_asset')
    assert bucket['known_fee_subtotal_raw']=='1950' and bucket['known_reports']==1
    assert bucket['aggregation_scope']=='tokenized-strategy-only'
    assert any(name=='tokenized_fee_coverage' for name,row in rows)


def test_invalid_chunk_does_not_advance_and_can_resume(db):
    client=Client(CASES[1]);v=client.vault;h=client.header
    args=(db,[v],'polygon',h['number'],h['number'],h['timestamp']+1)
    valid=client.log['data'];client.log['data']='0x00'
    with pytest.raises(ValueError):index_tokenized_fees(*args,client=client)
    assert db.execute('SELECT count(*) FROM tokenized_fee_ranges').fetchone()[0]==0
    assert db.execute('SELECT count(*) FROM tokenized_fee_events').fetchone()[0]==0
    client.log['data']=valid
    assert index_tokenized_fees(*args,client=client)['events']==1
    with pytest.raises(ValueError,match='exclusive time cutoff'):
        index_tokenized_fees(db,[v],'polygon',h['number'],h['number'],h['timestamp'],client=client)
    with pytest.raises(ValueError,match='max-vaults'):
        index_tokenized_fees(db,[v,{**v,'address':'0x'+'12'*20}],'polygon',h['number'],h['number'],h['timestamp']+1,max_vaults=1,client=client)


def test_tokenized_import_preserves_allocator_and_lifetime_outputs(db):
    import runpy
    from yearn_data.analysis import run_lifetime_yield
    from yearn_data.fees import index_canonical_fees
    helpers=runpy.run_path(str(Path(__file__).parent/'test_canonical_fees.py'))
    allocator=next(c for c in helpers['CASES'] if c['event']['contract_family']=='yearn-v3-allocator')
    helpers['seed'](db,[allocator]);index_canonical_fees(db)
    def outputs(run_id,prefix=''):
        return [tuple(r) for r in db.execute('SELECT name,row_json FROM analysis_outputs WHERE run_id=? AND name LIKE ? ORDER BY name,row_json',(run_id,prefix+'%'))]
    before_lifetime=outputs(run_lifetime_yield(db))
    before_fees=outputs(run_canonical_fees(db),'canonical_')
    client=Client(CASES[1]);h=client.header
    index_tokenized_fees(db,[client.vault],'polygon',h['number'],h['number'],h['timestamp']+1,client=client)
    assert outputs(run_lifetime_yield(db))==before_lifetime
    assert outputs(run_canonical_fees(db),'canonical_')==before_fees


def test_tokenized_cli_uses_bounded_importer(db,tmp_path,monkeypatch):
    import yearn_data.tokenized_fees as tokenized
    from yearn_data.cli import main
    client=Client(CASES[1]);h=client.header
    inventory_path=tmp_path/'inventory.json'
    inventory_path.write_text(json.dumps([client.vault]))
    monkeypatch.setattr(tokenized,'web3_for',lambda chain:client)
    database=db.execute('PRAGMA database_list').fetchone()['file']
    assert main(['--db',database,'index-tokenized-fees','--inventory',str(inventory_path),
                 '--chain','polygon','--from-block',str(h['number']),'--to-block',str(h['number']),
                 '--before-timestamp',str(h['timestamp']+1),'--max-vaults','1'])==0
    assert db.execute('SELECT count(*) FROM tokenized_fee_events').fetchone()[0]==1


def test_overlap_preserves_original_evidence_but_rejects_financial_changes(db):
    client = Client(CASES[1]); header = client.header
    args = (db, [client.vault], 'polygon', header['number'], header['number'], header['timestamp'] + 1)
    index_tokenized_fees(*args, client=client)
    prior = db.execute('SELECT event_json FROM tokenized_fee_events').fetchone()[0]
    # Old acquisition used a different provider JSON representation and classification annotation.
    captured = json.loads(prior)
    captured['raw_log']['transactionIndex'] = '0x00'
    captured['inventory']['review_note'] = 'retained source evidence'
    retained = json.dumps(captured)
    db.execute('UPDATE tokenized_fee_events SET event_json=?', (retained,))
    db.execute('DELETE FROM tokenized_fee_ranges'); db.commit()
    assert index_tokenized_fees(*args, client=client)['events'] == 1
    assert db.execute('SELECT event_json FROM tokenized_fee_events').fetchone()[0] == retained
    db.execute('DELETE FROM tokenized_fee_ranges'); db.commit()
    changed = json.loads(retained)
    changed['accounting']['total_fees_paid_raw'] = '1'
    db.execute('UPDATE tokenized_fee_events SET event_json=?', (json.dumps(changed),)); db.commit()
    with pytest.raises(ValueError, match='conflicts'):
        index_tokenized_fees(*args, client=client)
    assert db.execute('SELECT count(*) FROM tokenized_fee_ranges').fetchone()[0] == 0


def test_explicit_confirmation_policy_is_bounded_and_recorded(db):
    client = Client(CASES[1]); header = client.header
    def get_block(number):
        if number == 'finalized':
            return {**header, 'number': header['number'] - 1000}
        if number == 'latest':
            return {**header, 'number': header['number'] + 1000}
        return header
    client.get_block = get_block
    args = (db, [client.vault], 'polygon', header['number'], header['number'], header['timestamp'] + 1)
    with pytest.raises(ValueError, match='finality'):
        index_tokenized_fees(*args, client=client)
    result = index_tokenized_fees(*args, client=client, confirmations=1000)
    assert result['finality_policy'] == 'latest-minus-1000'
    assert db.execute('SELECT finality_policy FROM tokenized_fee_ranges').fetchone()[0] == 'latest-minus-1000'
    with pytest.raises(ValueError, match='finality'):
        index_tokenized_fees(*args, client=client, confirmations=1001)
    with pytest.raises(ValueError, match='positive'):
        index_tokenized_fees(*args, client=client, confirmations=0)


def test_maintained_inventory_has_unique_valid_classifications():
    from yearn_data.tokenized_fees import load_inventory, validate_inventory
    inventory = load_inventory()
    assert inventory and any(v['is_retired'] for v in inventory)
    assert len(inventory) == len({(v['chain_id'], v['address']) for v in inventory})
    assert all(validate_inventory(v) == v for v in inventory)
