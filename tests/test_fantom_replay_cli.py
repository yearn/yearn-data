"""Portable offline replay entrypoint and retained acquisition batch validation."""
import gzip
import hashlib
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
import replay_fantom_prestate as operator

VAULT = '0x' + '2' * 40


def bundle():
    return {'prestate': {}, 'tx': {'hash': '0xtransaction'}, 'block': {'hash': '0xblock'},
            'receipt': {}, 'vaults': [VAULT], 'fork': 'istanbul'}


@pytest.mark.parametrize('compressed', [False, True])
def test_offline_cli_binds_input_and_writes_compact_result(tmp_path, monkeypatch, compressed):
    path = tmp_path / ('input.json.gz' if compressed else 'input.json')
    opener = gzip.open if compressed else open
    with opener(path, 'wt') as stream:
        json.dump(bundle(), stream)
    calls = []
    def verified(*args):
        calls.append(args)
        return {VAULT: {'ops': [], 'local_replay': {'receipt_logs_equal': True, 'opera_gas_used_equal': True}}}
    monkeypatch.setattr(operator, 'replay', verified)
    monkeypatch.setattr(operator, 'fetch_replay_traces', lambda *a: pytest.fail('offline CLI acquired RPC inputs'))
    out = tmp_path / 'result.json'
    assert operator.main(['--input', str(path), '--out', str(out)]) == 0
    assert calls[0][4:6] == ([VAULT], 'istanbul')
    value = json.loads(out.read_text())
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    assert value['input_sha256'] == digest
    assert value['traces'][VAULT]['local_replay']['input_sha256'] == {'bundle': digest}
    assert value['transaction_hash'] == '0xtransaction'


def test_frozen_fixture_schema_passes_proof_and_fork(tmp_path, monkeypatch):
    paths = sorted((Path(__file__).parent / 'fixtures/fee-fantom-replay').glob('*.gz'))
    assert paths
    for path in paths:
        with gzip.open(path, 'rt') as stream:
            case = json.load(stream)
        calls = []
        monkeypatch.setattr(operator, 'replay', lambda *args: calls.append(args) or {})
        operator.main(['--input', str(path), '--out', str(tmp_path / 'result.json')])
        assert calls[0][4] == [case['event']['vault_address']]
        assert calls[0][5] == case['trace']['local_replay']['fork']
        assert calls[0][6] == case.get('sender_prestate_proof')


def test_rejected_replay_does_not_replace_output(tmp_path, monkeypatch):
    path = tmp_path / 'input.json'
    path.write_text(json.dumps(bundle()))
    out = tmp_path / 'result.json'
    out.write_text('retained')
    def invalid(*args):
        raise ValueError('replay receipt logs differ')
    monkeypatch.setattr(operator, 'replay', invalid)
    with pytest.raises(ValueError, match='logs differ'):
        operator.main(['--input', str(path), '--out', str(out)])
    assert out.read_text() == 'retained'
    with pytest.raises(ValueError, match='overwrite'):
        operator.main(['--input', str(path), '--out', str(path)])


@pytest.mark.parametrize('payload', [{}, [], [{'id': 0, 'result': None}],
    [{'id': True, 'result': 1}], [{'id': 0, 'result': 1}, {'id': 0, 'result': 2}],
    [{'id': 1, 'result': 1}], [{'id': 0, 'error': {}}], [None]])
def test_native_input_batch_rejects_missing_or_unbound_results(payload):
    with pytest.raises(ValueError):
        operator.batch_results(payload, {0: 'prestate'})


def test_native_input_batch_accepts_reordered_bound_results():
    assert operator.batch_results([{'id': 1, 'result': 'b'}, {'id': 0, 'result': 'a'}],
                                  {0: 'tx', 1: 'block'}) == {'tx': 'a', 'block': 'b'}


def test_input_hash_binds_the_bytes_actually_replayed(tmp_path, monkeypatch):
    path = tmp_path / 'input.json'
    original = json.dumps(bundle()).encode()
    path.write_bytes(original)
    def verified(*args):
        path.write_text('changed while replay ran')
        return {VAULT: {'ops': [], 'local_replay': {}}}
    monkeypatch.setattr(operator, 'replay', verified)
    out = tmp_path / 'result.json'
    operator.main(['--input', str(path), '--out', str(out)])
    assert json.loads(out.read_text())['input_sha256'] == hashlib.sha256(original).hexdigest()
