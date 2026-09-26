# memo

A bounded in-process cache. Entries expire after a TTL and, when the cache is
full, the least recently used entry is evicted.

```python
from memo import TTLCache, memoize, SessionStore

cache = TTLCache(maxsize=1000, ttl=60)
cache.set("user:1", {"name": "Ada"})
cache.get("user:1")

@memoize(TTLCache(maxsize=256, ttl=5))
def exchange_rate(base, quote): ...

sessions = SessionStore(idle_timeout=1800, max_sessions=10_000)
```

All time handling goes through a clock callable, so tests can pass
`memo.clock.FakeClock()` and advance time by hand.

Run the tests with `python -m pytest -q tests`.
