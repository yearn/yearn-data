"""Retained, complete filtered V2 log evidence. Never substitutes for a receipt."""
import hashlib
import json


from .fee_zero_rules import zero_gain_rule


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def binding(report):
    from .fees import report_inputs
    return {**report_inputs(report), 'asset': report['asset'], 'asset_decimals': report['asset_decimals']}


def evaluate(report, payload):
    from .fees import FEE_TOPIC, REPORT_TOPICS, decode_v2_report_logs, _v2_amounts, _number
    if payload['report_inputs'] != binding(report):
        raise ValueError('filtered log report binding changed')
    coverage = payload['coverage']
    if (coverage['chain_id'] != report['chain_id'] or
            coverage['vault'] != report['vault_address'].lower() or
            set(coverage['topics']) not in (set(REPORT_TOPICS), {FEE_TOPIC, *REPORT_TOPICS}) or
            not coverage['from_block'] <= report['block_number'] <= coverage['to_block'] or
            not coverage['acquisition_id']):
        raise ValueError('incomplete filtered log coverage')
    logs = payload['logs']
    if not logs or any(n.get('removed') or n['address'].lower() != coverage['vault'] or
            n['transactionHash'].lower() != report['tx_hash'].lower() or
            _number(n['blockNumber']) != report['block_number'] or
            n['blockHash'].lower() != payload['block_hash'] or
            n['topics'][0].lower() not in coverage['topics'] for n in logs):
        raise ValueError('invalid filtered log identity')
    decoded = decode_v2_report_logs(logs, coverage['vault']).get(report['log_index'])
    if not decoded:
        raise ValueError('report required')
    zero_gain = not decoded['fee'] and decoded['gain'] == 0 and bool(zero_gain_rule(report['api_version']))
    if set(coverage['topics']) == set(REPORT_TOPICS) and not zero_gain:
        raise ValueError('report-only evidence requires a supported zero-gain rule')
    if not decoded['fee'] and not zero_gain:
        raise ValueError('observed FeeReport or supported zero-gain rule required')
    amounts, reason = _v2_amounts(report, decoded)
    if reason or amounts is None:
        raise ValueError('filtered log report differs')
    if zero_gain:
        amounts['amount_source'] = 'contract-zero-gain'
    return amounts, decoded['fee']['log_index'] if decoded['fee'] else None


def retain(conn, report, logs, coverage):
    payload = {'report_inputs': binding(report), 'coverage': coverage, 'logs': logs,
               'block_hash': logs[0]['blockHash'].lower()}
    evaluate(report, payload)
    conn.execute('INSERT OR REPLACE INTO fee_filtered_log_evidence VALUES (?,?,?,?,?)',
        (report['chain_id'], report['tx_hash'], report['log_index'], json.dumps(payload), digest(payload)))


def load(conn, report):
    row = conn.execute('SELECT payload_json,payload_sha256 FROM fee_filtered_log_evidence '
        'WHERE chain_id=? AND tx_hash=? AND report_log_index=?',
        (report['chain_id'], report['tx_hash'], report['log_index'])).fetchone()
    if row is None:
        return None
    payload = json.loads(row['payload_json'])
    if digest(payload) != row['payload_sha256']:
        raise ValueError('filtered log evidence changed')
    acquisition = conn.execute('SELECT manifest_json FROM fee_log_acquisitions WHERE acquisition_id=?',
                              (payload['coverage']['acquisition_id'],)).fetchone()
    if acquisition is None or digest(json.loads(acquisition[0])) != payload['coverage']['acquisition_id']:
        raise ValueError('filtered log acquisition missing or changed')
    manifest = json.loads(acquisition[0])
    topics = set()
    for scope in manifest['sources']:
        if (manifest['chain_id'] == report['chain_id'] and scope['chain_id'] == report['chain_id'] and
                report['vault_address'].lower() in scope['addresses'] and
                scope['from_block'] <= report['block_number'] <= scope['to_block']):
            topics.update(scope['topics'])
    if not set(payload['coverage']['topics']) <= topics:
        raise ValueError('acquisition does not cover report')
    amounts, index = evaluate(report, payload)
    return amounts, index, {'source': 'filtered_v2_logs', 'report_inputs': payload['report_inputs'],
                           'filtered_log_sha256': row['payload_sha256'],
                           'acquisition_id': payload['coverage']['acquisition_id'],
                           'block_hash': payload['block_hash'],
                           **({'zero_gain_rule': zero_gain_rule(report['api_version'])} if index is None else {})}
