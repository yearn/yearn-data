"""Canonical raw fees projected from existing reports, independent of USD prices."""
from __future__ import annotations

from collections import defaultdict
import json
import re
from typing import Any

from eth_abi import decode
from eth_utils import keccak

from .analysis import create_analysis_run, complete_analysis_run, write_output
from .chains import web3_for
from .config import CHAINS
from .indexing import _receipt_with_retry
from .storage import to_json

METHOD_VERSION = 'canonical-fees-6'
FEE_TOPIC = '0x' + keccak(text='FeeReport(uint256,uint256,uint256,uint256)').hex()
REPORT_TOPICS = {
    '0x' + keccak(text='StrategyReported(address,' + ','.join(['uint256'] * n) + ')').hex(): n
    for n in (7, 8)
}
COMPONENTS = ('management_fee_raw', 'performance_fee_raw', 'strategist_fee_raw',
              'protocol_fee_raw', 'manager_fee_raw', 'total_refunds_raw')


def report_inputs(report):
    return {k: report[k] for k in ('chain_id','tx_hash','log_index','vault_address','strategy_address',
                                   'block_number','gain_raw','loss_raw','api_version')}


def allocator_inputs(report):
    return {**report_inputs(report), **{k: report[k] for k in
        ('asset','asset_decimals','total_fees_raw','protocol_fees_raw','total_refunds_raw')}}


def unsigned(value: Any) -> int:
    if isinstance(value, bool) or not re.fullmatch(r'\d+', str(value)):
        raise ValueError('fee amounts must be unsigned integer strings')
    return int(value)


def _amounts(gain, loss, total) -> dict[str, Any]:
    return {
        'gross_gain_raw': str(unsigned(gain)), 'loss_raw': str(unsigned(loss)),
        'total_fees_paid_raw': str(unsigned(total)),
        **{key: None for key in COMPONENTS}, 'nominal_components': None,
        'amount_source': 'observed-event', 'component_coverage': 'complete',
        'report_kind': None, 'yield_mechanism': None,
    }


def normalize_allocator_fees(report) -> dict[str, Any]:
    gain, loss = unsigned(report['gain_raw']), unsigned(report['loss_raw'])
    total = unsigned(report['total_fees_raw'])
    protocol = unsigned(report['protocol_fees_raw'])
    refunds = unsigned(report['total_refunds_raw'])
    if protocol > total:
        raise ValueError('protocol fees exceed total fees')
    self_report = report['strategy_address'].lower() == report['vault_address'].lower()
    return {
        **_amounts(gain, loss, total), 'protocol_fee_raw': str(protocol),
        'manager_fee_raw': str(total - protocol), 'total_refunds_raw': str(refunds),
        'report_kind': 'self-report' if self_report else 'external-strategy',
        'yield_mechanism': ('direct-asset-distribution' if gain else 'self-report-loss' if loss else 'strategy-pnl')
        if self_report else 'strategy-pnl',
    }


def normalize_v2_fees(gain, loss, management, performance, strategist) -> dict[str, Any]:
    gain, loss = unsigned(gain), unsigned(loss)
    components = dict(zip(COMPONENTS[:3], map(lambda v: str(unsigned(v)), (management, performance, strategist))))
    nominal = sum(int(v) for v in components.values())
    capped = nominal > gain
    result = _amounts(gain, loss, min(gain, nominal))
    if capped:
        result.update(component_coverage='unavailable', nominal_components=components)
    else:
        result.update(components)
    return result


def _hex(value) -> str:
    return value.lower() if isinstance(value, str) else '0x' + bytes(value).hex()


def _number(value) -> int:
    return int(value, 16) if isinstance(value, str) and value.startswith('0x') else int(value)


def decode_v2_receipt(receipt, vault_address: str) -> dict[int, dict[str, Any]]:
    """Pair FeeReport to the next report of this vault within a complete receipt."""
    return decode_v2_report_logs(receipt["logs"], vault_address)


