"""Selective, bounded verification of observed Vyper fee-mint arithmetic."""
from eth_utils import keccak

TRANSFER = keccak(text='Transfer(address,address,uint256)').hex()


def extract_mint_operand(ops, minted_shares):
    """Identify the multiply/divide/supply-write sequence behind a known receipt mint.

    This recognizes the observed old Vyper instruction pattern only. It fails
    closed on absent or ambiguous matches and does not establish generic support
    for other bytecode/compiler versions. Input traces must be scoped to the
    correct vault/transaction and matched to a successful receipt by the caller.
    """
    minted = int(minted_shares)
    if minted <= 0:
        raise ValueError('positive receipt mint required')
    matches = []
    for i in range(len(ops)-5):
        window=ops[i:i+6]
        if [o['op'] for o in window] != ['MUL','DIV','DIV','SSTORE','SSTORE','LOG3']:
            continue
        mul,check,div,supply_write,balance_write,event=window
        if len({o['depth'] for o in window})!=1:
            continue
        amount,supply=int(mul['a'],16),int(mul['b'],16)
        numerator,assets=int(div['a'],16),int(div['b'],16)
        if not amount or not supply or not assets:
            continue
        if (amount*supply != numerator or numerator//assets != minted
            or int(check['a'],16)!=numerator or int(check['b'],16)!=amount
            or int(supply_write['a'],16)!=5 or int(supply_write['b'],16)!=supply+minted
            or int(balance_write['b'],16)!=minted or event['topic'].removeprefix('0x')!=TRANSFER
            or int(event['data'],16)!=minted):
            continue
        matches.append({'fee_operand_raw':str(amount),'supply_before_mint_raw':str(supply),
                        'valuation_assets_at_mint_raw':str(assets),'minted_shares_raw':str(minted),
                        'mul_pc':mul['pc'],'div_pc':div['pc'],'trace_window_start':i})
    if len(matches)!=1:
        raise ValueError('missing or ambiguous observed mint arithmetic')
    return matches[0]


def extract_precision_mint_operand(ops, minted_shares):
    """Recognize the piloted 0.3.5 precision-factor mint arithmetic."""
    minted = int(minted_shares)
    if minted < 0:raise ValueError("negative mint")
    pattern = ['MUL', 'DIV', 'MUL', 'DIV', 'DIV', 'DIV', 'SSTORE', 'SSTORE', 'LOG3']
    if len(ops) != 9 or [o['op'] for o in ops] != pattern or len({o['depth'] for o in ops}) != 1:
        raise ValueError('unrecognized precision-factor mint arithmetic')
    first, check1, second, check2, divide, precision_divide, supply_write, balance_write, event = ops
    def number(op, key):
        return int(op[key], 16)
    precision, amount = number(first, 'a'), number(first, 'b')
    scaled, supply = number(second, 'a'), number(second, 'b')
    numerator, assets = number(divide, 'a'), number(divide, 'b')
    if not all((amount, precision, supply, assets)):
        raise ValueError('invalid precision-factor mint inputs')
    if (amount * precision != scaled or scaled * supply != numerator
            or number(check1, 'a') != scaled or number(check1, 'b') != precision
            or number(check2, 'a') != numerator or number(check2, 'b') != scaled
            or number(precision_divide, 'a') != numerator // assets
            or number(precision_divide, 'b') != precision
            or numerator // assets // precision != minted
            or number(supply_write, 'a') != 6 or number(supply_write, 'b') != supply + minted
            or number(balance_write, 'b') != minted
            or event['topic'].removeprefix('0x') != TRANSFER or int(event['data'], 16) != minted):
        raise ValueError('inconsistent precision-factor mint arithmetic')
    return {'fee_operand_raw': str(amount), 'precision_factor_raw': str(precision),
            'supply_before_mint_raw': str(supply), 'valuation_assets_at_mint_raw': str(assets),
            'minted_shares_raw': str(minted), 'mul_pc': first['pc'], 'div_pc': divide['pc']}


