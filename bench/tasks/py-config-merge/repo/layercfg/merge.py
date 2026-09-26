"""Combining settings layers."""
import copy


def merge(base, override):
    """Return a new mapping with ``override`` applied on top of ``base``.

    Neither argument is modified. Sections present in both are combined so
    that an override only needs to mention the keys it changes.
    """
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            section = result[key]
            section.update(copy.deepcopy(value))
        else:
            result[key] = copy.deepcopy(value)
    return result


def merge_all(layers):
    """Merge a sequence of layers, lowest priority first."""
    result = {}
    for layer in layers:
        if layer:
            result = merge(result, layer)
    return result
