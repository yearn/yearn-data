"""Offline formula and acceptance recomputation from retained fee evidence."""
import json

from .fee_acceptance import assess_candidate, normalize_candidate, POLICY_VERSION
from .fee_reconstruction import reconstruct_candidate
from .fees import _amounts, _save, normalize_allocator_fees, report_inputs, allocator_inputs
from .config import CHAINS


def recompute_canonical_fees(conn, mode, chains=None, limit=None, version=None, report_keys=None):
    """Never fetch evidence. Missing/changed inputs remain explicit and retryable."""
    if mode not in ('policy','formulas'):
        raise ValueError('mode must be policy or formulas')
    if limit is not None and limit<=0:
        raise ValueError('limit must be positive')
    if version not in (None,'v2','v3'):
        raise ValueError('version must be v2 or v3')
    params=[];scope=''
    if chains:
        ids=[CHAINS[c].chain_id for c in chains]
        scope=' AND r.chain_id IN ('+','.join('?' for _ in ids)+')';params.extend(ids)
    if version is not None:
        scope+=' AND r.version=?';params.append(version)
    if report_keys is not None:
        if not report_keys:
            return {'processed':0,'accepted':0,'conditional':0,'rejected':0,'unavailable':0}
        scope += ' AND (r.chain_id,r.tx_hash,r.log_index) IN (VALUES '+','.join('(?,?,?)' for _ in report_keys)+')'
        params.extend(value for key in report_keys for value in key)
    sql='''SELECT r.*,v.api_version,f.event_log_index,f.evidence_json,f.candidate_json FROM canonical_fee_reports f
        JOIN strategy_reports r ON r.chain_id=f.chain_id AND r.tx_hash=f.tx_hash AND r.log_index=f.report_log_index
        LEFT JOIN vaults v ON v.chain_id=r.chain_id AND v.address=r.vault_address
        WHERE (f.candidate_json IS NOT NULL OR json_extract(f.evidence_json,'$.source')='filtered_v2_logs' OR
            (r.version='v3' AND json_extract(f.evidence_json,'$.source')='strategy_reports'))'''+scope+' ORDER BY r.chain_id,r.block_number,r.log_index'
    if limit is not None:sql+=' LIMIT ?';params.append(limit)
    counts={'processed':0,'accepted':0,'conditional':0,'rejected':0,'unavailable':0}
    for r in conn.execute(sql,params).fetchall():
        evidence=json.loads(r['evidence_json'])
        if r['version'] == 'v3' and evidence.get('source') == 'strategy_reports':
            try:
                if evidence.get('allocator_inputs') != allocator_inputs(r):
                    raise ValueError('allocator inputs missing or changed')
                from .yvusd_fees import applies, accounting
                amounts = accounting(r, evidence['yvusd']) if applies(dict(r)) else normalize_allocator_fees(r)
                reason = None
            except (ValueError, KeyError, TypeError):
                amounts, reason = None, 'cached_allocator_inputs_missing_or_changed'
            _save(conn, r, amounts, reason, evidence, r['log_index'])
            counts['processed'] += 1
            counts['accepted' if amounts is not None else 'unavailable'] += 1
            continue
        if evidence.get('source') == 'filtered_v2_logs':
            from .fee_log_evidence import load
            try:
                value = load(conn, r)
                if value is None:
                    raise ValueError('filtered log evidence missing')
                amounts, event_index, evidence = value
                reason = None
            except (ValueError, KeyError, TypeError):
                amounts, event_index, reason = None, r['event_log_index'], 'invalid_filtered_log_evidence'
            _save(conn, r, amounts, reason, evidence, event_index)
            counts['processed'] += 1
            counts['accepted' if amounts is not None else 'unavailable'] += 1
            continue
        candidate=json.loads(r['candidate_json']);amounts=None
        try:
            snapshot=evidence.get('report_inputs')
            if snapshot is not None and snapshot!=report_inputs(r):
                raise ValueError('source report changed')
            if candidate['method']=='execution-fee-mint' and 'direct_execution_asset' in evidence:
                from .fee_trace import verify_cached_direct_execution
                verification=verify_cached_direct_execution(conn,r,evidence)
                evidence['execution_verification']=verification
                candidate={**candidate,'total_fees_paid_raw':verification['fee_operand_raw'],
                           'calculation_version':verification['trace_version']}
            elif mode=='formulas':
                if snapshot is None:
                    raise ValueError('legacy source binding unavailable')
                baseline=normalize_candidate(reconstruct_candidate(evidence['fee_shares'],evidence['historical_state']))
                if candidate['method']=='execution-fee-mint':
                    # Formula changes update the estimate, never overwrite a traced operand.
                    candidate={**candidate,'baseline_candidate':baseline}
                else:
                    candidate=baseline
            decision=assess_candidate(candidate,evidence)
            if decision['status']=='accepted':
                amounts=_amounts(r['gain_raw'],r['loss_raw'],candidate['total_fees_paid_raw'])
                amounts.update(amount_source='derived-contract',component_coverage='unavailable')
        except (ValueError,KeyError,TypeError):
            decision={'policy_version':POLICY_VERSION,'status':'unavailable','reason':'cached_fee_inputs_missing_or_changed',
                      'state_scope':None,'state_suitability':'unverified'}
        _save(conn,r,amounts,decision['reason'],evidence,r['event_log_index'],candidate,decision)
        counts['processed']+=1;counts[decision['status']]+=1
    conn.commit()
    return counts
