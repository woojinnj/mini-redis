"""Start the browser console and its Redis server with one command."""
import argparse
from pathlib import Path
import signal
import threading

from .client import Client
from .demo import Catalog, DemoServer
from .server import RedisServer
from .store import Store


def main():
    parser = argparse.ArgumentParser(description='Mini Redis browser console + TCP server')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8080)
    parser.add_argument('--redis-port', type=int, default=6379)
    parser.add_argument('--snapshot', default='data/snapshot.json')
    args = parser.parse_args()
    store = Store()
    if Path(args.snapshot).exists():
        store.load(args.snapshot)
    try:
        redis = RedisServer(('127.0.0.1', args.redis_port), store, args.snapshot)
    except OSError as error:
        parser.exit(1, f'Redis port unavailable: {error}. Stop the existing server or choose --redis-port.\n')
    try:
        api = DemoServer((args.host, args.port), Catalog(Client(port=redis.server_address[1])))
    except OSError as error:
        redis.server_close()
        parser.exit(1, f'HTTP port unavailable: {error}. Choose --port or open the existing demo server.\n')
    thread = threading.Thread(target=redis.serve_forever, kwargs={'poll_interval': 0.1}, daemon=True)
    thread.start()

    def stop(*_):
        threading.Thread(target=api.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    print(f'Mini Redis GUI: http://{args.host}:{api.server_port} (TCP {redis.server_address[1]})', flush=True)
    try:
        api.serve_forever(poll_interval=0.1)
    finally:
        api.server_close()
        redis.shutdown()
        redis.server_close()
        thread.join()
        store.save(args.snapshot)


if __name__ == '__main__':
    main()