def decode_v2_report_logs(logs, vault_address: str) -> dict[int, dict[str, Any]]:
    """Pair complete FeeReport/StrategyReported logs for one transaction.

    Callers must establish transaction identity and completeness of both report
    topics and the fee topic. Filtered logs are not a full transaction receipt.
    """
    reports, fees = [], []
    transaction_ids, indices = set(), set()
    logs = sorted(logs, key=lambda log: _number(log["logIndex"]))
    for log in logs:
        if log['address'].lower() != vault_address.lower() or not log['topics']:
            continue
        topic = _hex(log['topics'][0])
        if topic != FEE_TOPIC and topic not in REPORT_TOPICS:
            continue
        if log.get('transactionHash') is not None:
            transaction_ids.add(_hex(log['transactionHash']))
            if len(transaction_ids) > 1:
                raise ValueError('V2 pairing requires one transaction')
        index = _number(log['logIndex'])
        if index in indices:
            raise ValueError('ambiguous duplicate V2 log index')
        indices.add(index)
        data = bytes.fromhex(_hex(log['data'])[2:])
        count = 4 if topic == FEE_TOPIC else REPORT_TOPICS[topic]
        if len(data) != count * 32:
            raise ValueError('unexpected V2 event data length')
        values = decode(['uint256'] * count, data)
        if topic == FEE_TOPIC:
            fees.append({'log_index': index, 'values': values, 'data': _hex(log['data'])})
        else:
            if len(log['topics']) != 2:
                raise ValueError('V2 report is missing its strategy topic')
            reports.append({'log_index': index, 'gain': values[0], 'loss': values[1],
                            'strategy': '0x' + _hex(log['topics'][1])[-40:], 'fee': None})
    for fee in fees:
        following = next((r for r in reports if r['log_index'] > fee['log_index']), None)
        if following is None or following['fee'] is not None:
            raise ValueError('unpaired or ambiguous V2 FeeReport')
        following['fee'] = fee
    return {r['log_index']: r for r in reports}


def _v2_amounts(report, decoded):
    if decoded is None:
        raise ValueError('stored report absent from transaction receipt')
    if (decoded['gain'] != unsigned(report['gain_raw']) or decoded['loss'] != unsigned(report['loss_raw'])
            or decoded['strategy'].lower() != report['strategy_address'].lower()):
        raise ValueError('stored report disagrees with receipt')
    fee = decoded['fee']
    if fee is not None:
        return normalize_v2_fees(decoded['gain'], decoded['loss'], *fee['values'][:3]), None
    from .fee_zero_rules import zero_gain_rule
    if decoded['gain'] == 0 and zero_gain_rule(report['api_version']):
        amounts = normalize_v2_fees(0, decoded['loss'], 0, 0, 0)
        amounts['amount_source'] = 'contract-zero-gain'
        return amounts, None
    if decoded['gain'] == 0:
        return None, 'zero_gain_requires_fee_evidence'
    version = re.fullmatch(r'0\.(\d+)\.(\d+)', report['api_version'] or '')
    emits_fees = version and (int(version[1]), int(version[2])) >= (4, 4)
    return None, 'missing_fee_report' if emits_fees else 'unsupported_or_unverified_release'


