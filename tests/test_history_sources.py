from types import SimpleNamespace

from eth_abi import encode
from eth_utils import event_abi_to_log_topic
from hexbytes import HexBytes
import pytest
from web3 import Web3

from yearn_data import abis
from yearn_data.history_sources import HistoricalSource, inventory_targets, route, write_rows
from yearn_data.storage import connect, init_db

ADDRESS = '0x' + '11' * 20
ASSET = '0x' + '22' * 20
HASH = '0x' + 'aa' * 32
TX = '0x' + 'bb' * 32


def client(chain=10, logs=()):
    return SimpleNamespace(codec=Web3().codec, eth=SimpleNamespace(
        chain_id=chain, block_number=100,
        get_block=lambda number: dict(number=90 if number == 'finalized' else number, hash=HexBytes(HASH), timestamp=1000),
        get_logs=lambda params: logs))


def registry_log(abi, values):
    indexed = [field for field in abi['inputs'] if field['indexed']]
    plain = [field for field in abi['inputs'] if not field['indexed']]
    return dict(address=ADDRESS, blockNumber=20, blockHash=HexBytes(HASH), logIndex=0,
                transactionHash=HexBytes(TX), transactionIndex=0,
                topics=[event_abi_to_log_topic(abi)] + [encode([field['type']], [values[field['name']]]) for field in indexed],
                data=HexBytes(encode([field['type'] for field in plain], [values[field['name']] for field in plain])))


def test_matrix_and_inventory():
    assert route(1, 'reports') == 'envio'
    assert route(10, 'reports') == route(250, 'reports') == 'rpc'
    assert route(1, 'flows') == 'rpc'
    assert route(1, 'reports', 'rpc') == 'rpc'
    with pytest.raises(ValueError):
        route(10, 'reports', 'envio')
    for chain in ['op', 'ftm']:
        assert list(inventory_targets(chain))
        assert all(t.version == 'v2' for t in inventory_targets(chain))
    assert any(t.kind == 'registry_experimental' for t in inventory_targets('op', True))
    assert any(t.action == 'removed' for t in inventory_targets('eth'))


def test_pin_and_wrong_chain():
    source = HistoricalSource('op', client())
    assert source.pin(use_envio=False)['number'] == 90
    with pytest.raises(ValueError, match='safe RPC'):
        source.pin(end=91, use_envio=False)
    assert source.pin(confirmations=20, use_envio=False)['number'] == 80
    with pytest.raises(ValueError, match='chain mismatch'):
        HistoricalSource('ftm', client()).pin(use_envio=False)
    with pytest.raises(ValueError, match='hash changed'):
        source.verify_pin(dict(number=90, hash='wrong'))


def test_envio_outage_does_not_fallback_and_watermark_bounds():
    calls = []
    def failed(*args):
        calls.append(1)
        raise RuntimeError('offline')
    with pytest.raises(RuntimeError):
        HistoricalSource('eth', client(1), failed).pin()
    assert len(calls) == 1
    source = HistoricalSource('eth', client(1), lambda *args: {'chain_metadata': [{'latest_processed_block': 70}]})
    assert source.pin()['number'] == 70
    with pytest.raises(ValueError, match='watermark'):
        source.pin(end=80)


def test_optimism_registry_decoding_and_deduplication(tmp_path):
    target = next(inventory_targets('op'))
    values = dict(token=ASSET, vault_id=0, vault=ADDRESS, api_version='0.4.6')
    log = registry_log(target.abi, values)
    log['address'] = target.address
    source = HistoricalSource('op', client(logs=[log]))
    rows = source.inventory(target, 0, 50, 'rpc')
    assert rows[0]['vault_address'] == ADDRESS
    assert rows[0]['block_number'] == 20
    conn = connect(tmp_path / 'test.sqlite')
    init_db(conn)
    with conn:
        assert write_rows(conn, 'vault_inventory_events', rows) == 1
        assert write_rows(conn, 'vault_inventory_events', rows) == 0
    log['blockHash'] = HexBytes('0x'+'00'*32)
    with pytest.raises(ValueError, match='hash mismatch'):
        source.inventory(target, 0, 50, 'rpc')


