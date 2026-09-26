"""Split one logical line into raw field strings."""
from .errors import ParseError


def split_fields(line, dialect):
    """Split ``line`` into a list of field strings according to ``dialect``.

    Quoted fields may contain the delimiter. Inside a quoted field, two quote
    characters in a row stand for one literal quote character.
    """
    delim = dialect.delimiter
    quote = dialect.quotechar
    fields = []
    buf = []
    in_quotes = False
    was_quoted = False
    i = 0
    n = len(line)

    while i < n:
        ch = line[i]
        if in_quotes:
            if ch == quote:
                if i + 1 < n and line[i + 1] == quote:
                    buf.append(quote)
                else:
                    in_quotes = False
            else:
                buf.append(ch)
        elif ch == quote and not "".join(buf).strip():
            buf = []
            in_quotes = True
            was_quoted = True
        elif ch == delim:
            fields.append(_finish(buf, was_quoted, dialect))
            buf = []
            was_quoted = False
        elif was_quoted:
            # only whitespace may sit between a closing quote and the delimiter
            if not ch.isspace():
                raise ParseError("unexpected character %r after closing quote" % ch)
        else:
            buf.append(ch)
        i += 1

    if in_quotes:
        raise ParseError("unterminated quoted field")
    fields.append(_finish(buf, was_quoted, dialect))
    return fields


def _finish(buf, was_quoted, dialect):
    value = "".join(buf)
    if was_quoted:
        return value
    return value.strip() if dialect.strip_unquoted else value
