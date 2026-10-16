"""Acceptance policy is independent of fee arithmetic and price conversion."""
POLICY_VERSION = 'fee-acceptance-2'


def assess_candidate(candidate, evidence):
    verification = evidence.get('execution_verification', {})
    if (candidate['method'] == 'execution-fee-mint' and verification.get('status') == 'verified'
            and verification.get('fee_operand_raw') == candidate['total_fees_paid_raw']):
        status, reason, scope = 'accepted', None, 'fee-mint-execution'
    elif candidate['method'] == 'receipt-no-mint-zero':
        status, reason, scope = 'accepted', None, 'receipt'
    elif evidence.get('later_vault_report_in_receipt'):
        status, reason, scope = 'rejected', 'later_vault_report_in_receipt', 'end-of-block'
    else:
        # This candidate has no verified execution evidence; end-block state alone is insufficient.
        status, reason, scope = 'conditional', 'transaction_local_state_unverified', 'end-of-block'
    return {'policy_version': POLICY_VERSION, 'status': status, 'reason': reason,
            'state_scope': scope, 'state_suitability': 'verified' if status == 'accepted'
            else 'unsuitable' if status == 'rejected' else 'unverified'}


def normalize_candidate(candidate):
    result = dict(candidate)
    quality = result.pop('quality', None)
    result.setdefault('calculation_version', 'v2-fee-calculation-1')
    result.setdefault('precision', {'exact-state': 'singleton', 'lower-bound': 'bounded-interval',
                                   'pps-estimate': 'pps-estimate', 'receipt-zero': 'receipt-zero'}.get(quality, 'unverified'))
    return result
