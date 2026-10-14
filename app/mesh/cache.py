import logging
import math
import threading
import time
from abc import ABC, abstractmethod
from collections import defaultdict
from typing import Callable, Dict, Iterable, Optional, Set, Tuple

import redis

logger = logging.getLogger(__name__)


class Cache(ABC):
    """Tag-aware string cache. ``InMemoryCache`` lives in one process; ``RedisCache`` is
    shared by every process and node, so an invalidation reaches all of them."""

    @abstractmethod
    def get(self, key: str) -> Optional[str]: ...

    @abstractmethod
    def set(self, key: str, value: str, *, tags: Iterable[str], ttl_seconds: float) -> None: ...

    @abstractmethod
    def invalidate_tags(self, tags: Iterable[str]) -> None: ...

    @abstractmethod
    def clear(self) -> None: ...


class InMemoryCache(Cache):
    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: Dict[str, Tuple[float, str, Tuple[str, ...]]] = {}
        self._keys_by_tag: Dict[str, Set[str]] = defaultdict(set)
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> Optional[str]:
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None and entry[0] <= self._clock():
                self._discard(key)
                entry = None
            if entry is None:
                self.misses += 1
                return None
            self.hits += 1
            return entry[1]

    def set(self, key: str, value: str, *, tags: Iterable[str], ttl_seconds: float) -> None:
        tags = tuple(tags)
        with self._lock:
            self._discard(key)
            self._entries[key] = (self._clock() + ttl_seconds, value, tags)
            for tag in tags:
                self._keys_by_tag[tag].add(key)

    def invalidate_tags(self, tags: Iterable[str]) -> None:
        with self._lock:
            for tag in tags:
                for key in self._keys_by_tag.pop(tag, set()):
                    self._discard(key)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._keys_by_tag.clear()
            self.hits = self.misses = 0

    def _discard(self, key: str) -> None:
        entry = self._entries.pop(key, None)
        if entry is not None:
            for tag in entry[2]:
                self._keys_by_tag.get(tag, set()).discard(key)


class RedisCache(Cache):
    """Distributed cache: every mesh node pointing at the same Redis sees the same
    entries and the same invalidations.

    Layout (``prefix`` defaults to ``mesh-cache:``)::

        <prefix>entry:<key>  STRING  the JSON value, with the entry's TTL (SET ... EX)
        <prefix>tag:<tag>    SET     keys of the entries tagged with <tag>

    ``invalidate_tags`` reads and deletes each tag set in one MULTI/EXEC transaction,
    so an entry added concurrently lands in a fresh set and is not lost, then deletes
    the entries. Tag sets get the entry TTL on every add (all entries share the same
    TTL), so sets of tags nobody invalidates expire on their own.

    Failure policy: a failed ``get`` or ``set`` is logged and treated as a miss (the
    cache is an optimisation). A failed ``invalidate_tags`` raises: silently keeping
    stale entries could serve data a user may no longer see.
    """

    def __init__(self, client: "redis.Redis", prefix: str = "mesh-cache:"):
        self._redis = client
        self._prefix = prefix

    @classmethod
    def from_url(cls, url: str, **kwargs) -> "RedisCache":
        return cls(redis.Redis.from_url(url, decode_responses=True, socket_timeout=2, socket_connect_timeout=2),
                   **kwargs)

    def get(self, key: str) -> Optional[str]:
        try:
            return self._redis.get(self._entry(key))
        except redis.RedisError:
            logger.warning("Cache read failed; treating as a miss", exc_info=True)
            return None

    def set(self, key: str, value: str, *, tags: Iterable[str], ttl_seconds: float) -> None:
        ttl = math.ceil(ttl_seconds)
        if ttl <= 0:
            return
        entry = self._entry(key)
        try:
            with self._redis.pipeline(transaction=True) as pipe:
                pipe.set(entry, value, ex=ttl)
                for tag in tags:
                    pipe.sadd(self._tag(tag), entry)
                    pipe.expire(self._tag(tag), ttl)
                pipe.execute()
        except redis.RedisError:
            logger.warning("Cache write failed; result not cached", exc_info=True)

    def invalidate_tags(self, tags: Iterable[str]) -> None:
        tag_keys = [self._tag(tag) for tag in tags]
        if not tag_keys:
            return
        with self._redis.pipeline(transaction=True) as pipe:
            for tag_key in tag_keys:
                pipe.smembers(tag_key)
            pipe.delete(*tag_keys)
            *members, _deleted = pipe.execute()
        entries = set().union(*members)
        if entries:
            self._redis.delete(*entries)

    def clear(self) -> None:
        keys = list(self._redis.scan_iter(match=f"{self._prefix}*", count=500))
        if keys:
            self._redis.delete(*keys)

    def _entry(self, key: str) -> str:
        return f"{self._prefix}entry:{key}"

    def _tag(self, tag: str) -> str:
        return f"{self._prefix}tag:{tag}"
