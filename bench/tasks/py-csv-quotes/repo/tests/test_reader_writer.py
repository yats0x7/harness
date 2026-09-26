import io

import pytest

from rowfile import ParseError, Schema, SchemaError, read_records, read_rows, write_records
from rowfile.writer import format_field, format_row


def test_read_rows_skips_blank_and_comment_lines():
    lines = ["# header comment\n", "a,b\n", "\n", "1,2\n"]
    assert list(read_rows(lines)) == [["a", "b"], ["1", "2"]]


def test_read_records_with_schema():
    text = "id,price,active,name\n1,9.5,yes,Widget\n2,,no,\"Gadget, large\"\n"
    schema = Schema({"id": int, "price": float, "active": bool})
    assert read_records(io.StringIO(text), schema=schema) == [
        {"id": 1, "price": 9.5, "active": True, "name": "Widget"},
        {"id": 2, "price": None, "active": False, "name": "Gadget, large"},
    ]


def test_field_count_mismatch():
    with pytest.raises(ParseError):
        read_records(["a,b", "1,2,3"])


def test_parse_error_reports_line_number():
    with pytest.raises(ParseError) as info:
        list(read_rows(["a,b", 'x,"y']))
    assert info.value.line_no == 2


def test_schema_errors():
    with pytest.raises(SchemaError):
        Schema({"n": int}).convert({"n": "abc"})
    with pytest.raises(SchemaError):
        Schema({"n": int}, required=["n"]).convert({"n": ""})


def test_format_field_quoting():
    assert format_field("plain") == "plain"
    assert format_field("a,b") == '"a,b"'
    assert format_field('say "hi"') == '"say ""hi"""'
    assert format_field(" x") == '" x"'
    assert format_field(None) == ""
    assert format_field(True) == "true"


def test_format_row():
    assert format_row([1, "two", "3,4"]) == '1,two,"3,4"'


def test_simple_round_trip():
    records = [{"id": "1", "name": "Smith, John"}, {"id": "2", "name": "Lee"}]
    out = io.StringIO()
    write_records(records, out)
    out.seek(0)
    assert read_records(out) == records
