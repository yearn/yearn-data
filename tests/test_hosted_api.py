import copy
import json
from contextlib import closing
import shutil
import threading
from http.server import ThreadingHTTPServer
from urllib.request import urlopen
from urllib.error import HTTPError

import pytest
from test_pairing import snapshots
from test_tvl_api import run, DAY, PARENT
from yearn_data.storage import connect, init_db
from yearn_data.pairing import select_pairing, PairingStore, pairing_response
from yearn_data.tvl_api import TvlStore, tvl_response
from yearn_data.tvl_history_cache import prepare
from yearn_data.analytics import SCHEMA as ANALYTICS_SCHEMA
from yearn_data.hosted_publications import publish, Registry, save_release
from yearn_data.hosted_api import HostedAPI, Handler, cache_headers


@pytest.fixture
def published(tmp_path):
    database = tmp_path/'source.sqlite'
    financial = tmp_path/'fees'
    history = tmp_path/'tvl'
    with closing(connect(database)) as conn:
        init_db(conn)
        runs = snapshots(conn)
        run(conn, 1, DAY, [(PARENT, '100')])
        run(conn, 2, DAY+7*86400, [(PARENT, '200')])
    fees = select_pairing(database, *runs, financial)
    tvl = TvlStore(database, history).get()
    prepare(tvl)
    analytics_id = 'a'*64
    meta = {'publicationId': analytics_id, 'feesDatasetId': fees['datasetId'], 'tvlDatasetId': tvl.id}
    with closing(connect(database)) as conn:
        conn.executescript(ANALYTICS_SCHEMA)
        conn.execute('INSERT INTO analytics_publications VALUES (?,1,?)',
                     (analytics_id, json.dumps({'meta':meta, 'stack':{'chains':[]}, 'comparison':{},
                                               'comparable':{}, 'profitability':{'None':{'vaults':[]}}})))
        conn.execute("INSERT INTO analytics_selection VALUES ('powerglove',?)", (analytics_id,))
        conn.commit()
    release = publish(database, financial, history)
    return database, financial, history, release


def test_fileless_routes_match_existing_contracts(published):
    database, financial, history, _ = published
    expected_fees = pairing_response(PairingStore(financial), '/api/fees/history?interval=weekly')
    expected_tvl = tvl_response(TvlStore(database, history, auto_publish=False), '/api/tvl')
    # Remove every local publication file before initializing the hosted API.
    shutil.rmtree(financial); shutil.rmtree(history)
    api = HostedAPI(database)
    assert api.response('/api/fees/history?interval=weekly') == expected_fees
    assert api.response('/api/tvl') == expected_tvl
    assert api.response('/api/fees/stack')[1]['chains'] == []
    assert api.response('/api/analytics/publication')[1]['feesDatasetId'] == expected_fees[1]['datasetId']
    assert api.response('/api/fees?chainId=bad')[0] == 400
    assert api.response('/api/fees?datasetId='+'f'*64)[0] == 410
    assert api.response('/api/fees/stack?publicationId=bad')[0] == 400
    assert api.response('/api/publication?unknown=yes')[0] == 400
    assert api.response('/unknown')[0] == 404


def test_atomic_selection_and_immutable_old_manifests(published):
    database, _, _, release = published
    registry = Registry(database)
    old = registry.selection()
    fees = registry.manifest('fees', old['feesDatasetId'])
    tvl = registry.manifest('tvl', old['tvlDatasetId'])
    with closing(connect(database)) as conn:
        assert save_release(conn, fees, tvl, old['analyticsPublicationId']) == release
        changed = copy.deepcopy(tvl);changed['currentBridgePolicy'] = 'changed'
        with pytest.raises(ValueError, match='immutable'):
            save_release(conn, fees, changed, 'b'*64)
    assert registry.selection() == old
    with closing(connect(database)) as conn:
        new = save_release(conn, fees, tvl, 'b'*64)
    assert registry.selection()['releaseId'] == new  # Same reader, no restart.
    assert registry.manifest('tvl', old['tvlDatasetId']) == tvl


