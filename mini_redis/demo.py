"""Example HTTP API using Mini Redis to cache serialized product responses."""
import argparse
import base64
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
from pathlib import Path
import threading
import time
from urllib.parse import parse_qs, urlsplit
import uuid

from .client import Client
from .protocol import ProtocolError, RedisError

CACHE_ERRORS = (OSError, ConnectionError, ProtocolError, RedisError)


@dataclass
class Product:
    id: int
    name: str
    price: float
    version: int = 1
    lock: threading.RLock = field(default_factory=threading.RLock)

    def body(self):
        return {'id': self.id, 'name': self.name, 'price': self.price}


class Catalog:
    def __init__(self, client, delay=0.05, ttl_ms=5000):
        self.client, self.delay, self.ttl_ms = client, delay, ttl_ms
        self.namespace = uuid.uuid4().hex
        self.products = {i: Product(i, f'Product {i}', i * 1000) for i in range(1, 4)}
        self.stats_lock = threading.Lock()
        self.counters = dict(origin_reads=0, hits=0, misses=0, bypasses=0, cache_errors=0)

    def count(self, name):
        with self.stats_lock:
            self.counters[name] += 1

    def key(self, product):
        return f'demo:{self.namespace}:product:{product.id}:v{product.version}'

    def origin(self, body):
        self.count('origin_reads')
        time.sleep(self.delay)  # Explicit simulated upstream latency, not a real DB.
        return json.dumps(body, ensure_ascii=False, allow_nan=False).encode()

    def get(self, product_id, cached=True):
        product = self.products[product_id]
        if not cached:
            with product.lock:
                body = product.body()
            self.count('bypasses')
            return self.origin(body), 'BYPASS'
        # One producer per key. Updates use this same lock, preventing stale refills.
        with product.lock:
            key = self.key(product)
            try:
                value = self.client.execute('GET', key)
                if value is not None:
                    self.count('hits')
                    return value, 'HIT'
            except CACHE_ERRORS:
                self.count('cache_errors')
                return self.origin(product.body()), 'ERROR'
            self.count('misses')
            value = self.origin(product.body())
            try:
                self.client.execute('SET', key, value, 'PX', self.ttl_ms)
            except CACHE_ERRORS:
                self.count('cache_errors')
                return value, 'ERROR'
            return value, 'MISS'

    def update(self, product_id, changes):
        if not isinstance(changes, dict) or not changes or set(changes) - {'name', 'price'}:
            raise ValueError('provide name and/or price')
        if 'name' in changes and (not isinstance(changes['name'], str) or not 1 <= len(changes['name']) <= 200):
            raise ValueError('name must contain 1..200 characters')
        if 'price' in changes and (type(changes['price']) not in (int, float) or not math.isfinite(changes['price']) or not 0 <= changes['price'] <= 10**12):
            raise ValueError('price must be a finite number in 0..1000000000000')
        product = self.products[product_id]
        with product.lock:
            old_key = self.key(product)
            for name, value in changes.items():
                setattr(product, name, value)
            product.version += 1
            try:
                self.client.execute('DEL', old_key)
            except CACHE_ERRORS:
                # The new version never reads the old key, even if deletion fails.
                self.count('cache_errors')
            return product.body()


class DemoHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def setup(self):
        super().setup()
        self.connection.settimeout(5)

    def respond(self, status, body, cache=None):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        if cache:
            self.send_header('X-Cache', cache)
        self.end_headers()
        self.wfile.write(data)

    def product_id(self):
        parts = urlsplit(self.path).path.split('/')
        if len(parts) != 3 or parts[1] != 'products' or not parts[2].isdigit():
            raise KeyError('not found')
        number = int(parts[2])
        if number not in self.server.catalog.products:
            raise KeyError('not found')
        return number

    def do_GET(self):
        path = urlsplit(self.path)
        if path.path in ('/', '/app.js', '/style.css'):
            name = {'/': 'index.html', '/app.js': 'app.js', '/style.css': 'style.css'}[path.path]
            data = (Path(__file__).parent / 'static' / name).read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', {'/': 'text/html; charset=utf-8', '/app.js': 'text/javascript; charset=utf-8', '/style.css': 'text/css; charset=utf-8'}[path.path])
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-cache')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(data)
            return
        if path.path == '/api/status':
            try:
                start = time.perf_counter()
                self.server.catalog.client.execute('PING')
                size = self.server.catalog.client.execute('DBSIZE')
                self.respond(200, {'connected': True, 'keys': size, 'latency_ms': round((time.perf_counter() - start) * 1000, 2)})
            except CACHE_ERRORS:
                self.respond(503, {'connected': False, 'error': 'Mini Redis 서버에 연결할 수 없습니다.'})
            return
        if path.path == '/health':
            self.respond(200, {'status': 'ok'})
            return
        if path.path == '/stats':
            with self.server.catalog.stats_lock:
                stats = dict(self.server.catalog.counters)
            self.respond(200, stats)
            return
        try:
            product_id = self.product_id()
            mode = parse_qs(path.query).get('cache', ['1'])
            if mode not in (['0'], ['1']):
                raise ValueError('cache must be 0 or 1')
            body, state = self.server.catalog.get(product_id, mode == ['1'])
            self.respond(200, body, state)
        except KeyError:
            self.respond(404, {'error': 'product not found'})
        except ValueError as error:
            self.respond(400, {'error': str(error)})

    def read_json(self):
        origin = self.headers.get('Origin')
        if origin and urlsplit(origin).netloc != self.headers.get('Host'):
            raise ValueError('cross-origin writes are not allowed')
        if self.headers.get('Transfer-Encoding'):
            raise ValueError('transfer encoding is unsupported')
        size = int(self.headers.get('Content-Length', '0'))
        if not 1 <= size <= 65536:
            raise ValueError('body must be 1..65536 bytes')
        raw = self.rfile.read(size)
        if len(raw) != size:
            raise ValueError('incomplete body')
        return json.loads(raw)

    def do_POST(self):
        if urlsplit(self.path).path != '/api/command':
            self.respond(404, {'error': 'not found'})
            return
        try:
            if self.headers.get_content_type() != 'application/json':
                raise ValueError('application/json required')
            body = self.read_json()
            args = body.get('args') if isinstance(body, dict) else None
            if not isinstance(args, list) or not 1 <= len(args) <= 64 or not all(isinstance(arg, str) for arg in args):
                raise ValueError('args must be an array of strings')
            if args[0].upper() not in {'SET', 'GET', 'DEL', 'EXISTS', 'EXPIRE', 'PEXPIRE', 'TTL', 'PTTL', 'PERSIST', 'INCR', 'PING', 'DBSIZE', 'SAVE'}:
                raise ValueError('unsupported command')
            start = time.perf_counter()
            value = self.server.catalog.client.execute(*args)
            elapsed = round((time.perf_counter() - start) * 1000, 2)
            kind = 'null' if value is None else 'integer' if isinstance(value, int) else 'string'
            encoding = 'utf-8'
            if isinstance(value, bytes):
                try:
                    value = value.decode('utf-8')
                except UnicodeDecodeError:
                    value, encoding = base64.b64encode(value).decode(), 'base64'
            self.respond(200, {'value': value, 'type': kind, 'encoding': encoding, 'elapsed_ms': elapsed})
        except (ValueError, RedisError) as error:
            self.respond(400, {'error': str(error)})
        except CACHE_ERRORS:
            self.respond(503, {'error': 'Mini Redis 서버에 연결할 수 없습니다.'})

    def do_PUT(self):
        try:
            product_id = self.product_id()
            changes = self.read_json()
            result = self.server.catalog.update(product_id, changes)
            self.respond(200, result)
        except KeyError:
            self.respond(404, {'error': 'product not found'})
        except (ValueError, OverflowError, UnicodeError) as error:
            self.respond(400, {'error': str(error)})


class DemoServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 128

    def __init__(self, address, catalog):
        self.catalog = catalog
        super().__init__(address, DemoHandler)


def main():
    parser = argparse.ArgumentParser(description='Product API backed by Mini Redis')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8080)
    parser.add_argument('--redis-host', default='127.0.0.1')
    parser.add_argument('--redis-port', type=int, default=6379)
    parser.add_argument('--origin-delay-ms', type=float, default=50)
    parser.add_argument('--ttl-ms', type=int, default=5000)
    args = parser.parse_args()
    if not math.isfinite(args.origin_delay_ms) or args.origin_delay_ms < 0 or args.ttl_ms < 1:
        parser.error('delay must be nonnegative and TTL must be positive')
    catalog = Catalog(Client(args.redis_host, args.redis_port), args.origin_delay_ms / 1000, args.ttl_ms)
    with DemoServer((args.host, args.port), catalog) as server:
        print(f'Demo API listening on http://{args.host}:{server.server_port}', flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == '__main__':
    main()