def extract_interleaved_mint_operand(ops, minted_shares):
    """Bind mint multiplication across the observed 0.4.3 free-funds calculation."""
    minted = int(minted_shares)
    if minted <= 0 or len(ops) < 6:
        raise ValueError('invalid interleaved mint')
    divide, supply_write, balance_write, event = ops[-4:]
    if [o['op'] for o in ops[-4:]] != ['DIV', 'SSTORE', 'SSTORE', 'LOG3']:
        raise ValueError('unrecognized interleaved mint tail')
    numerator, assets = int(divide['a'], 16), int(divide['b'], 16)
    supply = int(supply_write['b'], 16) - minted
    if (assets <= 0 or supply <= 0 or numerator // assets != minted
            or int(supply_write['a'], 16) != 5 or int(balance_write['b'], 16) != minted
            or event['topic'].removeprefix('0x') != TRANSFER or int(event['data'], 16) != minted
            or len({o['depth'] for o in ops[-4:]}) != 1):
        raise ValueError('inconsistent interleaved mint tail')
    matches = []
    for mul, check in zip(ops[:-4], ops[1:-4]):
        if mul['op'] != 'MUL' or check['op'] != 'DIV':continue
        amount = int(mul['a'], 16)
        if (amount > 0 and int(mul['b'], 16) == supply and amount * supply == numerator
                and int(check['a'], 16) == numerator and int(check['b'], 16) == amount
                and mul['depth'] == check['depth'] == divide['depth']):
            matches.append({'fee_operand_raw': str(amount), 'supply_before_mint_raw': str(supply),
                            'valuation_assets_at_mint_raw': str(assets), 'minted_shares_raw': str(minted),
                            'mul_pc': mul['pc'], 'div_pc': divide['pc']})
    if len(matches) != 1:raise ValueError('missing or ambiguous interleaved mint multiplication')
    return matches[0]


TRACE_VERSION = 'vyper-fee-mint-1'
# Only releases with actual execution evidence in the bounded investigation.
TRACE_RELEASES = {'0.3.0', '0.4.2'}
EXPLICIT_TRACE_RELEASES = {'0.2.2', '0.3.0', '0.3.1', '0.3.2', '0.3.3', '0.3.5', '0.4.2', '0.4.3'}
# Expanded only for the independently piloted zero-gain fee-mint path.
ZERO_GAIN_TRACE_RELEASES = {'0.2.2', '0.3.0', '0.3.2', '0.3.3'}


def fetch_trace(client, tx_hash, vault):
    return fetch_traces(client, tx_hash, [vault])[vault.lower()]


def fetch_traces(client, tx_hash, vaults):
    """Acquire one transaction trace and retain separate operations per vault."""
    import json
    import requests
    targets = sorted({vault.lower() for vault in vaults})
    if not targets:raise ValueError("empty trace target list")
    target = json.dumps(targets)
    tracer = '''{ops:[],overflow:false,step:function(l,db){
      var address=toHex(l.contract.getAddress()).toLowerCase();
      if(TARGET.indexOf(address)<0)return;
      if(this.ops.length>=10000){this.overflow=true;return;}
      var op=l.op.toString();
      if(op==="MUL"||op==="DIV"||op==="SSTORE"){
        this.ops.push({address:address,op:op,pc:l.getPC(),depth:l.getDepth(),a:l.stack.peek(0).toString(16),b:l.stack.peek(1).toString(16)});
      }else if(op==="LOG3"||op==="LOG2"){
        var off=parseInt(l.stack.peek(0).toString(10));var size=parseInt(l.stack.peek(1).toString(10));
        if(size>1024){this.overflow=true;return;}
        this.ops.push({address:address,op:op,pc:l.getPC(),depth:l.getDepth(),topic:l.stack.peek(2).toString(16),data:toHex(l.memory.slice(off,off+size))});
      }},fault:function(l,db){},result:function(ctx,db){return {ops:this.ops,error:ctx.error,overflow:this.overflow};}}'''.replace('TARGET', target)
    response = requests.post(client.provider.endpoint_uri, json={'jsonrpc':'2.0','id':1,
        'method':'debug_traceTransaction','params':[tx_hash,{'tracer':tracer,'timeout':'25s'}]}, timeout=30)
    response.raise_for_status()
    body = response.json()
    if 'error' in body or not isinstance(body.get('result'),dict):
        raise ValueError('trace unavailable')
    result = body['result']
    grouped = {vault: {'ops': [], 'error': result.get('error'), 'overflow': result.get('overflow')} for vault in targets}
    for op in result['ops']:
        value = dict(op);address = value.pop('address')
        if address not in grouped:raise ValueError('unexpected trace address')
        grouped[address]['ops'].append(value)
    return grouped