def test_publish_rejects_mismatched_analytics(published):
    database, financial, history, _ = published
    old = Registry(database).selection()
    with closing(connect(database)) as conn:
        conn.execute('INSERT INTO analytics_publications VALUES (?,1,?)',
                     ('b'*64, json.dumps({'meta':{'feesDatasetId':'c'*64,'tvlDatasetId':old['tvlDatasetId']}})))
        conn.commit()
    with pytest.raises(ValueError, match='analytics must reference'):
        publish(database, financial, history, 'b'*64)
    assert Registry(database).selection() == old


def test_request_connections_are_readonly(published, monkeypatch):
    from yearn_data import hosted_publications, pairing, analytics, tvl_api, tvl_history_cache
    original = connect
    calls = []
    def reader(*args, **kwargs):
        assert kwargs.get('readonly') is True
        calls.append(args)
        return original(*args, **kwargs)
    for module in (hosted_publications, pairing, analytics, tvl_api, tvl_history_cache):
        monkeypatch.setattr(module, 'connect', reader)
    api = HostedAPI(published[0])
    for route in ['/api/fees', '/api/tvl', '/api/fees/stack', '/api/publication']:
        assert api.response(route)[0] == 200
    assert calls


@pytest.mark.parametrize('url,status,expected', [
    ('/api/fees',200,'public, s-maxage=30'),
    ('/api/fees?datasetId='+'a'*64,200,'public, s-maxage=3600'),
    ('/api/profitability?datasetId='+'a'*64,200,'public, s-maxage=30'),
    ('/api/profitability?publicationId='+'a'*64,200,'public, s-maxage=3600'),
    ('/api/fees?datasetId='+'a'*64,400,None),
    ('/api/publication',503,None),
])
def test_cdn_policy(url,status,expected):
    headers = cache_headers(url,status)
    assert headers.get('Vercel-CDN-Cache-Control') == expected
    if status != 200:assert headers['Cache-Control']=='no-store'


def test_http_failure_does_not_expose_credentials(monkeypatch):
    from yearn_data import hosted_api
    def fail():raise ValueError('postgres://secret-password')
    monkeypatch.setattr(hosted_api, 'application', fail)
    server = ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread = threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        with pytest.raises(HTTPError) as caught:
            urlopen(f'http://127.0.0.1:{server.server_port}/api/fees')
        assert caught.value.code == 503
        assert caught.value.headers['Cache-Control']=='no-store'
        assert 'secret' not in caught.value.read().decode()
    finally:
        server.shutdown();server.server_close();thread.join()

# Reuse the existing fixture, which creates and drops a dedicated test schema.
from test_postgres import pg


def test_postgres_registry_transaction_and_readonly_selection(pg):
    conn, _ = pg
    fees = {'datasetId':'1'*64,'database':'neon'}
    tvl = {'datasetId':'2'*64,'database':'neon'}
    release = save_release(conn, fees, tvl, '3'*64)
    registry = Registry('neon')
    assert registry.selection()['releaseId'] == release
    with pytest.raises(ValueError, match='immutable'):
        save_release(conn, fees, dict(tvl,extra='changed'), '4'*64)
    assert registry.selection()['releaseId'] == release
    assert registry.manifest('tvl', tvl['datasetId']) == tvl


def test_publish_rejects_tampered_tvl_manifest(published):
    database, financial, history, _ = published
    selected = Registry(database).selection()
    path = history/'datasets'/(selected['tvlDatasetId']+'.json')
    body = json.loads(path.read_text());body['currentBridgePolicy']='changed'
    path.write_text(json.dumps(body))
    with pytest.raises(ValueError, match='content does not match'):
        publish(database, financial, history)
    assert Registry(database).selection() == selected


def test_prepared_history_survives_removal_of_old_ingestion_rows(published):
    database, _, history, _ = published
    route = '/api/tvl/history/runs/latest?interval=weekly'
    expected = tvl_response(TvlStore(database, history, auto_publish=False), route)
    with closing(connect(database)) as conn:
        conn.execute('DELETE FROM tvl_snapshots WHERE timestamp=?', (DAY,))
        conn.commit()
    api = HostedAPI(database)
    assert api.response(route) == expected
    assert len(expected[1]['chart']) == 2

