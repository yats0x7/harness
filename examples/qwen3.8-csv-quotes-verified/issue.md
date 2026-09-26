# Can't read back files that contain a double quote inside a quoted field

`rowfile` writes a value like `The "best" one` as `"The ""best"" one"` (which matches what Excel and Python's `csv` module do), but reading that same file back fails.

### Reproduce

```python
import io
from rowfile import read_records, write_records

records = [{"sku": "P-2", "desc": 'The "best" one'}]
buf = io.StringIO()
write_records(records, buf)
print(buf.getvalue())
buf.seek(0)
print(read_records(buf))
```

### Actual

```
sku,desc
P-2,"The ""best"" one"

Traceback (most recent call last):
  ...
rowfile.errors.ParseError: line 2: unexpected character 'b' after closing quote
```

We see the same error on product exports from our ERP, which has lots of descriptions with inch marks, e.g.

```
sku,desc
P-1,"Hose, 1/2"" x 50'"
```

### Expected

```
[{'sku': 'P-2', 'desc': 'The "best" one'}]
```

and `'Hose, 1/2" x 50\''` for the ERP row. Whatever `write_records` produces, `read_records` should read back into the same values. Quoted fields with commas and no inner quotes (`"Smith, John"`) work fine.

rowfile 0.9.1