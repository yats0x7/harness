# pagekit

Pagination helpers for list views and JSON APIs.

```python
from pagekit import Paginator, page_links, summary

p = Paginator(list(range(95)), per_page=10)
page = p.page(3)
page.object_list      # [20, ..., 29]
summary(page)         # "Showing 21-30 of 95"
page_links(page)      # [1, 2, 3, 4, 5, None, 10]
```

Run the tests with `python -m pytest -q tests`.