def verify_receipt_mint(trace, receipt, vault, mint_index, minted_shares):
    """Bind filtered execution logs to the complete receipt before extracting a fee."""
    from .fees import _hex, _number
    if trace.get('error') or trace.get('overflow') or len(trace['ops']) > 10000:
        raise ValueError('incomplete trace')
    replay = trace.get('local_replay')
    if replay is not None:
        if (replay.get('method') != 'local-prestate-replay'
                or replay.get('receipt_logs_equal') is not True or replay.get('opera_gas_used_equal') is not True
                or replay.get('block_number') != _number(receipt['blockNumber'])
                or replay.get('gas_used_raw') != str(_number(receipt['gasUsed']))):
            raise ValueError('unverified local replay')
    logs = sorted((l for l in receipt['logs'] if l['address'].lower()==vault.lower()
                   and len(l['topics']) in (2,3)), key=lambda l:_number(l['logIndex']))
    traced = [(i,o) for i,o in enumerate(trace['ops']) if o['op'] in ('LOG2','LOG3')]
    if len(logs)!=len(traced):
        raise ValueError('trace logs differ from receipt')
    target = None
    for log,(i,op) in zip(logs,traced):
        if (op['op'] != 'LOG'+str(len(log['topics'])) or
            int(op['topic'],16) != int(_hex(log['topics'][0]),16) or op['data'].lower()!=_hex(log['data'])):
            raise ValueError('trace logs differ from receipt')
        if _number(log['logIndex'])==mint_index:
            target=i
    if target is None or target<5:
        raise ValueError('receipt mint absent from trace')
    window = trace['ops'][max(0, target-8):target+1]
    if (len(window) == 9 and window[-3]['op'] == 'SSTORE' and int(window[-3]['a'], 16) == 6):
        return extract_precision_mint_operand(window, minted_shares)
    try:
        return extract_mint_operand(trace['ops'][target-5:target+1], minted_shares)
    except ValueError:
        previous = max((i for i, _ in traced if i < target), default=-1)
        return extract_interleaved_mint_operand(trace['ops'][previous+1:target+1], minted_shares)


class SelectiveFeeVerifier:
    """Per-run bounded trace budget and per-block log reuse; successful traces persist."""
    def __init__(self, conn, limit, client_for, block_logs_fetcher=None, trace_fetcher=None):
        if limit<=0:
            raise ValueError('trace limit must be positive')
        self.conn,self.limit,self.client_for=conn,limit,client_for
        self.block_logs_fetcher,self.trace_fetcher=block_logs_fetcher,trace_fetcher
        self.attempts=0
        self.logs={}
        self.traces={}

    def verify(self, report, receipt, shares, *, explicit=False):
        import json
        from .fees import _hex, _number
        from eth_utils import to_checksum_address
        direct_zero = 'gain_raw' in report.keys() and int(report['gain_raw']) == 0
        supported = EXPLICIT_TRACE_RELEASES if explicit else (ZERO_GAIN_TRACE_RELEASES if direct_zero else TRACE_RELEASES)
        if report['api_version'] not in supported:
            return {'status':'not_selected','reason':'unsupported_trace_release'}
        chain,vault,tx=report['chain_id'],report['vault_address'],report['tx_hash']
        block_hash=_hex(receipt['blockHash'])
        block_key=(chain,vault,block_hash)
        if not direct_zero and not explicit:
            try:
                if block_key not in self.logs:
                    self.logs[block_key]=None
                    cached=self.conn.execute('SELECT logs_json FROM fee_block_log_evidence WHERE chain_id=? AND vault_address=? AND block_hash=?',block_key).fetchone()
                    if cached:
                        try:
                            value=json.loads(cached[0])
                            if isinstance(value,list) and all(l['address'].lower()==vault.lower() and _hex(l['blockHash'])==block_hash for l in value):
                                self.logs[block_key]=value
                        except (ValueError,KeyError,TypeError):
                            pass
                    if self.logs[block_key] is None:
                        self.logs[block_key]=(self.block_logs_fetcher(chain,vault,block_hash) if self.block_logs_fetcher else
                            self.client_for(chain).eth.get_logs({'blockHash':block_hash,'address':to_checksum_address(vault)}))
                logs=self.logs[block_key]
                if logs is None:
                    raise ValueError('block logs unavailable')
                if any(l['address'].lower()!=vault.lower() or _hex(l['blockHash'])!=block_hash for l in logs):
                    raise ValueError('wrong block logs')
                from web3 import Web3
                self.conn.execute('INSERT OR REPLACE INTO fee_block_log_evidence VALUES (?,?,?,?)',(*block_key,Web3.to_json(logs)))
                if not any(_number(l['logIndex'])>report['log_index'] for l in logs):
                    return {'status':'not_selected','reason':'no_later_vault_logs_detected'}
            except Exception:
                return {'status':'unavailable','reason':'block_activity_scan_failed'}
        key=(chain,tx,vault,block_hash,TRACE_VERSION)
        try:
            if key not in self.traces:
                row=self.conn.execute('''SELECT trace_json FROM fee_execution_traces WHERE
                    chain_id=? AND tx_hash=? AND vault_address=? AND block_hash=? AND trace_version=?''',key).fetchone()
                if row:
                    try:
                        cached_trace=json.loads(row[0])
                        verify_receipt_mint(cached_trace,receipt,vault,shares['mint_log_index'],shares['minted_shares_raw'])
                        self.traces[key]=cached_trace
                    except (ValueError,KeyError,TypeError):
                        row=None
                if row is None:
                    if self.attempts>=self.limit:
                        return {'status':'deferred','reason':'trace_budget_exhausted'}
                    self.attempts+=1
                    self.traces[key]=None
                    trace=(self.trace_fetcher(chain,tx,vault) if self.trace_fetcher else fetch_trace(self.client_for(chain),tx,vault))
                    # Only persist evidence after validating it against this receipt.
                    verify_receipt_mint(trace,receipt,vault,shares['mint_log_index'],shares['minted_shares_raw'])
                    self.conn.execute('INSERT OR REPLACE INTO fee_execution_traces VALUES (?,?,?,?,?,?)',(*key,json.dumps(trace)))
                    self.traces[key]=trace
            trace=self.traces[key]
            if trace is None:
                raise ValueError('trace unavailable')
            result=verify_receipt_mint(trace,receipt,vault,shares['mint_log_index'],shares['minted_shares_raw'])
            return {'status':'verified','trace_version':TRACE_VERSION,'block_hash':block_hash,
                    'trigger':'explicit_report_selection' if explicit else ('zero_gain_fee_mint' if direct_zero else 'later_vault_logs'),'mint_log_index':shares['mint_log_index'],
                    **({'execution_source':'local-prestate-replay','replay':trace['local_replay']} if 'local_replay' in trace else {}),**result}
        except Exception:
            return {'status':'unavailable','reason':'trace_unavailable_or_unrecognized'}


