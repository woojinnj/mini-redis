"""Atomic commands, passive/active expiry, and atomic disk snapshots."""
import base64
import json
import math
import os
from pathlib import Path
import re
import tempfile
import threading
import time

from .hash_table import HashTable


class Store:
    def __init__(self, clock=time.time):
        self.table = HashTable()
        self.clock = clock
        self.lock = threading.RLock()
        self.snapshot_lock = threading.Lock()
        self.sweep_cursor = 0

    def _live(self, key):
        node = self.table.get(key)
        if node and node.expires_at is not None and self.clock() >= node.expires_at:
            self.table.delete(key)
            return None
        return node

    def get(self, key):
        with self.lock:
            node = self._live(key)
            return node.value if node else None

    def set(self, key, value, ttl_ms=None):
        if ttl_ms is not None and ttl_ms <= 0:
            raise ValueError('invalid expire time in SET')
        with self.lock:
            expires = None if ttl_ms is None else self.clock() + ttl_ms / 1000
            self.table.set(key, value, expires)

    def delete(self, *keys):
        with self.lock:
            count = 0
            for key in keys:
                if self._live(key):
                    count += self.table.delete(key)
            return count

    def exists(self, *keys):
        with self.lock:
            return sum(self._live(key) is not None for key in keys)

    def expire(self, key, ttl_ms):
        with self.lock:
            node = self._live(key)
            if node is None:
                return 0
            if ttl_ms <= 0:
                self.table.delete(key)
            else:
                node.expires_at = self.clock() + ttl_ms / 1000
            return 1

    def ttl(self, key, milliseconds=False):
        with self.lock:
            node = self._live(key)
            if node is None:
                return -2
            if node.expires_at is None:
                return -1
            remaining = max(0, node.expires_at - self.clock())
            return int(remaining * (1000 if milliseconds else 1))

    def persist(self, key):
        with self.lock:
            node = self._live(key)
            if node is None or node.expires_at is None:
                return 0
            node.expires_at = None
            return 1

    def incr(self, key):
        with self.lock:
            node = self._live(key)
            raw = node.value if node else b'0'
            if not re.fullmatch(rb'-?(0|[1-9][0-9]*)', raw) or len(raw) > 20:
                raise ValueError('value is not an integer or out of range')
            number = int(raw)
            if not -(2**63) <= number < 2**63 - 1:
                raise ValueError('increment would overflow')
            self.table.set(key, str(number + 1).encode(), node.expires_at if node else None)
            return number + 1

    def sweep(self, bucket_budget=32):
        with self.lock:
            now = self.clock()
            for _ in range(min(bucket_budget, len(self.table.buckets))):
                index = self.sweep_cursor % len(self.table.buckets)
                node = self.table.buckets[index]
                while node:
                    following = node.next
                    if node.expires_at is not None and now >= node.expires_at:
                        self.table.delete(node.key)
                    node = following
                self.sweep_cursor = index + 1

    def dbsize(self):
        with self.lock:
            self.sweep(len(self.table.buckets))
            return self.table.size

    def save(self, filename):
        # Serialize saves so an older snapshot cannot overwrite a newer one.
        with self.snapshot_lock:
            with self.lock:
                now = self.clock()
                records = [[base64.b64encode(n.key).decode(), base64.b64encode(n.value).decode(), n.expires_at]
                           for n in self.table.entries()
                           if n.expires_at is None or n.expires_at > now]
            path = Path(filename)
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = None
            try:
                with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as stream:
                    temp = stream.name
                    json.dump({'version': 1, 'records': records}, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temp, path)
                directory = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            finally:
                if temp and os.path.exists(temp):
                    os.unlink(temp)

    def load(self, filename):
        # Startup only: validate fully before installing the new table.
        document = json.loads(Path(filename).read_text())
        if not isinstance(document, dict) or document.get('version') != 1 or not isinstance(document.get('records'), list):
            raise ValueError('invalid snapshot format')
        table = HashTable()
        now = self.clock()
        for record in document['records']:
            if not isinstance(record, list) or len(record) != 3:
                raise ValueError('invalid snapshot record')
            key, value, expires = record
            if expires is not None and (type(expires) not in (int, float) or not math.isfinite(expires)):
                raise ValueError('invalid snapshot expiry')
            key, value = base64.b64decode(key, validate=True), base64.b64decode(value, validate=True)
            if expires is None or expires > now:
                table.set(key, value, expires)
        with self.lock:
            self.table = table
