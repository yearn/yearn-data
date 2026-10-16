"""Observed Tokenized Strategy fees, separate from allocator P&L and revenue."""
from __future__ import annotations

import hashlib
import json
import re
from importlib.resources import files

from eth_abi import decode
from eth_utils import keccak, to_checksum_address
from web3 import Web3

from .analysis import write_output
from .chains import web3_for
from .config import CHAINS
from .fees import _amounts, _hex, _number, unsigned

FAMILY = 'yearn-v3-tokenized-strategy'
TOPICS = {'0x'+keccak(text=f'{name}(uint256,uint256,uint256,uint256)').hex(): name
          for name in ('Reported','Accrued')}


def load_inventory():
    """Read the maintained classification snapshot; membership is not exhaustive."""
    payload = json.loads(files('yearn_data').joinpath('inventories/tokenized-inventory.json').read_text())
    inventory = [validate_inventory(v) for v in payload['tokenized_inventory']]
    if len({(v['chain_id'], v['address']) for v in inventory}) != len(inventory):
        raise ValueError('duplicate maintained inventory')
    return inventory


def comparable_event(event):
    """Compare financial identity while preserving the originally captured evidence.

    Provider JSON shapes and extra classification metadata may differ on overlap.
    Changes to block identity, event kind, asset, amounts or components may not.
    """
    fields = ('chain_id', 'vault_address', 'contract_family', 'api_version', 'asset',
              'asset_decimals', 'tx_hash', 'log_index', 'block_number', 'block_hash',
              'block_timestamp', 'event_name', 'accounting')
    result = {key: event[key] for key in fields}
    for key in ('vault_address', 'asset', 'tx_hash', 'block_hash'):
        result[key] = result[key].lower()
    return result


def validate_inventory(vault):
    if vault['contract_family'] != FAMILY or vault['classification_status'] != 'complete':
        raise ValueError('complete Tokenized Strategy classification required')
    for field in ('address','asset_address'):
        if not re.fullmatch(r'0x[0-9a-fA-F]{40}',vault[field]):
            raise ValueError(f'invalid {field}')
    decimals=unsigned(vault['asset_decimals'])
    if decimals>255 or not vault.get('api_version') or not vault.get('classification_source'):
        raise ValueError('missing or invalid asset/release provenance')
    unsigned(vault['deployment_block'])
    return {**vault,'address':vault['address'].lower(),'asset_address':vault['asset_address'].lower(),
            'asset_decimals':decimals}


def decode_tokenized_fee(vault, log, header):
    vault=validate_inventory(vault)
    if log.get('removed') or log['address'].lower()!=vault['address']:
        raise ValueError('removed log or wrong emitter')
    if len(log['topics'])!=1 or _hex(log['topics'][0]) not in TOPICS:
        raise ValueError('unexpected Tokenized event signature')
    if _hex(log['blockHash'])!=_hex(header['hash']) or _number(log['blockNumber'])!=_number(header['number']):
        raise ValueError('log/header mismatch')
    for value in (log['transactionHash'],log['blockHash']):
        if not re.fullmatch(r'0x[0-9a-f]{64}',_hex(value)):
            raise ValueError('invalid transaction/block hash')
    for value in (log['logIndex'],header['number'],header['timestamp']):
        unsigned(_number(value))
    data=bytes.fromhex(_hex(log['data'])[2:])
    if len(data)!=128:
        raise ValueError('unexpected Tokenized event data length')
    profit,loss,protocol,performance=decode(['uint256']*4,data)
    amounts={**_amounts(profit,loss,protocol+performance),
             'protocol_fee_raw':str(protocol),'performance_fee_raw':str(performance)}
    return {'chain_id':vault['chain_id'],'vault_address':vault['address'],
            'contract_family':FAMILY,'api_version':vault['api_version'],
            'asset':vault['asset_address'],'asset_decimals':vault['asset_decimals'],
            'tx_hash':_hex(log['transactionHash']),'log_index':_number(log['logIndex']),
            'block_number':_number(header['number']),'block_hash':_hex(header['hash']),
            'block_timestamp':_number(header['timestamp']),'method_version':'tokenized-fees-1','event_name':TOPICS[_hex(log['topics'][0])],
            'accounting':amounts,'raw_log':json.loads(Web3.to_json(log)),
            'inventory':vault}


