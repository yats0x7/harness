"""Human and machine readable views of a page."""


def summary(page, noun="results"):
    """Return a string like ``"Showing 11-20 of 45 results"``."""
    total = page.paginator.count
    if total == 0:
        return "No %s" % noun
    return "Showing %d-%d of %d %s" % (page.start_index(), page.end_index(), total, noun)


def to_dict(page, serialize=lambda obj: obj):
    """Serialise a page for a JSON API response."""
    paginator = page.paginator
    return {
        "page": page.number,
        "per_page": paginator.per_page,
        "total": paginator.count,
        "total_pages": paginator.num_pages,
        "has_next": page.has_next(),
        "has_previous": page.has_previous(),
        "next_page": page.number + 1 if page.has_next() else None,
        "previous_page": page.number - 1 if page.has_previous() else None,
        "results": [serialize(obj) for obj in page.object_list],
    }
