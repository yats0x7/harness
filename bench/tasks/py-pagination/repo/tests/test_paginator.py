import pytest

from pagekit import EmptyPage, PageNotAnInteger, Paginator


def test_basic_counts():
    p = Paginator(list(range(25)), per_page=10)
    assert p.count == 25
    assert p.num_pages == 3
    assert list(p.page_range) == [1, 2, 3]


def test_page_contents():
    p = Paginator(list(range(25)), per_page=10)
    assert p.page(1).object_list == list(range(10))
    assert p.page(3).object_list == [20, 21, 22, 23, 24]


def test_single_partial_page():
    p = Paginator(["a", "b", "c"], per_page=10)
    assert p.num_pages == 1
    assert p.page(1).object_list == ["a", "b", "c"]
    assert not p.page(1).has_next()


def test_empty_list_allows_first_page():
    p = Paginator([], per_page=5)
    assert p.num_pages == 1
    assert p.page(1).object_list == []


def test_empty_list_without_empty_first_page():
    p = Paginator([], per_page=5, allow_empty_first_page=False)
    assert p.num_pages == 0
    with pytest.raises(EmptyPage):
        p.page(1)


def test_orphans_folded_into_last_page():
    p = Paginator(list(range(23)), per_page=10, orphans=4)
    assert p.num_pages == 2
    assert p.page(2).object_list == list(range(10, 23))


def test_navigation():
    p = Paginator(list(range(35)), per_page=10)
    page = p.page(2)
    assert page.has_next() and page.has_previous()
    assert page.next_page_number() == 3
    assert page.previous_page_number() == 1
    assert not p.page(4).has_next()


def test_invalid_numbers():
    p = Paginator(list(range(35)), per_page=10)
    with pytest.raises(PageNotAnInteger):
        p.page("abc")
    with pytest.raises(PageNotAnInteger):
        p.page(1.5)
    with pytest.raises(EmptyPage):
        p.page(0)
    with pytest.raises(EmptyPage):
        p.page(5)


def test_get_page_falls_back():
    p = Paginator(list(range(35)), per_page=10)
    assert p.get_page("x").number == 1
    assert p.get_page(99).number == 4


def test_indexes():
    p = Paginator(list(range(35)), per_page=10)
    assert (p.page(1).start_index(), p.page(1).end_index()) == (1, 10)
    assert (p.page(4).start_index(), p.page(4).end_index()) == (31, 35)


def test_per_page_validation():
    with pytest.raises(ValueError):
        Paginator([1, 2], per_page=0)
