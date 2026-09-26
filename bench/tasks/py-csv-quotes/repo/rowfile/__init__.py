"""Delimited text reader/writer."""
from .dialect import CSV, PIPE, TSV, Dialect
from .errors import ParseError, SchemaError
from .reader import read_records, read_rows
from .schema import Schema
from .writer import format_row, write_records

__all__ = [
    "CSV", "PIPE", "TSV", "Dialect",
    "ParseError", "SchemaError",
    "read_records", "read_rows",
    "Schema",
    "format_row", "write_records",
]
__version__ = "0.9.1"
