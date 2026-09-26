import pytest

from rowfile import CSV, PIPE, TSV, ParseError
from rowfile.lexer import split_fields


def test_plain_fields():
    assert split_fields("a,b,c", CSV) == ["a", "b", "c"]


def test_empty_fields():
    assert split_fields("a,,c,", CSV) == ["a", "", "c", ""]


def test_unquoted_whitespace_stripped():
    assert split_fields(" a , b ", CSV) == ["a", "b"]


def test_quoted_field_keeps_delimiter():
    assert split_fields('1,"Smith, John",x', CSV) == ["1", "Smith, John", "x"]


def test_quoted_field_keeps_whitespace():
    assert split_fields('"  padded  ",b', CSV) == ["  padded  ", "b"]


def test_empty_quoted_field():
    assert split_fields('"",b', CSV) == ["", "b"]


def test_other_delimiters():
    assert split_fields("a\tb c\td", TSV) == ["a", "b c", "d"]
    assert split_fields('x|"y|z"', PIPE) == ["x", "y|z"]


def test_space_before_opening_quote():
    assert split_fields('a, "b,c"', CSV) == ["a", "b,c"]


def test_unterminated_quote():
    with pytest.raises(ParseError):
        split_fields('a,"oops', CSV)


def test_garbage_after_closing_quote():
    with pytest.raises(ParseError):
        split_fields('"abc"def,1', CSV)


def test_whitespace_after_closing_quote_ignored():
    assert split_fields('"a,b"  ,c', CSV) == ["a,b", "c"]
