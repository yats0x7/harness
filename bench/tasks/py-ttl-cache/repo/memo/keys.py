"""Turning call arguments into hashable cache keys."""

_KWARGS_MARK = ("__kw__",)


def _freeze(value):
    if isinstance(value, dict):
        return ("__dict__",) + tuple(sorted((k, _freeze(v)) for k, v in value.items()))
    if isinstance(value, (list, tuple)):
        return (type(value).__name__,) + tuple(_freeze(v) for v in value)
    if isinstance(value, (set, frozenset)):
        return ("__set__",) + tuple(sorted(_freeze(v) for v in value))
    hash(value)  # raise TypeError early for unhashable leaves
    return value


def make_key(args, kwargs, typed=False):
    key = tuple(_freeze(a) for a in args)
    if kwargs:
        key += _KWARGS_MARK + tuple(sorted((k, _freeze(v)) for k, v in kwargs.items()))
    if typed:
        key += tuple(type(a) for a in args)
        key += tuple(type(v) for _, v in sorted(kwargs.items()))
    return key
