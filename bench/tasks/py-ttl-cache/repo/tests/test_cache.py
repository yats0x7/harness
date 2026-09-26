import pytest

from memo import FakeClock, TTLCache


def make(maxsize=3, ttl=10):
    clock = FakeClock()
    return TTLCache(maxsize=maxsize, ttl=ttl, clock=clock), clock


def test_set_and_get():
    cache, _ = make()
    cache.set("a", 1)
    assert cache.get("a") == 1
    assert cache.get("missing", "d") == "d"


def test_entry_expires_after_ttl():
    cache, clock = make(ttl=10)
    cache.set("a", 1)
    clock.advance(9.9)
    assert cache.get("a") == 1
    clock.advance(0.1)
    assert cache.get("a") is None
    assert cache.stats.expirations == 1


def test_per_entry_ttl_and_no_expiry():
    cache, clock = make(ttl=10)
    cache.set("short", 1, ttl=1)
    cache.set("forever", 2, ttl=None)
    clock.advance(1000)
    assert cache.get("short") is None
    assert cache.get("forever") == 2
    assert cache.ttl_remaining("forever") is None


def test_ttl_remaining():
    cache, clock = make(ttl=10)
    cache.set("a", 1)
    clock.advance(4)
    assert cache.ttl_remaining("a") == pytest.approx(6)
    assert cache.ttl_remaining("zzz") == 0.0


def test_lru_eviction_order():
    evicted = []
    clock = FakeClock()
    cache = TTLCache(maxsize=2, ttl=None, clock=clock, on_evict=lambda k, v: evicted.append(k))
    cache.set("a", 1)
    cache.set("b", 2)
    cache.set("c", 3)
    assert evicted == ["a"]
    assert cache.keys() == ["b", "c"]


def test_get_marks_recently_used():
    cache, _ = make(maxsize=2, ttl=None)
    cache.set("a", 1)
    cache.set("b", 2)
    cache.get("a")
    cache.set("c", 3)
    assert "a" in cache and "c" in cache and "b" not in cache


def test_peek_does_not_touch_recency():
    cache, _ = make(maxsize=2, ttl=None)
    cache.set("a", 1)
    cache.set("b", 2)
    assert cache.peek("a") == 1
    cache.set("c", 3)
    assert "a" not in cache


def test_expired_entries_are_dropped_before_live_ones():
    cache, clock = make(maxsize=2, ttl=10)
    cache.set("old", 1, ttl=1)
    cache.set("live", 2)
    clock.advance(2)
    cache.set("new", 3)
    assert cache.keys() == ["live", "new"]
    assert cache.stats.evictions == 0


def test_setting_an_expired_key_stores_fresh_entry():
    cache, clock = make(ttl=5)
    cache.set("a", 1)
    clock.advance(6)
    cache.set("a", 2)
    assert cache.get("a") == 2
    assert cache.ttl_remaining("a") == pytest.approx(5)


def test_len_delete_clear():
    cache, clock = make(ttl=5)
    cache.set("a", 1)
    cache.set("b", 2, ttl=1)
    clock.advance(2)
    assert len(cache) == 1
    assert cache.delete("a") is True
    assert cache.delete("a") is False
    cache.set("c", 3)
    cache.clear()
    assert len(cache) == 0


def test_stats():
    cache, _ = make()
    cache.set("a", 1)
    cache.get("a")
    cache.get("b")
    assert cache.stats.as_dict() == {
        "hits": 1, "misses": 1, "evictions": 0, "expirations": 0, "hit_rate": 0.5,
    }


def test_validation():
    with pytest.raises(ValueError):
        TTLCache(maxsize=0)
    with pytest.raises(ValueError):
        TTLCache(ttl=0)