@pytest.mark.parametrize('route', [
    '/api/fees', '/api/fees/vaults', '/api/fees/history?interval=weekly',
    '/api/fees/history?chainId=1,10&interval=monthly',
    '/api/fees?chainId=10&since=1704153600&until=1704240000',
    '/api/fees?chainId=1&vaultAddress=0x'+'22'*20,
    '/api/fees/history?chainId=1&vaultAddress=0x'+'33'*20+'&interval=weekly',
    '/api/fees?chainId=999', '/api/fees?since=9999999999',
])
def test_compact_financial_rows_match_without_analysis_outputs(published, route):
    database, financial, _, _ = published
    expected = pairing_response(PairingStore(financial), route)
    with closing(connect(database)) as conn:
        conn.execute('DELETE FROM analysis_outputs')
        conn.commit()
    assert HostedAPI(database).response(route) == expected


@pytest.mark.parametrize('encoding,compressed', [('gzip',True), ('gzip;q=0',False), ('identity',False)])
def test_http_compression(monkeypatch, encoding, compressed):
    import gzip
    from urllib.request import Request
    from yearn_data import hosted_api
    payload = {'data':'a'*3000}
    class App:
        def response(self, url):return 200, payload
    monkeypatch.setattr(hosted_api, 'application', lambda:App())
    server = ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread = threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        request = Request(f'http://127.0.0.1:{server.server_port}/api/fees',
                          headers={'Accept-Encoding':encoding})
        with urlopen(request) as response:
            assert (response.headers.get('Content-Encoding')=='gzip') == compressed
            assert response.headers['Vary'] == 'Accept-Encoding'
            body=response.read()
            assert len(body)==int(response.headers['Content-Length'])
            assert json.loads(gzip.decompress(body) if compressed else body)==payload
    finally:
        server.shutdown();server.server_close();thread.join()


def test_postgres_compact_rows_preserve_filters_and_nulls(pg):
    from yearn_data.hosted_fees import prepare as prepare_fees, PublishedFees
    from yearn_data.pairing import PairingDataset
    conn, _ = pg
    init_db(conn)
    runs = snapshots(conn)
    source = PairingDataset('neon', *runs, vault_names={})
    prepare_fees(conn, source)
    prepare_fees(conn, source)  # Same immutable publication is safe to retry.
    reader = PublishedFees('neon', source.id)
    for view in ('summary', 'history', 'vaults'):
        assert reader.view(view, chains=(10,), interval='weekly') == source.view(view, chains=(10,), interval='weekly')


def test_release_rollback_rejects_unknown_and_restores_selection(published):
    from yearn_data.hosted_publications import select_release
    database, _, _, original = published
    registry = Registry(database)
    previous = registry.selection()
    with closing(connect(database)) as conn:
        other = save_release(conn, registry.manifest('fees', previous['feesDatasetId']),
                             registry.manifest('tvl', previous['tvlDatasetId']), 'b'*64)
    assert registry.selection()['releaseId'] == other
    assert select_release(database, original) == original
    with pytest.raises(ValueError, match='unknown releaseId'):
        select_release(database, 'f'*64)
    assert registry.selection() == previous


def test_prepare_analytics_does_not_switch_legacy_selection(published, monkeypatch):
    from yearn_data import analytics
    database, financial, history, _ = published
    old = Registry(database).selection()
    def prepare_owned(db, fees, tvl, *, select):
        assert select is False
        assert fees.id == old['feesDatasetId'] and tvl.id == old['tvlDatasetId']
        return old['analyticsPublicationId']
    monkeypatch.setattr(analytics, 'prepare', prepare_owned)
    assert publish(database, financial, history, prepare_analytics=True) == old['releaseId']


def test_hosted_date_index_does_not_scan_vault_rows(published):
    database, _, _, release = published
    registry = Registry(database)
    identity = registry.selection()['tvlDatasetId']
    with closing(connect(database)) as conn:
        conn.execute('DROP TABLE tvl_chart_rows')
        conn.commit()
    assert registry.tvl(identity).dates() == (DAY, DAY+7*86400)
