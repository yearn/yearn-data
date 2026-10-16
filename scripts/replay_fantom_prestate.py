"""Offline Fantom replay from observed transaction prestate, with strict receipt checks.

No fork or public RPC is configured. Full opcode output is temporary; only the
receipt-bound vault operations and replay provenance are retained by callers.
"""
import argparse
from functools import lru_cache
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import time

import requests


def charged_gas(gas_limit, remaining, local_used, fork, refund_counter=None):
    """Opera burns floor(remaining/10) before applying the refund cap.

    Source: Fantom-foundation/go-opera evmcore/state_transition.go. A locally
    capped refund requires the opcode logger's final refund counter. Cross-check
    that counter against the local receipt before applying Opera's cap.
    """
    spent = gas_limit - remaining
    quotient = 5 if fork == 'london' else 2
    if refund_counter is None:
        refund = spent - local_used
        if refund < 0 or refund >= spent // quotient:
            raise ValueError('unknown or capped refund counter')
    else:
        refund = refund_counter
        if not isinstance(refund, int) or refund < 0 or spent-min(refund, spent//quotient) != local_used:
            raise ValueError('refund counter differs from local receipt')
    penalty = remaining // 10
    return spent + penalty - min(refund, (spent + penalty) // quotient)


def creation_frames(steps):
    """Bind successful constructor frames to CREATE's returned address.

    Reverted/failed constructors and CREATE2 remain unsupported. No constructor
    address is guessed from an end-of-block nonce or missing prestate.
    """
    pending = {}; frames = {}
    for index, step in enumerate(steps):
        depth = step['depth']
        for parent in list(pending):
            if depth <= parent:
                if depth != parent or not step['stack'] or int(step['stack'][-1], 16) == 0:
                    raise ValueError('failed or incomplete constructor')
                frames[pending.pop(parent)] = '0x'+step['stack'][-1].removeprefix('0x').zfill(40)[-40:]
        if index and depth > steps[index-1]['depth'] and steps[index-1]['op'] == 'CREATE':
            if depth != steps[index-1]['depth']+1:
                raise ValueError('unsupported constructor depth')
            pending[depth-1] = index
        if step['op'] == 'CREATE' and (index+1 == len(steps) or steps[index+1]['depth'] != depth+1):
            raise ValueError('constructor execution missing')
    if pending: raise ValueError('incomplete constructor')
    return frames


def compact_trace(steps, transaction_to, vaults, prestate, receipt):
    targets = {v.lower() for v in vaults}
    grouped = {v: {'ops': [], 'error': None, 'overflow': False} for v in targets}
    frames = {1: transaction_to.lower()}; previous = None; written = set()
    storage = {a: {int(k,16) for k in s.get('storage',{})} for a,s in prestate.items()}
    logs = {v: [l for l in receipt['logs'] if l['address'].lower()==v and len(l['topics']) in (2,3)] for v in targets}
    positions = {v: 0 for v in targets}
    constructors = creation_frames(steps); created = set()
    for index, step in enumerate(steps):
        depth = step['depth']; stack = step['stack']; op = step['op']
        if op in ('BLOCKHASH', 'CREATE2', 'SELFDESTRUCT'):
            raise ValueError('unsupported replay opcode')
        if previous and depth > previous['depth']:
            if depth != previous['depth'] + 1 or previous['op'] not in ('CALL', 'STATICCALL', 'DELEGATECALL', 'CALLCODE', 'CREATE'):
                raise ValueError('unsupported call frame')
            if previous['op'] == 'CREATE':
                address = constructors[index].lower()
                state = prestate.get(address, {})
                if (state.get('nonce', 0) or state.get('code', '0x') != '0x'
                        or any(int(v, 16) for v in state.get('storage', {}).values()) or address in created):
                    raise ValueError('constructor prestate is not empty')
                created.add(address); frames[depth] = address
            else:
                address = '0x' + previous['stack'][-2].removeprefix('0x').zfill(40)[-40:]
                frames[depth] = frames[depth-1] if previous['op'] in ('DELEGATECALL', 'CALLCODE') else address.lower()
        address = frames[depth]
        if op == 'SLOAD':
            slot = int(stack[-1], 16)
            known = storage.get(address,set())
            if slot not in known and (address, slot) not in written and address not in created:
                raise ValueError('prestate missing accessed storage')
        elif op == 'SSTORE':
            written.add((address, int(stack[-1], 16)))
        if address in targets:
            value = {'op': op, 'pc': step['pc'], 'depth': depth}
            if op in ('MUL', 'DIV', 'SSTORE'):
                value.update(a=stack[-1], b=stack[-2]); grouped[address]['ops'].append(value)
            elif op in ('LOG2', 'LOG3'):
                off, size = int(stack[-1], 16), int(stack[-2], 16)
                position = positions[address]
                if position >= len(logs[address]): raise ValueError('extra replay log')
                log = logs[address][position]; positions[address] += 1
                topics = ['0x'+stack[-3-i].removeprefix('0x').zfill(64) for i in range(int(op[-1]))]
                if topics != log['topics'] or size*2 != len(log['data'])-2 or size>1024:
                    raise ValueError('replay log topics or size differ')
                # Data comes from the actual local receipt, already compared in full
                # with the chain receipt. Avoid repeating memory at every opcode.
                value.update(topic=stack[-3], data=log['data']); grouped[address]['ops'].append(value)
            if len(grouped[address]['ops']) > 10000:
                raise ValueError('compact trace limit')
        previous = step
    if any(positions[v]!=len(logs[v]) for v in targets): raise ValueError('missing replay logs')
    return grouped


@lru_cache(maxsize=1)
def binary_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def correct_sender_prestate(prestate, tx, block, proof):
    """Recover an inconsistent sender balance from hash-bound parent evidence.

    Only an untouched EOA is eligible: every earlier transaction's full call
    tree must exclude it, its nonce must match, and it cannot be the coinbase.
    Original provider prestate remains retained separately.
    """
    sender = tx['from'].lower()
    parent = {'blockHash': block['parentHash'], 'requireCanonical': True}
    def result(key, method, params):
        evidence = proof[key]; request = evidence['request']; response = evidence['response']
        if (request['method'] != method or request['params'] != params
                or request['id'] != response['id'] or 'error' in response):
            raise ValueError('sender proof request binding differs')
        return response['result']
    balance = int(result('balance', 'eth_getBalance', [tx['from'], parent]), 16)
    nonce = int(result('nonce', 'eth_getTransactionCount', [tx['from'], parent]), 16)
    traces = result('traces', 'debug_traceBlockByHash', [block['hash'], {'tracer': 'callTracer'}])
    if [t.get('txHash', '').lower() for t in traces] != [h.lower() for h in block['transactions']]:
        raise ValueError('sender proof block transactions differ')
    index = int(tx['transactionIndex'], 16)
    if block['transactions'][index].lower() != tx['hash'].lower():
        raise ValueError('sender proof transaction position differs')
    current = traces[index]['result']
    if any(current[k].lower() != tx[k].lower() for k in ('from', 'to', 'input', 'value')):
        raise ValueError('sender proof transaction differs')
    state = prestate[sender]
    if (state.get('code', '0x') != '0x' or nonce != int(tx['nonce'], 16)
            or nonce != int(state['nonce']) or sender == block['miner'].lower()):
        raise ValueError('sender proof account is not an untouched EOA')
    def check(call):
        if (call.get('type') not in ('CALL', 'STATICCALL', 'DELEGATECALL', 'CALLCODE', 'CREATE', 'CREATE2', 'SELFDESTRUCT')
                or not call.get('from') or not call.get('to')
                or sender in (call['from'].lower(), call['to'].lower())):
            raise ValueError('sender touched by earlier transaction or incomplete call tree')
        for child in call.get('calls', []): check(child)
    for trace in traces[:index]: check(trace['result'])
    before = int(state['balance'], 16)
    upfront = int(tx['gas'], 16)*int(tx['gasPrice'], 16)+int(tx['value'], 16)
    if not before < upfront <= balance:
        raise ValueError('sender balance proof does not resolve insufficient funding')
    corrected = {**prestate, sender: {**state, 'balance': hex(balance)}}
    return corrected, {'method': 'untouched-sender-parent-state', 'sender': sender,
                       'original_balance_raw': str(before), 'corrected_balance_raw': str(balance),
                       'parent_block_hash': block['parentHash'], 'prior_transactions': index,
                       'proof_sha256': hashlib.sha256(json.dumps(proof, sort_keys=True).encode()).hexdigest()}


def replay(prestate, tx, block, receipt, vaults, fork='istanbul', sender_prestate_proof=None):
    """Return compact operations only after complete log, gas and context validation."""
    if (tx['hash'].lower() != receipt['transactionHash'].lower()
            or tx['blockHash'].lower() != block['hash'].lower()
            or receipt['blockHash'].lower() != block['hash'].lower()
            or int(receipt['status'], 16) != 1 or int(tx.get('type', '0x0'), 16) not in (0, 1, 2)):
        raise ValueError('unsupported transaction or mismatched identity')
    if fork not in ('istanbul', 'berlin', 'london'):
        raise ValueError('unsupported fork')
    if int(tx.get('type', '0x0'), 16) == 2 and fork != 'london':
        raise ValueError('dynamic fee transaction requires London')
    binary = Path(os.environ.get('ANVIL_BINARY') or shutil.which('anvil') or Path.home()/'.foundry/bin/anvil')
    number, timestamp = int(block['number'], 16), int(block['timestamp'], 16)
    config = {'chainId': 250, **{name+'Block': 0 for name in
              ('homestead', 'eip150', 'eip155', 'eip158', 'byzantium', 'constantinople', 'petersburg', 'istanbul')}}
    if fork in ('berlin', 'london'): config['berlinBlock'] = 0
    if fork == 'london': config['londonBlock'] = 0
    prestate = {a.lower(): state for a, state in prestate.items()}
    correction = None
    if sender_prestate_proof is not None:
        prestate, correction = correct_sender_prestate(prestate, tx, block, sender_prestate_proof)
    if int(prestate[tx['from'].lower()]['nonce']) != int(tx['nonce'], 16):
        raise ValueError('sender prestate nonce differs')
    alloc = {address.removeprefix('0x'): {'balance': '0x0', **state} for address, state in prestate.items()}
    genesis = {'config': config, 'alloc': alloc, 'gasLimit': block['gasLimit'],
               'difficulty': block['difficulty'], 'timestamp': hex(timestamp-1), 'number': hex(number-1),
               'coinbase': block['miner']}
    with tempfile.TemporaryDirectory(prefix='fantom-fee-replay-') as tmp:
        root = Path(tmp); (root/'genesis.json').write_text(json.dumps(genesis))
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
        url = f'http://127.0.0.1:{port}'
        def call(method, params):
            response = requests.post(url, json={'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params}, timeout=60)
            response.raise_for_status(); body = response.json()
            if 'error' in body: raise ValueError('local replay RPC failed: '+method)
            return body['result']
        with (root/'anvil.log').open('w') as output:
            process = subprocess.Popen([str(binary), '--host', '127.0.0.1', '--port', str(port),
                '--hardfork', fork, '--steps-tracing', '--chain-id', '250', '--number', str(number-1),
                '--timestamp', str(timestamp-1), '--init', str(root/'genesis.json'),
                '--auto-impersonate', '--silent'], stdout=output, stderr=output)
            try:
                for attempt in range(100):
                    if process.poll() is not None: raise ValueError('local replay process exited')
                    try:
                        call('eth_chainId', []); break
                    except requests.ConnectionError: time.sleep(.05)
                else: raise ValueError('local replay startup timeout')
                call('evm_setNextBlockTimestamp', [timestamp])
                call('anvil_setCoinbase', [block['miner']])
                if fork == 'london': call('anvil_setNextBlockBaseFeePerGas', [block.get('baseFeePerGas', '0x0')])
                send = {key: tx[key] for key in ('from', 'to', 'gas', 'value', 'nonce')}
                if int(tx.get('type', '0x0'), 16) == 2:
                    send.update({key: tx[key] for key in ('maxFeePerGas', 'maxPriorityFeePerGas')})
                else: send['gasPrice'] = tx['gasPrice']
                if 'accessList' in tx: send['accessList'] = tx['accessList']
                send['data'] = tx['input']; local_hash = call('eth_sendTransaction', [send])
                local = None
                for attempt in range(200):
                    local = call('eth_getTransactionReceipt', [local_hash])
                    if local: break
                    time.sleep(.05)
                if not local or int(local['status'], 16) != 1: raise ValueError('local replay failed')
                local_block = call('eth_getBlockByHash', [local['blockHash'], False])
                for key in ('number', 'timestamp', 'miner', 'difficulty'):
                    if local_block[key].lower() != block[key].lower(): raise ValueError('replay block context differs: '+key)
                def log_values(value):
                    return [(log['address'].lower(), log['topics'], log['data']) for log in value['logs']]
                if log_values(local) != log_values(receipt): raise ValueError('replay receipt logs differ')
                trace = call('debug_traceTransaction', [local_hash, {'disableStorage': True, 'enableMemory': False, 'disableStack': False}])
                steps = trace['structLogs']
                if trace.get('failed') or not steps or steps[-1]['depth'] != 1:
                    raise ValueError('incomplete local execution')
                remaining = steps[-1]['gas'] - steps[-1]['gasCost']
                refund = steps[-1].get('refund') if steps[-1]['op'] in ('RETURN', 'STOP') else None
                charged = charged_gas(int(tx['gas'], 16), remaining, int(local['gasUsed'], 16), fork, refund)
                if charged != int(receipt['gasUsed'], 16): raise ValueError('replay gas differs under Opera rules')
                grouped = compact_trace(steps, tx['to'], vaults, prestate, local)
                evidence = {'method': 'local-prestate-replay', 'fork': fork,
                    'prestate_sha256': hashlib.sha256(json.dumps(prestate, sort_keys=True).encode()).hexdigest(),
                    'anvil_sha256': binary_hash(binary),
                    'receipt_logs_equal': True, 'opera_gas_used_equal': True,
                    'refund_counter': refund, 'local_gas_used_raw': str(int(local['gasUsed'], 16)),
                    'gas_used_raw': str(charged), 'block_number': number, 'block_timestamp': timestamp}
                if correction is not None: evidence['sender_prestate_correction'] = correction
                for value in grouped.values(): value['local_replay'] = evidence
                return grouped
            finally:
                process.terminate()
                try: process.wait(timeout=5)
                except subprocess.TimeoutExpired: process.kill(); process.wait()


def input_paths(root, tx_hash, receipt):
    root = Path(root)
    return {'prestate': root/'transactions'/tx_hash/'prestate.json.gz',
            'tx': root/'transactions'/tx_hash/'tx.json.gz',
            'block': root/'blocks'/(receipt['blockHash'].lower()+'.json.gz')}


def missing_input_count(root, tx_hash, receipt):
    return sum(not p.exists() for p in input_paths(root, tx_hash, receipt).values())


def batch_results(payload, expected):
    """Validate a bounded RPC batch without depending on migration tooling."""
    if not isinstance(payload, list):
        raise ValueError('provider did not return a batch')
    results = {}
    for item in payload:
        if not isinstance(item, dict):
            raise ValueError('batch contains malformed result')
        key = item.get('id')
        if (type(key) is not int or key not in expected or key in results
                or 'error' in item or item.get('result') is None):
            raise ValueError('batch contains duplicate, unexpected or failed result')
        results[key] = item['result']
    if results.keys() != expected.keys():
        raise ValueError('batch result missing')
    return {expected[key]: value for key, value in results.items()}


def fetch_replay_traces(url, root, tx_hash, receipt, vaults):
    """One bounded batch for missing native prestate/transaction/header inputs."""
    paths = input_paths(root, tx_hash, receipt)
    specifications = {'prestate': ('debug_traceTransaction', [tx_hash, {'tracer': 'prestateTracer', 'timeout': '10s'}]),
                      'tx': ('eth_getTransactionByHash', [tx_hash]),
                      'block': ('eth_getBlockByHash', [receipt['blockHash'], False])}
    missing = [key for key, path in paths.items() if not path.exists()]
    if missing:
        payload = [{'jsonrpc': '2.0', 'id': i, 'method': specifications[key][0], 'params': specifications[key][1]}
                   for i, key in enumerate(missing)]
        with requests.post(url, json=payload, timeout=45, stream=True) as response:
            response.raise_for_status(); data = bytearray()
            for chunk in response.iter_content(65536):
                data.extend(chunk)
                if len(data) > 20*1024**2: raise ValueError('native replay evidence size limit')
            values = batch_results(json.loads(data), dict(enumerate(missing)))
        for key, value in values.items():
            path = paths[key]; path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix('.tmp')
            with gzip.open(temporary, 'wt') as stream: json.dump(value, stream)
            temporary.replace(path)
    values = {key: json.load(gzip.open(path, 'rt')) for key, path in paths.items()}
    proof_path = paths['tx'].parent/'sender-prestate-proof.json'
    proof = json.loads(proof_path.read_text()) if proof_path.exists() else None
    if proof is not None: paths['sender_prestate_proof'] = proof_path
    forks = ['london'] if int(values['tx'].get('type', '0x0'), 16) == 2 else ['istanbul', 'berlin', 'london']
    errors = []
    for fork in forks:
        try:
            result = replay(values['prestate'], values['tx'], values['block'], receipt, vaults, fork, proof)
            for trace in result.values():
                trace['local_replay']['input_sha256'] = {key: hashlib.sha256(path.read_bytes()).hexdigest() for key,path in paths.items()}
            return result
        except ValueError as error:
            errors.append({'fork': fork, 'reason': str(error)})
    failure = paths['tx'].parent/'replay-failure.json'
    failure.write_text(json.dumps(errors, indent=2))
    raise ValueError('local replay validation failed')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True, type=Path, help='Offline JSON or JSON.gz replay bundle')
    parser.add_argument('--out', required=True, type=Path, help='Verified compact trace output JSON')
    args = parser.parse_args(argv)
    if args.input.resolve() == args.out.resolve():
        raise ValueError('output must not overwrite the input evidence')
    input_bytes = args.input.read_bytes()
    digest = hashlib.sha256(input_bytes).hexdigest()
    bundle = json.loads(gzip.decompress(input_bytes) if args.input.suffix == '.gz' else input_bytes)
    if not isinstance(bundle, dict):
        raise ValueError('replay bundle must be an object')
    required = ('prestate', 'tx', 'block', 'receipt')
    if any(not isinstance(bundle.get(key), dict) for key in required):
        raise ValueError('replay bundle requires prestate, tx, block and receipt objects')
    # The maintained regression captures carry the same fields, with the vault
    # and fork recorded beside the expected trace instead of duplicated.
    vaults = bundle.get('vaults')
    if vaults is None and isinstance(bundle.get('event'), dict):
        vaults = [bundle['event'].get('vault_address')]
    fork = bundle.get('fork')
    if fork is None:
        fork = bundle.get('trace', {}).get('local_replay', {}).get('fork')
    if (not isinstance(vaults, list) or not vaults
            or any(not isinstance(v, str) or len(v) != 42 or not v.startswith('0x')
                   or any(c not in '0123456789abcdefABCDEF' for c in v[2:]) for v in vaults)):
        raise ValueError('replay bundle requires explicit vault addresses')
    traces = replay(bundle['prestate'], bundle['tx'], bundle['block'], bundle['receipt'],
                    vaults, fork, bundle.get('sender_prestate_proof'))
    for trace in traces.values():
        trace['local_replay']['input_sha256'] = {'bundle': digest}
    result = {'input_sha256': digest, 'transaction_hash': bundle['tx']['hash'],
              'block_hash': bundle['block']['hash'], 'traces': traces}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile('w', dir=args.out.parent, prefix=args.out.name+'.', delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(result, stream, indent=2)
            stream.write('\n')
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        temporary.replace(args.out)
    finally:
        temporary.unlink(missing_ok=True)
    print(f'Wrote verified replay for {len(traces)} vault(s) to {args.out}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
