import io

import pytest

from rowfile import CSV, PIPE, Schema, read_records, write_records
from rowfile.lexer import split_fields


@pytest.mark.parametrize("line,expected", [
    ('"say ""hi""",x', ['say "hi"', "x"]),
    ('"a""b",1', ['a"b', "1"]),
    ('"""quoted""",1', ['"quoted"', "1"]),
    ('"He said ""hi, there"" to me",42', ['He said "hi, there" to me', "42"]),
    ('"""",end', ['"', "end"]),
    ('x,"12"" pipe, 3/4"" bore"', ["x", '12" pipe, 3/4" bore']),
])
def test_escaped_quotes_inside_quoted_field(line, expected):
    assert split_fields(line, CSV) == expected


def test_escaped_quote_with_other_delimiter():
    assert split_fields('"a ""b|c"" d"|e', PIPE) == ['a "b|c" d', "e"]


def test_round_trip_values_with_quotes_and_delimiters():
    records = [
        {"sku": "P-1", "desc": 'Hose, 1/2" x 50\''},
        {"sku": "P-2", "desc": 'The "best" one'},
        {"sku": "P-3", "desc": '""'},
        {"sku": "P-4", "desc": "plain"},
    ]
    out = io.StringIO()
    write_records(records, out)
    out.seek(0)
    assert read_records(out) == records


def test_schema_after_escaped_quote_field():
    text = 'name,qty\n"Bolt ""M8"", zinc",40\n'
    assert read_records(io.StringIO(text), schema=Schema({"qty": int})) == [
        {"name": 'Bolt "M8", zinc', "qty": 40},
    ]
