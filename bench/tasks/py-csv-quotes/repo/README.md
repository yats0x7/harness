# rowfile

Reads and writes delimited text (CSV, TSV, pipe-separated) with an optional
column schema for type conversion.

```python
from rowfile import Dialect, Schema, read_records, write_records

schema = Schema({"id": int, "price": float, "active": bool})
rows = read_records(open("products.csv"), schema=schema)
```

Quoting follows the usual CSV convention: a field wrapped in the quote
character may contain the delimiter, and a quote character inside a quoted
field is written twice.

Run the tests with `python -m pytest -q tests`.
