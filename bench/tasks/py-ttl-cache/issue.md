# Active sessions expire while users are still clicking around

We run `SessionStore(idle_timeout=1800)` behind our web app. Every request handler does `load(sid)` at the start and `save(sid, data)` at the end, which according to the docstring should keep an active session alive for another 30 minutes after each request.

In practice every session dies exactly 30 minutes after login, no matter how active the user is. Support has a stream of "logged out in the middle of checkout" tickets.

### Reproduce

```python
from memo import FakeClock, SessionStore

clock = FakeClock()
store = SessionStore(idle_timeout=60, clock=clock)
sid = store.create({"user": 7})

clock.advance(50)
store.save(sid, store.load(sid))   # user is active
clock.advance(50)

print(store.load(sid))
print(store.expires_in(sid))
```

### Actual

```
None
0.0
```

### Expected

```
{'user': 7}
10.0
```

(i.e. the session is still there, with 60 - 50 = 10 seconds left since the last save).

### Probably related

With a plain `TTLCache` we also see the wrong key being evicted when the cache is full:

```python
from memo import TTLCache, FakeClock

c = TTLCache(maxsize=2, ttl=None, clock=FakeClock())
c.set("a", 1)
c.set("b", 2)
c.set("a", 3)      # "a" was just written
c.set("c", 4)
print(c.keys())    # actual: ['b', 'c']   expected: ['a', 'c']
```

Passing a different `ttl=` when overwriting an existing key also seems to have no effect. Reading keys with `get()` does keep them fresh in the LRU order, so it only shows up when a key is written again.

memo 2.1.0
