import pytest

from memo import FakeClock, SessionStore, TTLCache, memoize


def test_overwriting_a_live_key_restarts_its_ttl():
    clock = FakeClock()
    cache = TTLCache(maxsize=10, ttl=10, clock=clock)
    cache.set("a", 1)
    clock.advance(8)
    cache.set("a", 2)
    assert cache.ttl_remaining("a") == pytest.approx(10)
    clock.advance(8)
    assert cache.get("a") == 2


def test_overwrite_honours_explicit_ttl():
    clock = FakeClock()
    cache = TTLCache(maxsize=10, ttl=10, clock=clock)
    cache.set("a", 1)
    cache.set("a", 2, ttl=None)
    clock.advance(1000)
    assert cache.get("a") == 2

    cache.set("b", 1, ttl=100)
    cache.set("b", 2, ttl=1)
    clock.advance(2)
    assert cache.get("b") is None


def test_overwriting_a_live_key_marks_it_most_recently_used():
    evicted = []
    cache = TTLCache(maxsize=2, ttl=None, clock=FakeClock(),
                     on_evict=lambda k, v: evicted.append(k))
    cache.set("a", 1)
    cache.set("b", 2)
    cache.set("a", 3)
    cache.set("c", 4)
    assert evicted == ["b"]
    assert cache.keys() == ["a", "c"]
    assert cache.get("a") == 3


def test_active_session_is_kept_alive_by_save():
    clock = FakeClock()
    store = SessionStore(idle_timeout=60, clock=clock)
    sid = store.create({"user": 7})
    for _ in range(5):
        clock.advance(50)
        data = store.load(sid)
        assert data is not None
        data["hits"] = data.get("hits", 0) + 1
        store.save(sid, data)
    assert store.load(sid) == {"user": 7, "hits": 5}
    assert store.expires_in(sid) == pytest.approx(60)


def test_recently_saved_session_survives_capacity_eviction():
    clock = FakeClock()
    store = SessionStore(idle_timeout=600, max_sessions=2, clock=clock)
    first = store.create({"n": 1})
    second = store.create({"n": 2})
    store.save(first, {"n": 1, "touched": True})
    third = store.create({"n": 3})
    assert store.load(first) == {"n": 1, "touched": True}
    assert store.load(second) is None
    assert store.load(third) == {"n": 3}


def test_memoized_value_written_back_restarts_ttl():
    clock = FakeClock()
    cache = TTLCache(maxsize=10, ttl=10, clock=clock)
    calls = []

    @memoize(cache)
    def load(x):
        calls.append(x)
        return x

    load(1)
    clock.advance(9)
    cache.set((1,), "refreshed")
    clock.advance(9)
    assert load(1) == "refreshed"
    assert calls == [1]