def _save(conn, report, amounts, reason, evidence, event_index=None, candidate=None, decision=None):
    if decision is None:
        from .fee_acceptance import POLICY_VERSION
        decision = {'policy_version': POLICY_VERSION, 'status': 'accepted' if amounts else 'unavailable',
                    'reason': reason, 'state_scope': 'event' if amounts else None,
                    'state_suitability': 'not-required' if amounts else 'unverified'}
    conn.execute('''
        INSERT INTO canonical_fee_reports (
            chain_id,tx_hash,report_log_index,event_log_index,contract_family,
            api_version,method_version,status,reason,accounting_json,evidence_json,candidate_json,decision_json
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(chain_id,tx_hash,report_log_index) DO UPDATE SET
            event_log_index=excluded.event_log_index,contract_family=excluded.contract_family,
            api_version=excluded.api_version,method_version=excluded.method_version,
            status=excluded.status,reason=excluded.reason,
            accounting_json=excluded.accounting_json,evidence_json=excluded.evidence_json,
            candidate_json=excluded.candidate_json,decision_json=excluded.decision_json
    ''', (report['chain_id'],report['tx_hash'],report['log_index'],event_index,
          'yearn-v3-allocator' if report['version']=='v3' else 'yearn-v2-vault',report['api_version'],
          METHOD_VERSION,'ok' if amounts is not None else 'unresolved',reason,
          to_json(amounts) if amounts is not None else None,to_json(evidence),
          to_json(candidate) if candidate else None,to_json(decision) if decision else None))


