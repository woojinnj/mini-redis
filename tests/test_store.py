from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import random
import tempfile
import unittest
from unittest.mock import patch

from mini_redis.hash_table import HashTable
from mini_redis.store import Store


class HashTableTests(unittest.TestCase):
    def test_collisions_and_delete_chain_positions(self):
        table = HashTable()
        table.hash_key = lambda _: 0
        for key in (b'a', b'b', b'c'):
            table.set(key, key)
        self.assertTrue(table.delete(b'b'))
        self.assertEqual(table.get(b'a').value, b'a')
        self.assertEqual(table.get(b'c').value, b'c')
        self.assertTrue(table.delete(b'c'))
        self.assertTrue(table.delete(b'a'))
        self.assertFalse(table.delete(b'a'))
        self.assertEqual(table.size, 0)

    def test_resize_and_overwrite(self):
        table = HashTable(4)
        for i in range(1000):
            table.set(str(i).encode(), b'value', 123)
        self.assertGreater(len(table.buckets), 4)
        for i in range(1000):
            node = table.get(str(i).encode())
            self.assertEqual((node.value, node.expires_at), (b'value', 123))
        table.set(b'1', b'new')
        self.assertEqual(table.size, 1000)
        self.assertIsNone(table.get(b'1').expires_at)

    def test_randomized_reference_model(self):
        rng, table, expected = random.Random(42), HashTable(), {}
        for _ in range(5000):
            key, value = str(rng.randrange(200)).encode(), rng.randbytes(8)
            op = rng.randrange(3)
            if op == 0:
                table.set(key, value)
                expected[key] = value
            elif op == 1:
                self.assertEqual(table.delete(key), key in expected)
                expected.pop(key, None)
            else:
                entry = table.get(key)
                self.assertEqual(entry.value if entry else None, expected.get(key))
            self.assertEqual(table.size, len(expected))


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        self.store = Store(lambda: self.now)

    def test_empty_and_missing(self):
        self.store.set(b'', b'')
        self.assertEqual(self.store.get(b''), b'')
        self.assertIsNone(self.store.get(b'missing'))
        self.assertEqual(self.store.delete(b'', b'', b'missing'), 1)

    def test_expiration_boundary(self):
        self.store.set(b'a', b'b', 1000)
        self.now = 1000.999
        self.assertEqual(self.store.get(b'a'), b'b')
        self.now = 1001
        self.assertIsNone(self.store.get(b'a'))
        self.assertEqual(self.store.ttl(b'a'), -2)
        self.assertEqual(self.store.table.size, 0)

    def test_sweep_reclaims_unread_entries(self):
        for i in range(100):
            self.store.set(str(i).encode(), b'x', 100)
        self.now += 1
        for _ in range(20):
            self.store.sweep()
        self.assertEqual(self.store.table.size, 0)

    def test_ttl_persist_overwrite_and_nonpositive(self):
        self.assertEqual(self.store.expire(b'a', 0), 0)
        self.store.set(b'a', b'x', 10000)
        self.assertEqual(self.store.ttl(b'a'), 10)
        self.assertEqual(self.store.ttl(b'a', True), 10000)
        self.assertEqual(self.store.persist(b'a'), 1)
        self.assertEqual(self.store.persist(b'a'), 0)
        self.assertEqual(self.store.ttl(b'a'), -1)
        self.store.expire(b'a', 1000)
        self.store.set(b'a', b'new')
        self.now += 2
        self.assertEqual(self.store.get(b'a'), b'new')
        self.assertEqual(self.store.expire(b'a', -1), 1)
        self.assertIsNone(self.store.get(b'a'))
        for ttl in (0, -1):
            with self.assertRaises(ValueError):
                self.store.set(b'a', b'b', ttl)

    def test_atomic_increment(self):
        self.store.set(b'counter', b'0', 10000)
        def worker(_):
            for _ in range(1000):
                self.store.incr(b'counter')
        with ThreadPoolExecutor(max_workers=16) as pool:
            list(pool.map(worker, range(16)))
        self.assertEqual(self.store.get(b'counter'), b'16000')
        self.assertEqual(self.store.ttl(b'counter'), 10)

    def test_integer_errors_do_not_mutate(self):
        for value in (b'1.0', b'', b'abc', b'9223372036854775807', b'-9223372036854775809'):
            self.store.set(b'n', value)
            with self.assertRaises(ValueError):
                self.store.incr(b'n')
            self.assertEqual(self.store.get(b'n'), value)

    def test_snapshot_restores_binary_and_original_deadlines(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'dump.json'
            self.store.set(b'\xff\x00', b'\r\n\x80')
            self.store.set(b'expired', b'x', 100)
            self.store.set(b'live', b'x', 10000)
            self.store.save(path)
            self.now += 1
            restored = Store(lambda: self.now)
            restored.load(path)
            self.assertEqual(restored.get(b'\xff\x00'), b'\r\n\x80')
            self.assertIsNone(restored.get(b'expired'))
            self.assertEqual(restored.ttl(b'live'), 9)

    def test_failed_save_keeps_previous_snapshot(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'dump.json'
            self.store.set(b'k', b'old')
            self.store.save(path)
            before = path.read_bytes()
            self.store.set(b'k', b'new')
            with patch('mini_redis.store.os.replace', side_effect=OSError('disk failure')):
                with self.assertRaises(OSError):
                    self.store.save(path)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(len(list(Path(folder).iterdir())), 1)

    def test_corrupt_load_does_not_replace_store(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'dump.json'
            self.store.set(b'k', b'old')
            for content in ('{', json.dumps({'version': 1, 'records': [['aw==', 'dg==', float('nan')]]})):
                path.write_text(content)
                with self.assertRaises(ValueError):
                    self.store.load(path)
                self.assertEqual(self.store.get(b'k'), b'old')
