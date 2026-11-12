import argparse
import logging
from pathlib import Path
import re
import signal
import socketserver
import threading

from .protocol import ProtocolError, Simple, encode, read_request
from .store import Store


def integer(value):
    if len(value) > 20 or not re.fullmatch(rb'-?[0-9]+', value):
        raise ValueError('value is not an integer or out of range')
    result = int(value)
    if not -(2**63) <= result < 2**63:
        raise ValueError('value is not an integer or out of range')
    return result


def dispatch(store, args, snapshot=None):
    command, *params = args
    command = command.upper()
    n = len(params)
    if command == b'PING' and n <= 1:
        return params[0] if params else Simple('PONG')
    if command == b'ECHO' and n == 1:
        return params[0]
    if command == b'SET' and n in (2, 4):
        ttl = None
        if n == 4:
            option = params[2].upper()
            if option not in (b'EX', b'PX'):
                raise ValueError('unsupported SET option')
            ttl = integer(params[3]) * (1000 if option == b'EX' else 1)
        store.set(params[0], params[1], ttl)
        return Simple('OK')
    if command == b'GET' and n == 1:
        return store.get(params[0])
    if command == b'DEL' and n >= 1:
        return store.delete(*params)
    if command == b'EXISTS' and n >= 1:
        return store.exists(*params)
    if command in (b'EXPIRE', b'PEXPIRE') and n == 2:
        return store.expire(params[0], integer(params[1]) * (1000 if command == b'EXPIRE' else 1))
    if command in (b'TTL', b'PTTL') and n == 1:
        return store.ttl(params[0], command == b'PTTL')
    if command == b'PERSIST' and n == 1:
        return store.persist(params[0])
    if command == b'INCR' and n == 1:
        return store.incr(params[0])
    if command == b'DBSIZE' and n == 0:
        return store.dbsize()
    if command == b'SAVE' and n == 0:
        if not snapshot:
            raise ValueError('persistence disabled; configure --snapshot')
        store.save(snapshot)
        return Simple('OK')
    raise ValueError('unknown command or wrong number of arguments')


class Handler(socketserver.StreamRequestHandler):
    def setup(self):
        self.request.settimeout(self.server.client_timeout)
        super().setup()

    def handle(self):
        while not self.server.stopping.is_set():
            try:
                args = read_request(self.rfile)
                if args is None:
                    return
                try:
                    result = dispatch(self.server.store, args, self.server.snapshot)
                except ValueError as error:
                    result = error
                except OSError:
                    logging.exception('snapshot write failed')
                    result = ValueError('snapshot write failed')
                self.wfile.write(encode(result))
            except ProtocolError as error:
                try:
                    self.wfile.write(encode(error))
                except OSError:
                    pass
                return
            except (OSError, ConnectionError):
                return


class RedisServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = False
    request_queue_size = 128

    def __init__(self, address, store=None, snapshot=None, max_clients=128, client_timeout=10):
        self.store = store if store is not None else Store()
        self.snapshot = snapshot
        self.client_timeout = client_timeout
        self.slots = threading.BoundedSemaphore(max_clients)
        self.stopping = threading.Event()
        self.maintenance = None
        super().__init__(address, Handler)
        self.maintenance = threading.Thread(target=self._maintenance, daemon=True)
        self.maintenance.start()

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            try:
                request.settimeout(0.1)
                request.sendall(encode(ValueError('max clients reached')))
            except OSError:
                pass
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def _maintenance(self):
        while not self.stopping.wait(0.1):
            self.store.sweep()

    def server_close(self):
        self.stopping.set()
        super().server_close()
        if self.maintenance is not None:
            self.maintenance.join()


def main():
    parser = argparse.ArgumentParser(description='Mini Redis RESP2 server')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=6379)
    parser.add_argument('--snapshot', help='load on startup; SAVE and graceful shutdown write here')
    parser.add_argument('--max-clients', type=int, default=128)
    args = parser.parse_args()
    if args.max_clients < 1:
        parser.error('--max-clients must be positive')
    store = Store()
    if args.snapshot and Path(args.snapshot).exists():
        store.load(args.snapshot)
    server = RedisServer((args.host, args.port), store, args.snapshot, args.max_clients)

    def stop(*_):
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    print(f'Mini Redis listening on {args.host}:{server.server_address[1]}', flush=True)
    try:
        server.serve_forever(poll_interval=0.1)
    finally:
        server.server_close()
        if args.snapshot:
            store.save(args.snapshot)


if __name__ == '__main__':
    main()
