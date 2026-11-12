"""Separate chaining with FNV-1a hashing; no built-in mapping stores user data."""
from dataclasses import dataclass


@dataclass
class Entry:
    key: bytes
    value: bytes
    expires_at: float | None = None
    next: 'Entry | None' = None


class HashTable:
    def __init__(self, capacity=16):
        self.buckets = [None] * max(4, capacity)
        self.size = 0

    @staticmethod
    def hash_key(key):
        result = 14695981039346656037
        for byte in key:
            result = ((result ^ byte) * 1099511628211) & 0xffffffffffffffff
        return result

    def get(self, key):
        node = self.buckets[self.hash_key(key) % len(self.buckets)]
        while node:
            if node.key == key:
                return node
            node = node.next
        return None

    def set(self, key, value, expires_at=None):
        node = self.get(key)
        if node:
            node.value, node.expires_at = value, expires_at
            return
        index = self.hash_key(key) % len(self.buckets)
        self.buckets[index] = Entry(key, value, expires_at, self.buckets[index])
        self.size += 1
        if self.size > len(self.buckets) * 0.75:
            self._resize()

    def delete(self, key):
        index = self.hash_key(key) % len(self.buckets)
        previous, node = None, self.buckets[index]
        while node:
            if node.key == key:
                if previous:
                    previous.next = node.next
                else:
                    self.buckets[index] = node.next
                self.size -= 1
                return True
            previous, node = node, node.next
        return False

    def entries(self):
        for node in self.buckets:
            while node:
                yield node
                node = node.next

    def _resize(self):
        old = self.buckets
        self.buckets = [None] * (len(old) * 2)
        for node in old:
            while node:
                following = node.next
                index = self.hash_key(node.key) % len(self.buckets)
                node.next = self.buckets[index]
                self.buckets[index] = node
                node = following
