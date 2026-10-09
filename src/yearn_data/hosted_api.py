"""Powerglove HTTP adapter. Requests only read explicitly published results."""
from http.server import BaseHTTPRequestHandler
import json
import gzip
import logging
import os
from functools import lru_cache
from urllib.parse import parse_qs, urlsplit

from .hosted_publications import Registry
from .storage import DATABASE_ERRORS

FEE_PATHS = {'/api/fees', '/api/fees/', '/api/fees/history', '/api/fees/vaults'}
TVL_PATHS = {'/api/tvl', '/api/tvl/', '/api/tvl/history/runs/latest',
             '/api/tvl/history/runs/latest/constant-price', '/api/tvl/curation-products', '/api/audit/tree'}
ANALYTICS_PATHS = {'/api/analytics/publication', '/api/fees/stack', '/api/profitability',
                   '/api/comparison', '/api/comparison/defillama-comparable'}


class SelectedStore:
    def __init__(self, load, selected):
        self.load, self.selected = load, selected

    def get(self, identity=None):
        return self.load(self.selected if identity is None else identity)


class HostedAPI:
    def __init__(self, database):
        from .analytics import AnalyticsStore
        self.registry = Registry(database)
        self.analytics = AnalyticsStore(database)

    def response(self, url):
        from .tvl_api import tvl_response
        from .analytics import analytics_response
        path = urlsplit(url).path
        if path not in FEE_PATHS | TVL_PATHS | ANALYTICS_PATHS | {'/api/publication'}:
            return 404, {'error': 'Not found'}
        selection = self.registry.selection()
        if path == '/api/publication':
            if urlsplit(url).query:
                return 400, {'error': 'unsupported filter'}
            return 200, selection
        if path in FEE_PATHS:
            from .pairing import pairing_response
            return pairing_response(SelectedStore(self.registry.fees, selection['feesDatasetId']), url)
        if path in TVL_PATHS:
            return tvl_response(SelectedStore(self.registry.tvl, selection['tvlDatasetId']), url)
        return analytics_response(SelectedStore(self.analytics.get, selection['analyticsPublicationId']), url)


def cache_headers(url, status):
    # Short TTL for mutable selections. Only successful, explicitly pinned views
    # get the longer CDN TTL; errors are never cached. Browsers revalidate.
    headers = {'Cache-Control': 'no-cache'}
    if status != 200:
        return {'Cache-Control': 'no-store'}
    request = urlsplit(url)
    query = parse_qs(request.query, keep_blank_values=True)
    pin = 'publicationId' if request.path in ANALYTICS_PATHS else 'datasetId'
    pinned = len(query.get(pin, [])) == 1 and len(query[pin][0]) == 64
    headers['Vercel-CDN-Cache-Control'] = 'public, s-maxage=3600' if pinned else 'public, s-maxage=30'
    return headers


@lru_cache(maxsize=1)
def application():
    return HostedAPI(os.environ.get('YEARN_DATA_DB', 'neon'))


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            status, payload = application().response(self.path)
        except (OSError, *DATABASE_ERRORS, ValueError, KeyError, TypeError, json.JSONDecodeError):
            # Provider/database exception messages can contain connection details.
            logging.getLogger(__name__).error('Hosted publication could not be read')
            status, payload = 503, {'error': 'Published data is unavailable'}
        body = json.dumps(payload, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
        gzip_allowed = False
        for encoding in self.headers.get('Accept-Encoding', '').split(','):
            parts = encoding.strip().split(';')
            if parts[0].lower() == 'gzip':
                quality = next((p.strip()[2:] for p in parts[1:] if p.strip().startswith('q=')), '1')
                try:
                    gzip_allowed = 0 < float(quality) <= 1
                except ValueError:
                    pass
        compressed = gzip_allowed and len(body) > 1024
        if compressed:
            body = gzip.compress(body, compresslevel=3, mtime=0)
        self.send_response(status)
        self.send_header('Vary', 'Accept-Encoding')
        if compressed:
            self.send_header('Content-Encoding', 'gzip')
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Access-Control-Allow-Origin', '*')
        for key, value in cache_headers(self.path, status).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        self.send_response(405)
        self.send_header('Allow', 'GET')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', '0')
        self.end_headers()


def main():
    """Local-only runner for the same handler that Vercel imports."""
    import argparse
    from http.server import ThreadingHTTPServer
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=3494)
    args = parser.parse_args()
    ThreadingHTTPServer(('127.0.0.1', args.port), Handler).serve_forever()


if __name__ == '__main__':
    main()
