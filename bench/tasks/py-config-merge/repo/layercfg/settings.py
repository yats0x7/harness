"""Public entry point: ``load_settings`` and the ``Settings`` view."""
from .defaults import DEFAULTS
from .errors import ConfigError, MissingSetting
from .merge import merge_all
from .sources import read_env, read_json_file

_MISSING = object()


class Settings:
    """Read-only view over resolved settings with dotted-path lookup."""

    def __init__(self, data):
        self._data = data

    def get(self, path, default=_MISSING):
        node = self._data
        for part in path.split("."):
            if isinstance(node, dict) and part in node:
                node = node[part]
            elif default is not _MISSING:
                return default
            else:
                raise MissingSetting(path)
        return node

    def section(self, path):
        value = self.get(path)
        if not isinstance(value, dict):
            raise ConfigError("%s is not a section" % path)
        return Settings(value)

    def as_dict(self):
        import copy

        return copy.deepcopy(self._data)

    def __contains__(self, path):
        return self.get(path, _MISSING_SENTINEL) is not _MISSING_SENTINEL

    def __repr__(self):
        return "Settings(%r)" % (self._data,)


_MISSING_SENTINEL = object()


def load_settings(path=None, env=None, overrides=None):
    """Resolve settings from defaults, a JSON file, env vars and explicit overrides."""
    layers = [DEFAULTS, read_json_file(path), read_env(env), overrides or {}]
    return Settings(merge_all(layers))
