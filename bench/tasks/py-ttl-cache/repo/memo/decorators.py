"""Function memoization on top of TTLCache."""
import functools

from .cache import TTLCache
from .keys import make_key

_MISS = object()


def memoize(cache=None, typed=False, key=None):
    """Cache a function's results in ``cache`` (a new TTLCache by default).

    ``key`` may be a callable ``(args, kwargs) -> hashable`` to override the
    default key derivation. The wrapper exposes ``.cache`` and
    ``.invalidate(*args, **kwargs)``.
    """
    cache = cache if cache is not None else TTLCache()
    make = key or (lambda a, kw: make_key(a, kw, typed=typed))

    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            k = make(args, kwargs)
            value = cache.get(k, _MISS)
            if value is _MISS:
                value = func(*args, **kwargs)
                cache.set(k, value)
            return value

        def invalidate(*args, **kwargs):
            return cache.delete(make(args, kwargs))

        wrapper.cache = cache
        wrapper.invalidate = invalidate
        return wrapper

    return decorator