def test_missing_entity_and_out_of_scope_envio_are_not_empty_success():
    source = HistoricalSource('eth', client(1), lambda *args: {})
    with pytest.raises(ValueError, match='missing Envio'):
        list(source.envio_nodes('StrategyReported', 'chainId', 'vaultAddress', ADDRESS, 0, 50))
    node = dict(chainId=1, vaultAddress=ADDRESS, blockNumber=20, logIndex=0, blockHash=HASH, blockTimestamp=1000, transactionHash=TX)
    source.gql = lambda *args: {'StrategyReported': [node, node]}
    with pytest.raises(ValueError, match='pagination'):
        list(source.envio_nodes('StrategyReported', 'chainId', 'vaultAddress', ADDRESS, 0, 50))
    node['chainId'] = 10
    with pytest.raises(ValueError, match='scope mismatch'):
        list(source.envio_nodes('StrategyReported', 'chainId', 'vaultAddress', ADDRESS, 0, 50))


def test_rpc_result_at_common_cap_is_not_complete_history():
    source = HistoricalSource('op', client(logs=[{}] * 10000))
    with pytest.raises(ValueError, match='result cap'):
        list(source.rpc_logs(ADDRESS, abis.V2_STRATEGY_REPORTED_EVENTS, 0, 100))


def test_legacy_envio_inventory_payload_agrees_with_rpc(tmp_path):
    target = next(inventory_targets('op'))
    values = dict(token=ASSET, vault_id=1, vault=ADDRESS, api_version='0.4.6')
    log = registry_log(target.abi, values)
    log['address'] = target.address
    rows = HistoricalSource('op', client(logs=[log])).inventory(target, 0, 50, 'rpc')
    conn = connect(tmp_path/'legacy.sqlite')
    init_db(conn)
    import json
    legacy = {**rows[0], 'decoded_json': json.dumps(dict(token=ASSET, deployment_id='1', vault=ADDRESS,
                 api_version='0.4.6', chainId=10, blockNumber=20, blockTimestamp=1000, registryAddress=target.address,
                 logIndex=0, transactionHash=TX))}
    with conn:
        write_rows(conn, 'vault_inventory_events', [legacy])
        assert write_rows(conn, 'vault_inventory_events', rows) == 0
    changed = {**rows[0], 'decoded_json': json.dumps({**values, 'vault_id':2})}
    with pytest.raises(ValueError, match='conflicting inventory'):
        with conn:
            write_rows(conn, 'vault_inventory_events', [changed])


@pytest.mark.parametrize('stored_case', ['checksum', 'lower'])
def test_envio_finds_both_producer_address_formats(stored_case):
    emitter = Web3.to_checksum_address('0x'+'ab'*20)
    stored = emitter if stored_case=='checksum' else emitter.lower()
    node = dict(chainId=1, vaultAddress=stored, blockNumber=20, logIndex=0,
                blockHash=HASH, blockTimestamp=1000, transactionHash=TX)
    def gql(query, variables):
        # Hasura's _in filter is case-sensitive; a successful empty response
        # must not hide records stored under the other normal address format.
        return {'StrategyReported': [node] if stored in variables['addresses'] else []}
    source = HistoricalSource('eth', client(1), gql)
    assert list(source.envio_nodes('StrategyReported', 'chainId', 'vaultAddress', emitter.lower(), 20, 20)) == [node]


def test_corrected_address_policy_does_not_reuse_old_empty_coverage(tmp_path):
    from yearn_data.coverage import Scope, commit_range, missing_ranges, fingerprint
    from yearn_data.history_sources import report_scope
    conn=connect(tmp_path/'coverage.sqlite');init_db(conn)
    target=next(inventory_targets('eth'))
    old=Scope(target.chain_id,target.address,'inventory:'+target.entity)
    commit_range(conn,old,0,50,source='envio',end_hash=HASH,run_id=None,write=lambda _:0)
    assert list(missing_ranges(conn,target.scope,0,50,100)) == [(0,50)]
    vault=dict(chain_id=1,address=ADDRESS,version='v3',asset=ASSET,asset_decimals=6)
    old=Scope(1,ADDRESS,'reports',fingerprint(['v1','v3',ASSET.lower(),6]))
    commit_range(conn,old,0,50,source='envio',end_hash=HASH,run_id=None,write=lambda _:0)
    assert list(missing_ranges(conn,report_scope(vault),0,50,100)) == [(0,50)]
