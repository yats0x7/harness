"""Build the list of page numbers shown in a pager widget."""


def page_links(page, on_each_side=2, on_ends=1):
    """Return page numbers to render around ``page``.

    Gaps are represented by ``None`` so templates can render an ellipsis::

        >>> page_links(Paginator(range(200), 10).page(10))
        [1, None, 8, 9, 10, 11, 12, None, 20]
    """
    num_pages = page.paginator.num_pages
    number = page.number
    window = on_each_side * 2 + on_ends * 2 + 1

    if num_pages <= window + 2:
        return list(range(1, num_pages + 1))

    links = []
    if number > on_each_side + on_ends + 2:
        links.extend(range(1, on_ends + 1))
        links.append(None)
        start = number - on_each_side
    else:
        start = 1

    if number < num_pages - on_each_side - on_ends - 1:
        end = number + on_each_side
        links.extend(range(start, end + 1))
        links.append(None)
        links.extend(range(num_pages - on_ends + 1, num_pages + 1))
    else:
        links.extend(range(start, num_pages + 1))
    return links
