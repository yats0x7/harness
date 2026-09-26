"""LRU cache with per-entry expiry."""
from collections import OrderedDict
from threading import RLock

from .clock import monotonic_clock
from .stats import CacheStats

_DEFAULT = object()


class _Entry:
    __slots__ = ("value", "expires_at")

    def __init__(self, value, expires_at):
        self.value = value
        self.expires_at = expires_at

    def expired(self, now):
        return self.expires_at is not None and now >= self.expires_at


class TTLCache:
    """A bounded mapping whose entries expire ``ttl`` seconds after being set.

    * ``get`` of a live key marks it as most recently used.
    * ``set`` stores the value, (re)starting its TTL and marking it as most
      recently used.
    * When more than ``maxsize`` entries are held, expired entries are
      dropped first and then the least recently used ones.

    ``ttl=None`` means entries never expire on their own.
    """

    def __init__(self, maxsize=128, ttl=60.0, clock=None, on_evict=None):
        if maxsize < 1:
            raise ValueError("maxsize must be at least 1")
        if ttl is not None and ttl <= 0:
            raise ValueError("ttl must be positive or None")
        self.maxsize = maxsize
        self.ttl = ttl
        self._clock = clock or monotonic_clock
        self._on_evict = on_evict
        self._data = OrderedDict()
        self._lock = RLock()
        self.stats = CacheStats()

    # -- reads -----------------------------------------------------------

    def get(self, key, default=None):
        with self._lock:
            now = self._clock()
            entry = self._data.get(key)
            if entry is None:
                self.stats.misses += 1
                return default
            if entry.expired(now):
                del self._data[key]
                self.stats.expirations += 1
                self.stats.misses += 1
                return default
            self._data.move_to_end(key)
            self.stats.hits += 1
            return entry.value

    def peek(self, key, default=None):
        """Like ``get`` but does not affect recency or statistics."""
        with self._lock:
            entry = self._data.get(key)
            if entry is None or entry.expired(self._clock()):
                return default
            return entry.value

    def ttl_remaining(self, key):
        """Seconds until ``key`` expires, ``None`` if it never does, 0 if absent."""
        with self._lock:
            entry = self._data.get(key)
            now = self._clock()
            if entry is None or entry.expired(now):
                return 0.0
            if entry.expires_at is None:
                return None
            return entry.expires_at - now

    def __contains__(self, key):
        return self.peek(key, _DEFAULT) is not _DEFAULT

    def __len__(self):
        with self._lock:
            self._purge(self._clock())
            return len(self._data)

    def keys(self):
        """Live keys from least to most recently used."""
        with self._lock:
            self._purge(self._clock())
            return list(self._data.keys())

    # -- writes ----------------------------------------------------------

    def set(self, key, value, ttl=_DEFAULT):
        with self._lock:
            now = self._clock()
            ttl = self.ttl if ttl is _DEFAULT else ttl
            expires_at = None if ttl is None else now + ttl
            self._store(key, value, expires_at, now)
            self._shrink(now)

    def _store(self, key, value, expires_at, now):
        entry = self._data.get(key)
        if entry is not None and not entry.expired(now):
            # Fast path: the key is already cached and still live.
            entry.value = value
            return
        self._data[key] = _Entry(value, expires_at)
        self._data.move_to_end(key)

    def delete(self, key):
        with self._lock:
            return self._data.pop(key, None) is not None

    def clear(self):
        with self._lock:
            self._data.clear()

    # -- housekeeping ----------------------------------------------------

    def _purge(self, now):
        dead = [k for k, e in self._data.items() if e.expired(now)]
        for k in dead:
            del self._data[k]
        self.stats.expirations += len(dead)
        return len(dead)

    def _shrink(self, now):
        if len(self._data) <= self.maxsize:
            return
        self._purge(now)
        while len(self._data) > self.maxsize:
            key, entry = self._data.popitem(last=False)
            self.stats.evictions += 1
            if self._on_evict is not None:
                self._on_evict(key, entry.value)

    def purge(self):
        """Drop all expired entries now; returns how many were removed."""
        with self._lock:
            return self._purge(self._clock())
