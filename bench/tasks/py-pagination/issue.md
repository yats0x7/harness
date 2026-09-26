# Extra empty page at the end of the list when the item count divides evenly

We use `pagekit` for the `/api/orders` endpoint and for the admin list views. When the number of records is an exact multiple of the page size, the pager advertises one more page than actually exists.

### Steps to reproduce

```python
from pagekit import Paginator, to_dict

p = Paginator(list(range(20)), per_page=10)
print(p.num_pages)
print(to_dict(p.page(2))["has_next"])
print(p.page(3).object_list)
```

### Actual output

```
3
True
[]
```

So the last real page says there is a next page, the "Next" button is shown, and clicking it lands on an empty page 3 instead of a 404. The admin pager also shows a trailing page number that leads nowhere.

It also happens with `orphans`: 23 items, `per_page=10`, `orphans=3` gives 3 pages, the last one empty, instead of 2 pages with the 3 extra items folded into page 2.

### Expected output

```
2
False
EmptyPage raised for page 3
```

Counts that are not exact multiples (e.g. 25 items with 10 per page) behave correctly, which is probably why nobody noticed until our order count hit 400.

pagekit 0.4.2, Python 3.11.
