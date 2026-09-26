"""Reading settings layers from files and the environment."""
import json
import os

from .errors import ConfigError

ENV_PREFIX = "APP__"


def read_json_file(path):
    """Read a JSON settings file. A missing file is an empty layer."""
    if path is None or not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except json.JSONDecodeError as exc:
        raise ConfigError("%s: invalid JSON (%s)" % (path, exc))
    if not isinstance(data, dict):
        raise ConfigError("%s: top level must be an object" % path)
    return data


def coerce(raw):
    """Turn an environment string into a bool/int/float/None/str."""
    lowered = raw.strip().lower()
    if lowered in ("true", "yes", "on"):
        return True
    if lowered in ("false", "no", "off"):
        return False
    if lowered in ("null", "none", ""):
        return None
    for cast in (int, float):
        try:
            return cast(raw)
        except ValueError:
            pass
    return raw


def read_env(environ=None, prefix=ENV_PREFIX):
    """Build a nested layer from ``APP__A__B__C=value`` variables."""
    environ = os.environ if environ is None else environ
    layer = {}
    for name in sorted(environ):
        if not name.startswith(prefix):
            continue
        parts = [p.lower() for p in name[len(prefix):].split("__") if p]
        if not parts:
            continue
        node = layer
        for part in parts[:-1]:
            child = node.get(part)
            if not isinstance(child, dict):
                child = node[part] = {}
            node = child
        node[parts[-1]] = coerce(environ[name])
    return layer
