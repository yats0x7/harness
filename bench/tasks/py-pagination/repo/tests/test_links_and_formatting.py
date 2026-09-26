from pagekit import Paginator, page_links, summary, to_dict


def test_links_small_range():
    p = Paginator(list(range(45)), per_page=10)
    assert page_links(p.page(1)) == [1, 2, 3, 4, 5]


def test_links_middle():
    p = Paginator(list(range(195)), per_page=10)
    assert page_links(p.page(10)) == [1, None, 8, 9, 10, 11, 12, None, 20]


def test_links_near_start():
    p = Paginator(list(range(195)), per_page=10)
    assert page_links(p.page(2)) == [1, 2, 3, 4, None, 20]


def test_links_near_end():
    p = Paginator(list(range(195)), per_page=10)
    assert page_links(p.page(19)) == [1, None, 17, 18, 19, 20]


def test_summary():
    p = Paginator(list(range(45)), per_page=10)
    assert summary(p.page(2)) == "Showing 11-20 of 45 results"
    assert summary(p.page(5), noun="items") == "Showing 41-45 of 45 items"


def test_summary_empty():
    assert summary(Paginator([], per_page=10).page(1)) == "No results"


def test_to_dict():
    p = Paginator(list(range(15)), per_page=10)
    data = to_dict(p.page(2), serialize=str)
    assert data == {
        "page": 2,
        "per_page": 10,
        "total": 15,
        "total_pages": 2,
        "has_next": False,
        "has_previous": True,
        "next_page": None,
        "previous_page": 1,
        "results": ["10", "11", "12", "13", "14"],
    }
