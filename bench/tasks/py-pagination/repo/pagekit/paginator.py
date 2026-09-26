"""Core paginator.

A ``Paginator`` wraps any sliceable sequence (list, tuple, range, or an
object implementing ``__len__`` and ``__getitem__`` with slices) and hands
out ``Page`` objects.
"""
from .errors import EmptyPage, PageNotAnInteger


class Paginator:
    def __init__(self, object_list, per_page, orphans=0, allow_empty_first_page=True):
        per_page = int(per_page)
        if per_page < 1:
            raise ValueError("per_page must be at least 1")
        orphans = int(orphans)
        if orphans < 0:
            raise ValueError("orphans cannot be negative")
        self.object_list = object_list
        self.per_page = per_page
        self.orphans = orphans
        self.allow_empty_first_page = allow_empty_first_page

    @property
    def count(self):
        """Total number of objects across all pages."""
        return len(self.object_list)

    @property
    def num_pages(self):
        """Total number of pages."""
        if self.count == 0 and not self.allow_empty_first_page:
            return 0
        hits = max(1, self.count - self.orphans)
        return hits // self.per_page + 1

    @property
    def page_range(self):
        return range(1, self.num_pages + 1)

    def validate_number(self, number):
        """Return ``number`` as an int if it is a valid page number."""
        if isinstance(number, float) and not number.is_integer():
            raise PageNotAnInteger("page number is not an integer")
        try:
            number = int(number)
        except (TypeError, ValueError):
            raise PageNotAnInteger("page number is not an integer")
        if number < 1:
            raise EmptyPage("page number is less than 1")
        if number > self.num_pages:
            if number == 1 and self.allow_empty_first_page:
                return number
            raise EmptyPage("that page contains no results")
        return number

    def get_page(self, number):
        """Like ``page()`` but falls back to the first/last page instead of raising."""
        try:
            number = self.validate_number(number)
        except PageNotAnInteger:
            number = 1
        except EmptyPage:
            number = max(1, self.num_pages)
        return self.page(number)

    def page(self, number):
        number = self.validate_number(number)
        bottom = (number - 1) * self.per_page
        top = bottom + self.per_page
        if top + self.orphans >= self.count:
            top = self.count
        return Page(self.object_list[bottom:top], number, self)


class Page:
    def __init__(self, object_list, number, paginator):
        self.object_list = object_list
        self.number = number
        self.paginator = paginator

    def __repr__(self):
        return "<Page %s of %s>" % (self.number, self.paginator.num_pages)

    def __len__(self):
        return len(self.object_list)

    def __iter__(self):
        return iter(self.object_list)

    def has_next(self):
        return self.number < self.paginator.num_pages

    def has_previous(self):
        return self.number > 1

    def has_other_pages(self):
        return self.has_previous() or self.has_next()

    def next_page_number(self):
        return self.paginator.validate_number(self.number + 1)

    def previous_page_number(self):
        return self.paginator.validate_number(self.number - 1)

    def start_index(self):
        """1-based index of the first object on this page (0 if empty)."""
        if self.paginator.count == 0:
            return 0
        return (self.paginator.per_page * (self.number - 1)) + 1

    def end_index(self):
        """1-based index of the last object on this page."""
        if self.number == self.paginator.num_pages:
            return self.paginator.count
        return self.number * self.paginator.per_page