def index_canonical_fees(conn, chains=None, limit=None, retry_unresolved=False, receipt_fetcher=None,
                         reconstruct_v2=False, state_reader=None, verify_selective=False, trace_limit=10,
                         block_logs_fetcher=None, trace_fetcher=None, refresh_evidence=False, filtered_evidence_only=False, version=None, report_keys=None, verify_execution=False) -> int:
    """Project allocator fees locally; fetch each selected V2 receipt once per run.

    Scope is the stored report universe, including retired vaults. This does not
    certify historical completeness or expand Tokenized Strategy discovery.
    """
    if verify_execution and (not verify_selective or not report_keys):
        raise ValueError('explicit execution requires selective verification and report keys')
    if verify_selective and not reconstruct_v2:
        raise ValueError('selective verification requires V2 reconstruction')
    if verify_selective and trace_limit <= 0:
        raise ValueError('trace limit must be positive')
    if limit is not None and limit <= 0:
        raise ValueError('limit must be positive')
    if version not in (None, 'v2', 'v3'):
        raise ValueError('version must be v2 or v3')
    if version == 'v3' and filtered_evidence_only:
        raise ValueError('filtered evidence applies only to V2')
    params = []
    scope = ''
    if chains:
        ids = [CHAINS[chain].chain_id for chain in chains]
        scope = ' AND r.chain_id IN (' + ','.join('?' for _ in ids) + ')'
        params.extend(ids)
    if version is not None:
        scope += ' AND r.version=?'
        params.append(version)
    if report_keys is not None:
        if not report_keys:
            return 0
        scope += ' AND (r.chain_id,r.tx_hash,r.log_index) IN (VALUES '+','.join('(?,?,?)' for _ in report_keys)+')'
        params.extend(value for key in report_keys for value in key)
    if filtered_evidence_only:
        if refresh_evidence:
            raise ValueError('offline filtered projection cannot refresh evidence')
        scope += """ AND EXISTS (SELECT 1 FROM fee_filtered_log_evidence e WHERE
            e.chain_id=r.chain_id AND e.tx_hash=r.tx_hash AND e.report_log_index=r.log_index)"""
    retry = " OR f.status='unresolved'" if retry_unresolved else ''
    retry += " OR (r.version='v2' AND r.gain_raw='0' AND f.method_version<>?)"
    params.insert(0, METHOD_VERSION)
    if reconstruct_v2:
        retry += " OR (f.method_version<>? AND json_extract(f.accounting_json,'$.amount_source')='derived-contract')"
        # These placeholders precede the optional chain scope in the query.
        params.insert(1, METHOD_VERSION)
    query = f'''SELECT r.*,v.api_version,f.evidence_json AS previous_evidence FROM strategy_reports r
        LEFT JOIN vaults v ON v.chain_id=r.chain_id AND v.address=r.vault_address
        LEFT JOIN canonical_fee_reports f ON f.chain_id=r.chain_id AND f.tx_hash=r.tx_hash AND f.report_log_index=r.log_index
        WHERE r.version IN ('v2','v3') AND (f.chain_id IS NULL{retry}){scope}
        ORDER BY r.chain_id,r.block_number,r.tx_hash,r.log_index'''
    if limit is not None:
        query += ' LIMIT ?'
        params.append(limit)
    rows = conn.execute(query,params).fetchall()
    groups = defaultdict(list)
    for row in rows:
        groups[(row['chain_id'],row['tx_hash'])].append(row)
    clients = {}
    def client_for(chain_id):
        if chain_id not in clients:
            chain = next(c.key for c in CHAINS.values() if c.chain_id == chain_id)
            clients[chain_id] = web3_for(chain)
        return clients[chain_id]
    from .fee_trace import SelectiveFeeVerifier
    verifier = SelectiveFeeVerifier(conn, trace_limit, client_for, block_logs_fetcher, trace_fetcher) if verify_selective else None
    for (chain_id,tx_hash), reports in groups.items():
        receipt, receipt_error = None, None
        from .fee_log_evidence import load as load_filtered
        filtered = {}
        for r in reports:
            if r['version'] == 'v2' and not refresh_evidence:
                try:
                    value = load_filtered(conn, r)
                    if value is not None:
                        filtered[r['log_index']] = value
                except (ValueError, KeyError, TypeError):
                    filtered[r['log_index']] = None
        if any(r['version']=='v2' and r['log_index'] not in filtered for r in reports):
            try:
                cached = None if refresh_evidence else conn.execute(
                    'SELECT receipt_json,block_number,block_hash FROM fee_receipt_evidence WHERE chain_id=? AND tx_hash=?',
                    (chain_id,tx_hash)).fetchone()
                if cached and all(r['block_number']==cached['block_number'] for r in reports):
                    try:
                        receipt = json.loads(cached['receipt_json'])
                        if (_hex(receipt['transactionHash'])!=tx_hash.lower() or _number(receipt['status'])!=1
                                or not receipt.get('blockHash') or not isinstance(receipt.get('logs'),list)
                                or _number(receipt['blockNumber'])!=cached['block_number']
                                or _hex(receipt['blockHash'])!=cached['block_hash']):
                            receipt = None
                    except (ValueError,KeyError,TypeError):
                        receipt = None
                if receipt is None:
                    receipt = receipt_fetcher(chain_id,tx_hash) if receipt_fetcher else _receipt_with_retry(client_for(chain_id),tx_hash)
                if (_hex(receipt['transactionHash']) != tx_hash.lower()
                        or _number(receipt['status']) != 1 or not receipt.get('blockHash')):
                    raise ValueError('invalid receipt identity/status')
                from web3 import Web3
                if all(_number(receipt['blockNumber'])==r['block_number'] for r in reports):
                    conn.execute('INSERT INTO fee_receipt_evidence VALUES (?,?,?,?,?) ON CONFLICT(chain_id,tx_hash) DO UPDATE SET block_number=excluded.block_number, block_hash=excluded.block_hash, receipt_json=excluded.receipt_json',
                        (chain_id,tx_hash,_number(receipt['blockNumber']),_hex(receipt['blockHash']),Web3.to_json(receipt)))
            except Exception:
                receipt_error = 'receipt_unavailable_or_invalid'  # Never persist provider URLs/credentials.
        for report in reports:
            event_index, amounts, reason = None, None, None
            candidate, decision = None, None
            evidence = {'report_log_index':report['log_index'], 'report_inputs':report_inputs(report)}
            try:
                if report['version']=='v3':
                    amounts = normalize_allocator_fees(report)
                    event_index = report['log_index']
                    evidence.update(source='strategy_reports', allocator_inputs=allocator_inputs(report))
                elif report['log_index'] in filtered:
                    value = filtered[report['log_index']]
                    if value is None:
                        reason = 'invalid_filtered_log_evidence'
                        evidence['source'] = 'filtered_v2_logs'
                    else:
                        amounts, event_index, evidence = value
                elif receipt_error:
                    reason = receipt_error
                else:
                    if _number(receipt['blockNumber']) != report['block_number']:
                        raise ValueError('receipt block differs from report')
                    receipt_reports = decode_v2_receipt(receipt,report['vault_address'])
                    decoded = receipt_reports.get(report['log_index'])
                    amounts, reason = _v2_amounts(report,decoded)
                    fee = decoded['fee'] if decoded else None
                    event_index = fee['log_index'] if fee else report['log_index']
                    evidence.update(source='transaction_receipt', block_hash=_hex(receipt['blockHash']),
                                    fee_log_index=fee['log_index'] if fee else None,
                                    nominal_fee_values=[str(v) for v in fee['values']] if fee else None,
                                    fee_data=fee['data'] if fee else None,fee_topic=FEE_TOPIC if fee else None)
                    if amounts and amounts['amount_source']=='contract-zero-gain':
                        from .fee_zero_rules import zero_gain_rule
                        evidence['zero_gain_rule'] = zero_gain_rule(report['api_version'])
                    if reconstruct_v2 and amounts is None and fee is None:
                        from .fee_reconstruction import extract_fee_shares, reconstruct_candidate
                        from .fee_state import SUPPORTED_RELEASES, NO_MINT_RELEASES, read_fee_state
                        from .fee_trace import TRACE_RELEASES
                        if report['api_version'] in NO_MINT_RELEASES or (verify_selective and report['api_version'] in TRACE_RELEASES):
                            shares = extract_fee_shares(receipt['logs'], report['vault_address'], report['log_index'])
                            evidence['fee_shares'] = shares
                            if verifier and ((decoded['gain'] == 0 and shares['status'] == 'found') or
                                             (verify_execution and shares['status'] in ('found', 'zero-share-mint'))):
                                from .fee_acceptance import assess_candidate
                                verification=verifier.verify(report,receipt,shares,explicit=verify_execution)
                                if verify_execution:
                                    evidence['execution_selection']='explicit_report_keys'
                                evidence['fee_amount_basis']='assessed_asset_fee_operand'
                                evidence['issued_fee_shares_raw']=shares['minted_shares_raw']
                                evidence['execution_verification']=verification
                                evidence['direct_execution_asset']={'asset':report['asset'],'asset_decimals':report['asset_decimals']}
                                if verification['status']=='verified':
                                    candidate={'total_fees_paid_raw':verification['fee_operand_raw'],
                                        'method':'execution-fee-mint','precision':'singleton','interval':None,
                                        'calculation_version':verification['trace_version']}
                                    decision=assess_candidate(candidate,evidence)
                                    amounts=_amounts(decoded['gain'],decoded['loss'],candidate['total_fees_paid_raw'])
                                    amounts.update(amount_source='derived-contract',component_coverage='unavailable')
                                    reason=None
                                else:
                                    reason=verification.get('reason','execution_verification_unavailable')
                                _save(conn,report,amounts,reason,evidence,event_index,candidate,decision)
                                continue
                            if shares['status'] not in ('found', 'no-mint'):
                                reason = 'unresolved_fee_share_evidence'
                            else:
                                state = {}
                                later_report = any(index > report['log_index'] for index in receipt_reports)
                                evidence['later_vault_report_in_receipt'] = later_report
                                state_supported = report['api_version'] in SUPPORTED_RELEASES or (verify_selective and report['api_version'] in TRACE_RELEASES)
                                if shares['status'] == 'found' and not state_supported:
                                    reason, state = 'unsupported_historical_fee_state_release', None
                                elif shares['status'] == 'found':
                                    try:
                                        prior = json.loads(report['previous_evidence']) if report['previous_evidence'] and not refresh_evidence else {}
                                        if not prior and not refresh_evidence:
                                            cached = conn.execute('''SELECT evidence_json FROM fee_state_evidence WHERE
                                                chain_id=? AND tx_hash=? AND report_log_index=? AND block_hash=?''',
                                                (chain_id,tx_hash,report['log_index'],evidence['block_hash'])).fetchone()
                                            prior = json.loads(cached[0]) if cached else {}
                                        cached_state = prior.get('historical_state')
                                        reusable = (cached_state is not None and prior.get('report_inputs')==evidence['report_inputs']
                                            and prior.get('block_hash')==evidence['block_hash'] and prior.get('fee_shares')==shares)
                                        if reusable:
                                            try:
                                                reconstruct_candidate(shares,cached_state)
                                            except (ValueError,KeyError,TypeError):
                                                reusable=False
                                        state = cached_state if reusable else (state_reader(report,receipt,shares) if state_reader
                                            else read_fee_state(client_for(chain_id),report,receipt,shares))
                                    except Exception:
                                        reason = 'historical_fee_state_unavailable_or_invalid'
                                        state = None
                                if state is not None:
                                    evidence['historical_state'] = state
                                    from .fee_acceptance import assess_candidate, normalize_candidate
                                    candidate = normalize_candidate(reconstruct_candidate(shares, state))
                                    conn.execute('INSERT INTO fee_state_evidence VALUES (?,?,?,?,?) ON CONFLICT(chain_id,tx_hash,report_log_index,block_hash) DO UPDATE SET evidence_json=excluded.evidence_json',
                                        (chain_id,tx_hash,report['log_index'],evidence['block_hash'],json.dumps(evidence)))
                                    if verifier and shares['status'] == 'found':
                                        verification = verifier.verify(report, receipt, shares)
                                        evidence['execution_verification'] = verification
                                        if verification['status'] == 'verified':
                                            candidate = {'total_fees_paid_raw':verification['fee_operand_raw'],
                                                'method':'execution-fee-mint','precision':'singleton',
                                                'interval':None,'calculation_version':verification['trace_version'],
                                                'baseline_candidate':candidate}
                                    decision = assess_candidate(candidate, evidence)
                                    reason = decision['reason']
                                    if decision['status'] == 'accepted':
                                        amounts = _amounts(decoded['gain'], decoded['loss'], candidate['total_fees_paid_raw'])
                                        amounts.update(amount_source='derived-contract', component_coverage='unavailable')
            except (ValueError, KeyError, TypeError):
                amounts, reason = None, 'invalid_report_or_fee_evidence'
            _save(conn,report,amounts,reason,evidence,event_index,candidate,decision)
        conn.commit()
    return len(rows)


