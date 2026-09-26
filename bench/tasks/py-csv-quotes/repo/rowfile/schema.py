"""Column type conversion."""
from .errors import SchemaError

_TRUE = {"1", "true", "yes", "y", "t"}
_FALSE = {"0", "false", "no", "n", "f", ""}


def to_bool(raw):
    lowered = raw.strip().lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    raise SchemaError("not a boolean: %r" % raw)


_CONVERTERS = {bool: to_bool}


class Schema:
    def __init__(self, columns, required=()):
        self.columns = dict(columns)
        self.required = set(required)

    def convert(self, record):
        out = {}
        for name, raw in record.items():
            kind = self.columns.get(name, str)
            if raw == "" and name not in self.required and kind is not str:
                out[name] = None
                continue
            converter = _CONVERTERS.get(kind, kind)
            try:
                out[name] = converter(raw)
            except SchemaError:
                raise
            except (TypeError, ValueError):
                raise SchemaError("column %r: cannot convert %r to %s" % (name, raw, kind.__name__))
        missing = [c for c in self.required if out.get(c) in (None, "")]
        if missing:
            raise SchemaError("missing required columns: %s" % ", ".join(sorted(missing)))
        return out
