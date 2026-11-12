from concurrent.futures import ThreadPoolExecutor
import io
import json
import socket
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from mini_redis.benchmark import running
from mini_redis.client import Client
from mini_redis.demo import Catalog, DemoServer
from mini_redis.protocol import MAX_BULK, ProtocolError, RedisError, encode, read_request, read_response
from mini_redis.server import RedisServer
from mini_redis.store import Store


class ProtocolTests(unittest.TestCase):
    def test_binary_utf8_roundtrip(self):
        args = [b'SET', '한글'.encode(), b'\x00\xff\r\n']
        self.assertEqual(read_request(io.BytesIO(encode(args))), args)

    def test_invalid_frames(self):
        for data in (b'GET x\r\n', b'*0\r\n', b'*65\r\n', b'*-1\r\n',
                     b'*1\r\n$-1\r\n', b'*1\r\n$3\r\nx', b'*1\r\n$1\r\nx!!',
                     b'*1\r\n$' + str(MAX_BULK + 1).encode() + b'\r\n'):
            with self.subTest(data=data), self.assertRaises(ProtocolError):
                read_request(io.BytesIO(data))


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        self.context = running(RedisServer(('127.0.0.1', 0), Store(lambda: self.now)))
        self.server = self.context.__enter__()
        self.addCleanup(self.context.__exit__, None, None, None)
        self.client = Client(port=self.server.server_address[1])

    def test_crud_and_errors(self):
        self.assertEqual(self.client.execute('PING'), 'PONG')
        self.assertEqual(self.client.execute('SET', '한글', ''), 'OK')
        self.assertEqual(self.client.execute('GET', '한글'), b'')
        self.assertEqual(self.client.execute('DEL', '한글'), 1)
        self.assertIsNone(self.client.execute('GET', '한글'))
        for args in [('WHAT',), ('GET',), ('SET', 'a', 'b', 'EX', '0'), ('SET', 'a', 'b', 'PX', 'abc')]:
            with self.assertRaises(RedisError):
                self.client.execute(*args)

    def test_expiry_commands(self):
        self.client.execute('SET', 'a', 'b', 'PX', 1000)
        self.assertEqual(self.client.execute('PTTL', 'a'), 1000)
        self.assertEqual(self.client.execute('EXISTS', 'a', 'missing'), 1)
        self.now += 1
        self.assertIsNone(self.client.execute('GET', 'a'))
        self.assertEqual(self.client.execute('DBSIZE'), 0)

    def test_fragmented_and_pipelined_requests(self):
        with socket.create_connection(self.server.server_address, timeout=2) as sock:
            raw = encode([b'SET', b'key', b'\x00\r\n'])
            for byte in raw:
                sock.sendall(bytes([byte]))
            sock.sendall(encode([b'GET', b'key']) + encode([b'PING']))
            with sock.makefile('rb') as stream:
                self.assertEqual(read_response(stream), 'OK')
                self.assertEqual(read_response(stream), b'\x00\r\n')
                self.assertEqual(read_response(stream), 'PONG')

    def test_command_error_preserves_connection(self):
        with socket.create_connection(self.server.server_address, timeout=2) as sock:
            sock.sendall(encode([b'BAD']) + encode([b'PING']))
            with sock.makefile('rb') as stream:
                with self.assertRaises(RedisError):
                    read_response(stream)
                self.assertEqual(read_response(stream), 'PONG')

    def test_malformed_client_does_not_affect_others(self):
        with socket.create_connection(self.server.server_address, timeout=2) as sock:
            sock.sendall(b'*1\r\n$-1\r\n')
            with sock.makefile('rb') as stream:
                with self.assertRaises(RedisError):
                    read_response(stream)
        self.assertEqual(self.client.execute('PING'), 'PONG')

    def test_parallel_tcp_increments(self):
        with ThreadPoolExecutor(max_workers=16) as pool:
            results = list(pool.map(lambda _: self.client.execute('INCR', 'counter'), range(300)))
        self.assertEqual(sorted(results), list(range(1, 301)))
        self.assertEqual(self.client.execute('GET', 'counter'), b'300')


