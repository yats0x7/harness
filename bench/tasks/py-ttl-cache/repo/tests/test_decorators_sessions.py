from memo import FakeClock, SessionStore, TTLCache, memoize
from memo.keys import make_key


def test_memoize_caches_until_ttl():
    clock = FakeClock()
    calls = []

    @memoize(TTLCache(maxsize=10, ttl=5, clock=clock))
    def square(x):
        calls.append(x)
        return x * x

    assert square(3) == 9
    assert square(3) == 9
    assert calls == [3]
    clock.advance(5)
    assert square(3) == 9
    assert calls == [3, 3]


def test_memoize_invalidate_and_kwargs():
    calls = []

    @memoize()
    def f(a, b=0):
        calls.append((a, b))
        return a + b

    f(1, b=2)
    f(1, b=2)
    f.invalidate(1, b=2)
    f(1, b=2)
    assert calls == [(1, 2), (1, 2)]


def test_make_key():
    assert make_key((1, [2, 3]), {"x": {"y": 1}}) == make_key((1, [2, 3]), {"x": {"y": 1}})
    assert make_key((1,), {}) != make_key((1.0,), {}, typed=True)


def test_session_lifecycle():
    clock = FakeClock()
    store = SessionStore(idle_timeout=60, clock=clock)
    sid = store.create({"user": 1})
    assert store.load(sid) == {"user": 1}
    clock.advance(61)
    assert store.load(sid) is None


def test_session_destroy_and_len():
    store = SessionStore(idle_timeout=60, clock=FakeClock())
    a = store.create()
    store.create()
    assert len(store) == 2
    assert store.destroy(a)
    assert store.load(a) is None
    assert len(store) == 1


def test_loaded_copy_is_detached():
    store = SessionStore(clock=FakeClock())
    sid = store.create({"cart": 1})
    data = store.load(sid)
    data["cart"] = 99
    assert store.load(sid) == {"cart": 1}
