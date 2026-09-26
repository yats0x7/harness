"""Reading rows and header-keyed records."""
from .dialect import CSV
from .errors import ParseError
from .lexer import split_fields


def read_rows(lines, dialect=CSV):
    """Yield lists of field strings, skipping blank and comment lines."""
    for line_no, line in enumerate(lines, start=1):
        line = line.rstrip("\r\n")
        if not line.strip():
            continue
        if dialect.comment and line.lstrip().startswith(dialect.comment):
            continue
        try:
            yield split_fields(line, dialect)
        except ParseError as exc:
            raise ParseError(str(exc), line_no=line_no) from None


def read_records(lines, dialect=CSV, schema=None, header=None):
    """Return a list of dicts keyed by the header row (or ``header``)."""
    rows = read_rows(lines, dialect)
    if header is None:
        try:
            header = next(rows)
        except StopIteration:
            return []
    records = []
    for row in rows:
        if len(row) != len(header):
            raise ParseError("expected %d fields, got %d" % (len(header), len(row)))
        record = dict(zip(header, row))
        records.append(schema.convert(record) if schema else record)
    return records
