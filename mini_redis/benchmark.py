"""Reproducible, real HTTP/TCP benchmark; starts isolated servers on free ports."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import math
import platform
import statistics
import threading
import time
from urllib.request import urlopen

from .client import Client
from .demo import Catalog, DemoServer
from .server import RedisServer


@contextmanager
def running(server):
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def benchmark(requests=100, concurrency=1, delay_ms=20):
    with running(RedisServer(('127.0.0.1', 0))) as redis:
        catalog = Catalog(Client(port=redis.server_address[1]), delay_ms / 1000, 3600000)
        with running(DemoServer(('127.0.0.1', 0), catalog)) as api:
            root = f'http://127.0.0.1:{api.server_port}'
            results = []
            for mode in ('no_cache', 'cache_miss', 'cache_hit'):
                # Cold runs are sequential so key deletion cannot race a request.
                workers = 1 if mode == 'cache_miss' else concurrency
                if mode == 'cache_hit':
                    catalog.get(1)
                before = catalog.counters['origin_reads']

                def request(_):
                    if mode == 'cache_miss':
                        catalog.client.execute('DEL', catalog.key(catalog.products[1]))
                    start = time.perf_counter()
                    try:
                        with urlopen(root + '/products/1?cache=' + ('0' if mode == 'no_cache' else '1'), timeout=10) as response:
                            body = json.load(response)
                            state = response.headers['X-Cache']
                            assert body == {'id': 1, 'name': 'Product 1', 'price': 1000}
                            assert state == {'no_cache': 'BYPASS', 'cache_miss': 'MISS', 'cache_hit': 'HIT'}[mode]
                        return (time.perf_counter() - start) * 1000, False
                    except Exception:
                        return (time.perf_counter() - start) * 1000, True

                start = time.perf_counter()
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    observations = list(pool.map(request, range(requests)))
                elapsed = time.perf_counter() - start
                latency = sorted(value for value, _ in observations)
                results.append(dict(mode=mode, requests=requests, concurrency=workers,
                                    mean_ms=round(statistics.mean(latency), 3),
                                    p50_ms=round(statistics.median(latency), 3),
                                    p95_ms=round(latency[math.ceil(len(latency) * .95) - 1], 3),
                                    requests_per_second=round(requests / elapsed, 2),
                                    errors=sum(error for _, error in observations),
                                    origin_reads=catalog.counters['origin_reads'] - before))
            return dict(python=platform.python_version(), platform=platform.platform(),
                        simulated_origin_delay_ms=delay_ms, results=results,
                        notes='Cold runs use concurrency=1; throughput includes cold-key deletion setup. Latencies exclude that setup. Hot runs are prewarmed. No real database is used.')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--requests', type=int, default=100)
    parser.add_argument('--concurrency', type=int, default=1)
    parser.add_argument('--delay-ms', type=float, default=20)
    args = parser.parse_args()
    if args.requests < 1 or not 1 <= args.concurrency <= 64 or not math.isfinite(args.delay_ms) or args.delay_ms < 0:
        parser.error('requests > 0, concurrency in 1..64, finite delay >= 0 required')
    report = benchmark(args.requests, args.concurrency, args.delay_ms)
    print(json.dumps(report, indent=2))
    if any(result['errors'] for result in report['results']):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