class APITests(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        redis_context = running(RedisServer(('127.0.0.1', 0), Store(lambda: self.now)))
        self.redis = redis_context.__enter__()
        self.addCleanup(redis_context.__exit__, None, None, None)
        self.catalog = Catalog(Client(port=self.redis.server_address[1]), delay=0, ttl_ms=1000)
        api_context = running(DemoServer(('127.0.0.1', 0), self.catalog))
        self.api = api_context.__enter__()
        self.addCleanup(api_context.__exit__, None, None, None)
        self.root = f'http://127.0.0.1:{self.api.server_port}'

    def get(self, path='/products/1'):
        with urlopen(self.root + path, timeout=3) as response:
            return json.load(response), response.headers.get('X-Cache')

    def test_cache_hit_expiry_bypass_and_stats(self):
        first, state = self.get()
        self.assertEqual(state, 'MISS')
        second, state = self.get()
        self.assertEqual((second, state), (first, 'HIT'))
        self.now += 1
        self.assertEqual(self.get()[1], 'MISS')
        self.assertEqual(self.get('/products/1?cache=0')[1], 'BYPASS')
        stats, _ = self.get('/stats')
        self.assertEqual(stats['origin_reads'], 3)
        self.assertEqual(stats['hits'], 1)

    def test_update_invalidates_and_returns_fresh_value(self):
        self.get()
        old_key = self.catalog.key(self.catalog.products[1])
        request = Request(self.root + '/products/1', data=json.dumps({'name': '새 상품', 'price': 123}).encode(), method='PUT', headers={'Content-Type': 'application/json'})
        with urlopen(request, timeout=3) as response:
            self.assertEqual(response.status, 200)
        self.assertIsNone(self.catalog.client.execute('GET', old_key))
        body, state = self.get()
        self.assertEqual((body['name'], body['price'], state), ('새 상품', 123, 'MISS'))

    def test_concurrent_misses_only_fetch_origin_once(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            responses = list(pool.map(lambda _: self.get(), range(8)))
        self.assertEqual(sum(state == 'MISS' for _, state in responses), 1)
        self.assertEqual(self.catalog.counters['origin_reads'], 1)

    def test_http_validation(self):
        for path in ('/products/999', '/unknown'):
            with self.assertRaises(HTTPError) as error:
                self.get(path)
            self.assertEqual(error.exception.code, 404)
        for data in (b'{}', b'{', b'{"price": -1}', b'{"price": true}', b'{"price": NaN}', b'{"name": ""}'):
            request = Request(self.root + '/products/1', data=data, method='PUT')
            with self.assertRaises(HTTPError) as error:
                urlopen(request, timeout=3)
            self.assertEqual(error.exception.code, 400)

    def test_cache_outage_fallback_and_versioned_invalidation(self):
        from unittest.mock import patch
        self.get()
        with patch.object(self.catalog.client, 'execute', side_effect=ConnectionError('offline')):
            self.catalog.update(1, {'price': 42})
            body, state = self.get()
            self.assertEqual((body['price'], state), (42, 'ERROR'))
        body, state = self.get()
        self.assertEqual((body['price'], state), (42, 'MISS'))

    def command(self, args):
        request = Request(self.root + '/api/command', data=json.dumps({'args': args}).encode(),
                          headers={'Content-Type': 'application/json'}, method='POST')
        with urlopen(request, timeout=3) as response:
            return json.load(response)

    def test_gui_assets_and_status(self):
        for path, content_type in (('/', 'text/html'), ('/app.js', 'text/javascript'), ('/style.css', 'text/css')):
            with urlopen(self.root + path, timeout=3) as response:
                self.assertIn(content_type, response.headers['Content-Type'])
                self.assertIn("script-src 'self'", response.headers['Content-Security-Policy'])
                self.assertGreater(len(response.read()), 100)
        status, _ = self.get('/api/status')
        self.assertTrue(status['connected'])
        self.assertEqual(status['keys'], 0)

    def test_gui_command_crud_ttl_and_binary(self):
        self.assertEqual(self.command(['SET', '화면', '', 'EX', '1'])['value'], 'OK')
        self.assertEqual(self.command(['GET', '화면'])['value'], '')
        self.assertEqual(self.command(['PTTL', '화면'])['value'], 1000)
        self.now += 1
        self.assertEqual(self.command(['GET', '화면'])['type'], 'null')
        self.command(['SET', 'x', 'y'])
        self.assertEqual(self.command(['DEL', 'x'])['value'], 1)
        self.catalog.client.execute('SET', 'binary', b'\xff')
        self.assertEqual(self.command(['GET', 'binary'])['encoding'], 'base64')

    def test_gui_bad_command_and_cross_origin_rejected(self):
        for args in ([], ['WHAT'], ['SET', 'a', 'b', 'EX', '-1'], [1]):
            with self.assertRaises(HTTPError) as error:
                self.command(args)
            self.assertEqual(error.exception.code, 400)
        request = Request(self.root + '/api/command', data=b'{"args":["SET","a","b"]}',
                          headers={'Content-Type': 'application/json', 'Origin': 'https://example.com'}, method='POST')
        with self.assertRaises(HTTPError) as error:
            urlopen(request, timeout=3)
        self.assertEqual(error.exception.code, 400)
        self.assertIsNone(self.catalog.client.execute('GET', 'a'))

    def test_gui_offline_returns_503(self):
        from unittest.mock import patch
        with patch.object(self.catalog.client, 'execute', side_effect=ConnectionError('offline')):
            for action in (lambda: self.get('/api/status'), lambda: self.command(['GET', 'a'])):
                with self.assertRaises(HTTPError) as error:
                    action()
                self.assertEqual(error.exception.code, 503)