def run_canonical_fees(conn) -> int:
    """Export raw canonical fees and coverage of the stored report universe."""
    run_id = create_analysis_run(conn,'canonical-fees',{'method_version':METHOD_VERSION,'valuation':'raw-assets-only'})
    coverage, by_asset = {}, {}
    rows = conn.execute('''SELECT r.*,f.contract_family,f.status,f.reason,f.event_log_index,
        f.accounting_json,f.evidence_json,f.candidate_json,f.decision_json,f.api_version,f.method_version FROM strategy_reports r LEFT JOIN canonical_fee_reports f
        ON f.chain_id=r.chain_id AND f.tx_hash=r.tx_hash AND f.report_log_index=r.log_index
        WHERE r.version IN ('v2','v3') ORDER BY r.chain_id,r.block_number,r.log_index''')
    for r in rows:
        amounts = json.loads(r['accounting_json']) if r['accounting_json'] else None
        state = r['status'] or 'not_indexed'
        key = (r['chain_id'],r['version'])
        counts = coverage.setdefault(key,{'chain_id':r['chain_id'],'version':r['version'],
            'scope':'stored_reports_only','reports':0,'known_fee_reports':0,'zero_fee_reports':0,
            'unresolved_reports':0,'not_indexed_reports':0})
        counts['reports'] += 1
        if amounts:
            counts['known_fee_reports'] += 1
            counts['zero_fee_reports'] += amounts['total_fees_paid_raw']=='0'
        else:
            counts['unresolved_reports' if state=='unresolved' else 'not_indexed_reports'] += 1
        output = {'chain_id':r['chain_id'],'vault_address':r['vault_address'],'strategy_address':r['strategy_address'],
            'tx_hash':r['tx_hash'],'report_log_index':r['log_index'],'fee_log_index':r['event_log_index'],
            'block_number':r['block_number'],'block_timestamp':r['block_timestamp'],'asset':r['asset'],
            'asset_decimals':r['asset_decimals'],'status':state,'reason':r['reason'],
            'contract_family':r['contract_family'],'api_version':r['api_version'],'method_version':r['method_version'],
            **{k:None for k in _amounts(0,0,0)}, **(amounts or {}),
            'evidence_json':r['evidence_json']}
        evidence = json.loads(r['evidence_json']) if r['evidence_json'] else {}
        reconstruction = json.loads(r['candidate_json']) if r['candidate_json'] else {}
        decision = json.loads(r['decision_json']) if r['decision_json'] else {}
        # Guard exports from legacy rows even if a caller has not rerun migration.
        if amounts and amounts.get('amount_source') == 'derived-contract' and decision.get('status') != 'accepted':
            raise ValueError('legacy derived fee requires init_db migration before export')
        output.update(reconstruction_method=reconstruction.get('method'),
                      candidate_fee_raw=reconstruction.get('total_fees_paid_raw'),
                      calculation_precision=reconstruction.get('precision'),
                      acceptance_status=decision.get('status', 'accepted' if amounts else 'unavailable'),
                      state_scope=decision.get('state_scope'), state_suitability=decision.get('state_suitability'),
                      decision_json=r['decision_json'], candidate_json=r['candidate_json'],
                      fee_interval_json=to_json(reconstruction['interval']) if reconstruction.get('interval') else None)
        for transfer in evidence.get('fee_shares', {}).get('recipient_transfers', []):
            write_output(conn, run_id, 'canonical_fee_share_distributions', {
                'chain_id': r['chain_id'], 'tx_hash': r['tx_hash'], 'report_log_index': r['log_index'],
                'vault_address': r['vault_address'], 'fee_status': state, 'fee_reason': r['reason'],
                'share_evidence_status': evidence['fee_shares']['status'],
                'role': 'fee_share_evidence_not_additional_revenue', **transfer})
        nominal = output.pop('nominal_components')
        output['nominal_components_json'] = to_json(nominal) if nominal is not None else None
        write_output(conn,run_id,'canonical_fee_events',output)
        if r['asset']:
            asset_key = (r['chain_id'],r['asset'],r['asset_decimals'])
            bucket = by_asset.setdefault(asset_key,{'chain_id':r['chain_id'],'asset':r['asset'],
                'asset_decimals':r['asset_decimals'],'known_fee_subtotal_raw':0,'known_reports':0,'unknown_reports':0,'derived_reports':0,'lower_bound_reports':0,'pps_estimate_reports':0})
            if amounts:
                bucket['known_fee_subtotal_raw'] += int(amounts['total_fees_paid_raw'])
                bucket['known_reports'] += 1
                bucket['derived_reports'] += amounts['amount_source'] == 'derived-contract'
                bucket['lower_bound_reports'] += reconstruction.get('precision') == 'bounded-interval'
                bucket['pps_estimate_reports'] += reconstruction.get('precision') == 'pps-estimate'
            else:
                bucket['unknown_reports'] += 1
    for counts in coverage.values():
        write_output(conn,run_id,'canonical_fee_coverage',counts)
    for bucket in by_asset.values():
        bucket['known_fee_subtotal_raw'] = str(bucket['known_fee_subtotal_raw'])
        write_output(conn,run_id,'canonical_fees_by_asset',bucket)
    from .tokenized_fees import write_tokenized_outputs
    write_tokenized_outputs(conn,run_id)
    complete_analysis_run(conn,run_id)
    return run_id
