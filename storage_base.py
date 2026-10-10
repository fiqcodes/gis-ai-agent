"""Shared, expiring conversation state and atomic usage limits.

Production uses Upstash's REST API. MemoryStore is only for local development
and tests; it is never silently selected on Vercel.
"""
import json
import os
import secrets
import threading
import time
from contextlib import contextmanager

import requests


class StorageUnavailable(RuntimeError):
    pass


class ConversationBusy(RuntimeError):
    pass


class RedisStore:
    def __init__(self, url, token, prefix="orbiview:production", ttl=86400):
        if not url.startswith("https://"):
            raise ValueError("Redis REST URL must use HTTPS")
        self.url = url.rstrip("/")
        self.token = token
        self.prefix = prefix
        self.ttl = ttl

    def command(self, *args):
        try:
            response = requests.post(
                self.url, json=list(args),
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=(3, 5), allow_redirects=False,
            )
            response.raise_for_status()
            payload = response.json()
            if "error" in payload or "result" not in payload:
                raise ValueError("Redis command failed")
            return payload["result"]
        except (requests.RequestException, ValueError) as exc:
            # Do not include credentials, request bodies, or provider errors.
            raise StorageUnavailable("Conversation storage is unavailable") from exc

    def key(self, suffix):
        return f"{self.prefix}:{suffix}"

    def load(self, sid, cid):
        value = self.command("HGET", self.key(f"history:{sid}"), cid)
        return json.loads(value) if value else []

    def save(self, sid, cid, history):
        script = """
        if redis.call('HEXISTS', KEYS[1], ARGV[1]) == 0 and
           redis.call('HLEN', KEYS[1]) >= 30 then return 0 end
        redis.call('HSET', KEYS[1], ARGV[1], ARGV[2])
        redis.call('EXPIRE', KEYS[1], ARGV[3])
        return 1
        """
        if not self.command("EVAL", script, 1, self.key(f"history:{sid}"),
                            cid, json.dumps(history[-20:]), self.ttl):
            raise StorageUnavailable("Clear your history before starting more conversations")

    def clear(self, sid):
        self.command("DEL", self.key(f"history:{sid}"))

    def allow(self, key, limit, seconds):
        # Atomic fixed windows, with TTL set in the same operation as INCR.
        bucket = int(time.time()) // seconds
        script = """
        local n = redis.call('INCR', KEYS[1])
        if n == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end
        return n
        """
        count = self.command("EVAL", script, 1,
                             self.key(f"rate:{key}:{bucket}"), seconds + 1)
        return int(count) <= limit

    @contextmanager
    def lock(self, sid):
        key = self.key(f"lock:{sid}")
        owner = secrets.token_hex(16)
        if not self.command("SET", key, owner, "NX", "EX", 350):
            raise ConversationBusy("Please wait for your current request to finish")
        try:
            yield
        finally:
            script = """
            if redis.call('GET', KEYS[1]) == ARGV[1] then
                return redis.call('DEL', KEYS[1]) end
            return 0
            """
            self.command("EVAL", script, 1, key, owner)


class MemoryStore:
    """Single-process local development substitute, never production storage."""
    def __init__(self, ttl=86400):
        self.ttl = ttl
        self.data = {}
        self.rates = {}
        self.busy = set()
        self.mutex = threading.RLock()

    def load(self, sid, cid):
        with self.mutex:
            history, expiry = self.data.get((sid, cid), ([], 0))
            return json.loads(json.dumps(history)) if expiry > time.time() else []

    def save(self, sid, cid, history):
        with self.mutex:
            self.data[(sid, cid)] = (json.loads(json.dumps(history[-20:])), time.time() + self.ttl)

    def clear(self, sid):
        with self.mutex:
            self.data = {k: v for k, v in self.data.items() if k[0] != sid}

    def allow(self, key, limit, seconds):
        with self.mutex:
            now = time.time()
            self.rates = {k: v for k, v in self.rates.items() if v[1] > now}
            bucket = (key, int(now) // seconds)
            count = self.rates.get(bucket, (0, 0))[0] + 1
            self.rates[bucket] = (count, now + seconds)
            return count <= limit

    @contextmanager
    def lock(self, sid):
        with self.mutex:
            if sid in self.busy:
                raise ConversationBusy("Please wait for your current request to finish")
            self.busy.add(sid)
        try:
            yield
        finally:
            with self.mutex:
                self.busy.discard(sid)

