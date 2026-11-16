import json
import asyncio
from urllib.parse import urlencode
from pathlib import Path

from optic_lake.api import create_app
from tests.support import LakeCase


class Response:
    def __init__(self, status, body):
        self.status_code = status
        self.body = body
        self.text = body.decode('utf-8')

    def json(self):
        return json.loads(self.text)


class ASGIClient:
    def __init__(self, app):
        self.app = app

    def request(self, method, path, json_body=None, params=None):
        body = json.dumps(json_body).encode('utf-8') if json_body is not None else b''
        query = urlencode(params or {}).encode('ascii')
        scope = {'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1',
                 'method': method, 'scheme': 'http', 'path': path, 'raw_path': path.encode(),
                 'query_string': query, 'root_path': '',
                 'headers': [(b'host', b'testserver'), (b'content-type', b'application/json')],
                 'client': ('127.0.0.1', 12345), 'server': ('testserver', 80)}
        chunks = []
        received = False

        async def receive():
            nonlocal received
            if received:
                return {'type': 'http.disconnect'}
            received = True
            return {'type': 'http.request', 'body': body, 'more_body': False}

        async def send(message):
            chunks.append(message)

        asyncio.run(self.app(scope, receive, send))
        status = next(item['status'] for item in chunks if item['type'] == 'http.response.start')
        payload = b''.join(item.get('body', b'') for item in chunks
                           if item['type'] == 'http.response.body')
        return Response(status, payload)

    def get(self, path, params=None):
        return self.request('GET', path, params=params)

    def post(self, path, json=None):
        return self.request('POST', path, json_body=json)

    def put(self, path, json=None):
        return self.request('PUT', path, json_body=json)


class ApiTests(LakeCase):
    def setUp(self):
        super().setUp()
        self.client = ASGIClient(create_app(str(Path(self.temp.name) / 'http.sqlite3')))

    def _post(self, path, payload, status=201):
        response = self.client.post(path, json=payload)
        self.assertEqual(response.status_code, status, response.text)
        return response.json()

    def _register(self):
        for path, items in (('/facilities', self.fixture['facilities']),
                            ('/families', self.fixture['families']),
                            ('/stations', self.fixture['stations']),
                            ('/sources', self.fixture['sources']),
                            ('/contracts', self.fixture['contracts'])):
            for item in items:
                self._post(path, item)

    def test_http_replay_to_release(self):
        self._register()
        batches = [self._post('/batches', item) for item in self.fixture['batches']]
        self.assertEqual(batches[0]['quarantined'], 1)
        self.assertEqual(batches[1]['accepted'], 1)
        self._post('/links', self.fixture['links'][0])
        prepared = self._post('/snapshots', self.fixture['snapshot'])
        release = self._post('/snapshots/SNAP1/publish', {}, status=200)
        self.assertEqual(prepared['row_count'], 2)
        self.assertEqual(release['version'], 1)
        latest = self.client.get('/releases/latest').json()
        self.assertEqual(len(latest['rows']), 2)
        verified = self.client.get('/audit/releases/1/verify').json()
        self.assertTrue(verified['valid'])

    def test_http_error_maps_to_structured_conflict(self):
        self._register()
        response = self.client.post('/facilities', json=self.fixture['facilities'][0])
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['error'], 'conflict')

    def test_http_quarantine_is_observable(self):
        self._register()
        self._post('/batches', self.fixture['batches'][0])
        response = self.client.get('/batches/B_M/quarantine')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()[0]['external_id'], 'M_BAD')

    def test_http_source_disable_blocks_loading(self):
        self._register()
        response = self.client.put('/sources/SRC_M/enabled', json={'enabled': False})
        self.assertEqual(response.status_code, 200)
        blocked = self.client.post('/batches', json=self.fixture['batches'][0])
        self.assertEqual(blocked.status_code, 422)
        self.assertEqual(blocked.json()['error'], 'invalid_input')

    def test_http_diagnostics_show_consistent_storage(self):
        self._register()
        for item in self.fixture['batches']:
            self._post('/batches', item)
        result = self.client.get('/integrity').json()
        self.assertTrue(result['valid'])
        self.assertEqual(result['counts']['raw_rows'], 3)
        overview = self.client.get('/overview/facilities/F1',
                                   params={'from_day': '2026-02-01', 'to_day': '2026-02-02'}).json()
        self.assertEqual(overview['event_count'], 2)

    def test_http_missing_payload_fields_are_validation_error(self):
        response = self.client.post('/facilities', json={'facility_id': 'F1'})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()['error'], 'invalid_input')