def index_tokenized_fees(conn, inventory, chain, from_block, to_block, before_timestamp,
                         chunk_size=2000, max_vaults=10, client=None, confirmations=None):
    """Import explicit finalized ranges; commit each complete vault/chunk atomically."""
    if min(from_block,to_block)<0 or to_block<from_block or min(chunk_size,max_vaults,before_timestamp)<=0:
        raise ValueError('invalid bounded Tokenized indexing range')
    chain_id=CHAINS[chain].chain_id
    vaults=[validate_inventory(v) for v in inventory if v['chain_id']==chain_id and v['contract_family']==FAMILY]
    if len({v['address'] for v in vaults})!=len(vaults):
        raise ValueError('duplicate inventory addresses')
    if not vaults:
        raise ValueError('no classified Tokenized Strategies selected')
    if len(vaults)>max_vaults:
        raise ValueError('inventory exceeds max-vaults; select a smaller inventory or raise the bound explicitly')
    client=client or web3_for(chain)
    if client.eth.chain_id!=chain_id:
        raise ValueError('RPC chain mismatch')
    if confirmations is not None and (isinstance(confirmations, bool) or confirmations <= 0):
        raise ValueError('confirmations must be a positive block count')
    policy = 'finalized' if confirmations is None else f'latest-minus-{confirmations}'
    head = client.eth.get_block('finalized' if confirmations is None else 'latest')
    boundary = _number(head['number']) - (confirmations or 0)
    if to_block > boundary:
        raise ValueError('requested range extends beyond selected finality boundary')
    end=client.eth.get_block(to_block)
    if _number(end['timestamp'])>=before_timestamp:
        raise ValueError('requested range extends beyond exclusive time cutoff')
    counts={'vaults':len(vaults),'completed_chunks':0,'reused_chunks':0,'events':0,'finality_policy':policy,'finality_boundary':boundary}
    headers={to_block:end}
    for vault in vaults:
        fingerprint=hashlib.sha256(json.dumps(vault,sort_keys=True).encode()).hexdigest()
        start=max(from_block,int(vault['deployment_block']))
        for first in range(start,to_block+1,chunk_size):
            last=min(first+chunk_size-1,to_block)
            key=(chain_id,vault['address'],first,last,before_timestamp,fingerprint)
            if conn.execute('SELECT 1 FROM tokenized_fee_ranges WHERE chain_id=? AND vault_address=? AND from_block=? AND to_block=? AND before_timestamp=? AND inventory_hash=? AND finality_policy=?',(*key,policy)).fetchone():
                counts['reused_chunks']+=1
                continue
            logs=client.eth.get_logs({'address':to_checksum_address(vault['address']),
                'fromBlock':first,'toBlock':last,'topics':[list(TOPICS)]})
            events={}
            for log in logs:
                number=_number(log['blockNumber'])
                if not first<=number<=last:
                    raise ValueError('provider returned log outside requested range')
                if number not in headers:headers[number]=client.eth.get_block(number)
                event=decode_tokenized_fee(vault,log,headers[number])
                if event['block_timestamp']>=before_timestamp:
                    raise ValueError('event outside closed-day scope')
                identity=(chain_id,event['tx_hash'],event['log_index'])
                encoded=json.dumps(event,sort_keys=True)
                if identity in events and comparable_event(json.loads(events[identity])) != comparable_event(event):
                    raise ValueError('conflicting duplicate log identity')
                events[identity]=encoded
            # Failures leave no successful range marker and no partial chunk.
            with conn:
                for identity,encoded in events.items():
                    prior=conn.execute('SELECT event_json FROM tokenized_fee_events WHERE chain_id=? AND tx_hash=? AND log_index=?',identity).fetchone()
                    if prior and comparable_event(json.loads(prior[0])) != comparable_event(json.loads(encoded)):
                        raise ValueError('stored Tokenized event conflicts with acquired evidence')
                    conn.execute('INSERT INTO tokenized_fee_events VALUES (?,?,?,?) ON CONFLICT DO NOTHING',(*identity,encoded))
                conn.execute('INSERT INTO tokenized_fee_ranges (chain_id,vault_address,from_block,to_block,before_timestamp,inventory_hash,finality_policy) VALUES (?,?,?,?,?,?,?) ON CONFLICT(chain_id,vault_address,from_block,to_block,before_timestamp,inventory_hash) DO UPDATE SET finality_policy=excluded.finality_policy',(*key,policy))
            counts['completed_chunks']+=1;counts['events']+=len(events)
    return counts


def write_tokenized_outputs(conn, run_id):
    """Separate output names prevent adding nested strategy fees to allocator totals."""
    totals={}
    for row in conn.execute('SELECT event_json FROM tokenized_fee_events ORDER BY chain_id,tx_hash,log_index'):
        event=json.loads(row[0]);amounts=event['accounting']
        output={k:v for k,v in event.items() if k not in ('accounting','raw_log','inventory')}
        output.update(amounts)
        output.pop('nominal_components')
        output.update(acceptance_status='accepted',aggregation_scope='tokenized-strategy-only',
                      evidence_json=json.dumps({'raw_log':event['raw_log'],'inventory':event['inventory']}))
        write_output(conn,run_id,'tokenized_fee_events',output)
        key=(event['chain_id'],event['asset'],event['asset_decimals'])
        bucket=totals.setdefault(key,{'chain_id':key[0],'asset':key[1],'asset_decimals':key[2],
            'contract_family':FAMILY,'aggregation_scope':'tokenized-strategy-only',
            'known_reports':0,'zero_fee_reports':0,'known_fee_subtotal_raw':0})
        bucket['known_reports']+=1;bucket['zero_fee_reports']+=amounts['total_fees_paid_raw']=='0'
        bucket['known_fee_subtotal_raw']+=int(amounts['total_fees_paid_raw'])
    for bucket in totals.values():
        bucket['known_fee_subtotal_raw']=str(bucket['known_fee_subtotal_raw'])
        write_output(conn,run_id,'tokenized_fees_by_asset',bucket)
    for row in conn.execute('SELECT * FROM tokenized_fee_ranges ORDER BY chain_id,vault_address,from_block'):
        write_output(conn,run_id,'tokenized_fee_coverage',dict(row))
