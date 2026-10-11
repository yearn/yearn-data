"""Classify verified yBOLD accountant allocations to stakers, not fee revenue."""
import json

from eth_abi import decode
from eth_utils import keccak

VAULT = '0x9f4330700a36b29952869fac9b33f45eedd8a3d8'
ACCOUNTANT = '0x53acebb9470cfc9d231075154f5dcf1586a4c6fa'
STAKER = '0x23346b04a7f55b8760e5860aa5a77383d63491cd'
CODE_HASH = '0xa3098fa22faa347d2cded2cac45f884a69895472d3af03f2d369a4247d477e90'
REPORT_TOPIC = '0x' + keccak(text='StrategyReported(address,uint256,uint256,uint256,uint256,uint256,uint256)').hex()


def applies(report):
    return report.get('chain_id', 1) == 1 and report['vault_address'].lower() == VAULT


def collect(report, receipt, client):
    """Bind configuration and verified implementation to the receipt's block hash."""
    from .fees import _hex
    from web3 import Web3
    block = receipt['blockHash']
    def address_call(address, signature):
        raw = client.eth.call({'to': Web3.to_checksum_address(address), 'data': keccak(text=signature)[:4]},
                              block_identifier=block)
        if len(raw) != 32:
            raise ValueError('invalid historical yBOLD address response')
        return '0x' + bytes(raw).hex()[-40:]
    accountant = address_call(VAULT, 'accountant()')
    return {'accountant': accountant, 'recipient': address_call(accountant, 'feeRecipient()'),
            'accountant_code_hash': '0x' + keccak(client.eth.get_code(
                Web3.to_checksum_address(accountant), block_identifier=block)).hex(),
            'block_hash': _hex(block), 'receipt': json.loads(Web3.to_json(receipt))}


def accounting(report, evidence):
    from .fees import _hex, _number, unsigned, normalize_allocator_fees
    receipt = evidence['receipt']
    if (not applies(dict(report)) or evidence['accountant'].lower() != ACCOUNTANT
            or evidence['recipient'].lower() != STAKER or evidence['accountant_code_hash'] != CODE_HASH
            or evidence['block_hash'] != _hex(receipt['blockHash'])
            or _hex(receipt['transactionHash']) != report['tx_hash'].lower()
            or _number(receipt['blockNumber']) != report['block_number'] or _number(receipt['status']) != 1):
        raise ValueError('invalid yBOLD accountant/recipient/receipt evidence')
    seen = set()
    matched = False
    for log in receipt['logs']:
        index = _number(log['logIndex'])
        if index in seen:
            raise ValueError('duplicate receipt log index')
        seen.add(index)
        if index != report['log_index']:
            continue
        if (log['address'].lower() != VAULT or len(log['topics']) != 2
                or _hex(log['topics'][0]) != REPORT_TOPIC
                or '0x' + _hex(log['topics'][1])[-40:] != report['strategy_address'].lower()):
            raise ValueError('yBOLD report identity mismatch')
        values = decode(['uint256'] * 6, bytes.fromhex(_hex(log['data'])[2:]))
        for offset, field in ((0, 'gain_raw'), (1, 'loss_raw'), (3, 'protocol_fees_raw'),
                              (4, 'total_fees_raw'), (5, 'total_refunds_raw')):
            if values[offset] != unsigned(report[field]):
                raise ValueError('yBOLD report amount mismatch')
        matched = True
    if not matched:
        raise ValueError('yBOLD report missing from receipt')
    total, protocol = unsigned(report['total_fees_raw']), unsigned(report['protocol_fees_raw'])
    if protocol > total or total > unsigned(report['gain_raw']) or unsigned(report['total_refunds_raw']):
        raise ValueError('unreconciled yBOLD distribution')
    return normalize_allocator_fees(report, staker_distribution=total - protocol)
