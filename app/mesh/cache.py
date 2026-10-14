import threading
import time
from abc import ABC, abstractmethod
from collections import defaultdict
from typing import Callable, Dict, Iterable, Optional, Set, Tuple


class Cache(ABC):
    """Tag-aware string cache. Swap ``InMemoryCache`` for e.g. a Redis implementation
    when running more than one process."""

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
