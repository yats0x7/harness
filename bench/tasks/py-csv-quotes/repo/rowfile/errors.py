class ParseError(ValueError):
    """A line could not be split into fields."""

    def __init__(self, message, line_no=None):
        self.line_no = line_no
        if line_no is not None:
            message = "line %d: %s" % (line_no, message)
        super().__init__(message)


class SchemaError(ValueError):
    """A field value does not match its declared column type."""
