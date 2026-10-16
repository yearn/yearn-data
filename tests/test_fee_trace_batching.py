"""One transaction trace can supply isolated evidence for multiple vaults."""
from types import SimpleNamespace
import pytest
from yearn_data.fee_trace import fetch_traces


def test_multi_vault_trace_uses_one_request_and_separates_operations(monkeypatch):
    a, b = '0x' + '11' * 20, '0x' + '22' * 20
    calls = []
    payload = {'result': {'ops': [{'address': a, 'op': 'MUL', 'pc': 1},
                                 {'address': b, 'op': 'DIV', 'pc': 2},
                                 {'address': a, 'op': 'LOG3', 'pc': 3}], 'overflow': False, 'error': None}}
    def post(url, **kwargs):
        calls.append(kwargs['json'])
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: payload)
    monkeypatch.setattr('requests.post', post)
    client = SimpleNamespace(provider=SimpleNamespace(endpoint_uri='https://example.invalid'))
    result = fetch_traces(client, '0xtx', [a, b, a])
    assert len(calls) == 1
    assert a in calls[0]['params'][1]['tracer'] and b in calls[0]['params'][1]['tracer']
    assert [op['pc'] for op in result[a]['ops']] == [1, 3]
    assert [op['pc'] for op in result[b]['ops']] == [2]
    assert all('address' not in op for row in result.values() for op in row['ops'])
    payload['result']['overflow'] = True
    assert all(row['overflow'] for row in fetch_traces(client, '0xtx', [a, b]).values())
    with pytest.raises(ValueError, match='empty'):
        fetch_traces(client, '0xtx', [])
