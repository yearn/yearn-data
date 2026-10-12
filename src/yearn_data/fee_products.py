"""Explicit product membership; grouping never changes contract-level records."""
from .ybold_fees import VAULT, STAKER
from .yeth_fees import VAULT as YETH_VAULT, RECOVERY

PRODUCTS = ({'id': 'ybold', 'chainId': 1, 'address': VAULT, 'name': 'yBOLD',
             'memberAddresses': (VAULT, STAKER), 'earningsAddress': VAULT},
            {'id': 'yeth-recovery', 'chainId': 1, 'address': YETH_VAULT, 'name': 'yETH Recovery',
             'memberAddresses': (YETH_VAULT, RECOVERY), 'earningsAddress': YETH_VAULT})


def product_for(chain_id, address):
    return next((p for p in PRODUCTS if p['chainId'] == chain_id
                 and address.lower() in p['memberAddresses']), None)


def group_rows(fees, earnings):
    grouped = []
    for row in fees:
        product = product_for(row['chain_id'], row['vault_address'])
        grouped.append(dict(row, vault_address=product['address']) if product else row)
    # The staking leg recognizes redistributed yield. Keep only the product's
    # designated earnings leg, even if staking reports are indexed in future.
    return grouped, [r for r in earnings if not (p := product_for(r['chain_id'], r['vault_address']))
                     or r['vault_address'].lower() == p['earningsAddress']]
