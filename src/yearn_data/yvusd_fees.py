"""Separate verified yvUSD locker distributions from fees, retaining raw deductions."""
from eth_utils import keccak
from eth_abi import decode

VAULT = '0x696d02db93291651ed510704c9b286841d506987'
ACCOUNTANTS = frozenset({'0xaaafea48472f77563961cdb53291dedfb46f9040',
                         '0xab9018a699003a777d690c156045dfc4a7ef3a96'})
FEE_TOPIC = '0x' + keccak(text='FeesReported(uint256,uint256,uint256)').hex()
REPORT_TOPIC = '0x' + keccak(text='StrategyReported(address,uint256,uint256,uint256,uint256,uint256,uint256)').hex()


def applies(report):
    return report.get('chain_id', 1) == 1 and report['vault_address'].lower() == VAULT


def split(report, evidence):
    """Only accept an exact uncapped reconciliation; ambiguous charges stay unavailable.

    Protocol deductions or capped/rounded quotes need additional execution evidence,
    rather than an invented proportional allocation. None occurred in the backfill.
    """
    from .fees import _hex, _number, unsigned
    receipt = evidence['receipt']
    accountant = evidence['accountant'].lower()
    if (accountant not in ACCOUNTANTS or _hex(receipt['transactionHash']) != report['tx_hash'].lower()
            or _number(receipt['blockNumber']) != report['block_number']
            or _number(receipt['status']) != 1 or not receipt.get('blockHash')):
        raise ValueError('invalid yvUSD receipt/accountant identity')
    pending = []
    seen = set()
    for log in sorted(receipt['logs'], key=lambda item: _number(item['logIndex'])):
        index = _number(log['logIndex'])
        if index in seen:
            raise ValueError('duplicate receipt log index')
        seen.add(index)
        if not log['topics']:
            continue
        topic = _hex(log['topics'][0])
        if log['address'].lower() == accountant and topic == FEE_TOPIC:
            if len(log['topics']) != 4 or _hex(log['data']) != '0x':
                raise ValueError('invalid FeesReported encoding')
            pending.append([int(_hex(value), 16) for value in log['topics'][1:]])
        if log['address'].lower() != VAULT or topic != REPORT_TOPIC:
            continue
        if index == report['log_index']:
            if len(log['topics']) != 2 or ('0x'+_hex(log['topics'][1])[-40:]) != report['strategy_address'].lower():
                raise ValueError('report strategy mismatch')
            values = decode(['uint256'] * 6, bytes.fromhex(_hex(log['data'])[2:]))
            for offset, field in ((0,'gain_raw'),(1,'loss_raw'),(3,'protocol_fees_raw'),(4,'total_fees_raw'),(5,'total_refunds_raw')):
                if values[offset] != unsigned(report[field]):
                    raise ValueError('report amount mismatch')
            if values[4] == 0 and not pending:
                return (0, 0, 0)
            if len(pending) != 1 or sum(pending[0]) != values[4] or values[3] != 0:
                raise ValueError('unreconciled yvUSD fee split')
            return tuple(pending[0])
        pending = []  # Never reuse a split consumed by an earlier vault report.
    raise ValueError('report missing from receipt')


def accounting(report, evidence):
    from .fees import normalize_allocator_fees
    management, performance, bonus = split(report, evidence)
    amounts = normalize_allocator_fees(report, locker_split=(management, performance, bonus))
    return amounts