def verify_cached_direct_execution(conn, report, evidence):
    """Revalidate trace-only accounting from retained receipt and execution evidence."""
    import json
    from .fees import _number, _hex, decode_v2_receipt
    from .fee_reconstruction import extract_fee_shares
    if evidence.get('direct_execution_asset') != {'asset':report['asset'],'asset_decimals':report['asset_decimals']}:
        raise ValueError('execution asset binding changed')
    cached=conn.execute('SELECT receipt_json FROM fee_receipt_evidence WHERE chain_id=? AND tx_hash=?',
                        (report['chain_id'],report['tx_hash'])).fetchone()
    if not cached:raise ValueError('retained execution receipt missing')
    receipt=json.loads(cached[0])
    if (_hex(receipt['transactionHash'])!=report['tx_hash'].lower() or _number(receipt['status'])!=1 or
            _hex(receipt['blockHash'])!=evidence['block_hash'] or _number(receipt['blockNumber'])!=report['block_number']):
        raise ValueError('retained receipt identity changed')
    decoded=decode_v2_receipt(receipt,report['vault_address']).get(report['log_index'])
    if (not decoded or decoded['gain']!=int(report['gain_raw']) or decoded['loss']!=int(report['loss_raw']) or
            decoded['strategy'].lower()!=report['strategy_address'].lower() or decoded['fee'] is not None):
        raise ValueError('execution report changed')
    shares=extract_fee_shares(receipt['logs'],report['vault_address'],report['log_index'])
    explicit = evidence.get('execution_selection') == 'explicit_report_keys'
    if not explicit and decoded['gain'] != 0:raise ValueError('execution selection changed')
    if shares!=evidence['fee_shares'] or shares['status'] not in ('found', 'zero-share-mint'):raise ValueError('execution mint binding changed')
    def offline(*args):raise ValueError('execution evidence missing; offline replay cannot fetch')
    result=SelectiveFeeVerifier(conn,1,offline,offline,offline).verify(report,receipt,shares,explicit=explicit)
    if result['status']!='verified':raise ValueError('cached execution verification failed')
    return result
