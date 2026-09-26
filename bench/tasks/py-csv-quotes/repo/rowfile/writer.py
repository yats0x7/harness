"""Writing rows back out."""
from .dialect import CSV


def _needs_quotes(text, dialect):
    return (
        dialect.delimiter in text
        or dialect.quotechar in text
        or text != text.strip()
        or "\n" in text
        or text.startswith(dialect.comment)
    )


def format_field(value, dialect=CSV):
    if value is None:
        return ""
    if isinstance(value, bool):
        text = "true" if value else "false"
    else:
        text = str(value)
    if _needs_quotes(text, dialect):
        q = dialect.quotechar
        return q + text.replace(q, q + q) + q
    return text


def format_row(values, dialect=CSV):
    return dialect.delimiter.join(format_field(v, dialect) for v in values)


def write_records(records, out, dialect=CSV, columns=None):
    """Write dicts as a header line plus one line per record."""
    records = list(records)
    if columns is None:
        columns = list(records[0].keys()) if records else []
    out.write(format_row(columns, dialect) + "\n")
    for record in records:
        out.write(format_row([record.get(c) for c in columns], dialect) + "\n")
