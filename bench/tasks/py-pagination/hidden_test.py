import pytest

from pagekit import EmptyPage, Paginator, page_links, summary, to_dict


@pytest.mark.parametrize("total,per_page,expected", [
    (20, 10, 2),
    (10, 10, 1),
    (30, 5, 6),
    (100, 25, 4),
    (21, 10, 3),
    (1, 1, 1),
])
def test_num_pages_exact_multiple(total, per_page, expected):
    assert Paginator(list(range(total)), per_page=per_page).num_pages == expected


def test_last_page_has_no_next_when_total_is_exact_multiple():
    p = Paginator(list(range(20)), per_page=10)
    last = p.page(2)
    assert last.object_list == list(range(10, 20))
    assert not last.has_next()
    with pytest.raises(EmptyPage):
        p.page(3)


def test_api_payload_for_exact_multiple():
    p = Paginator(list(range(40)), per_page=20)
    data = to_dict(p.page(2))
    assert data["total_pages"] == 2
    assert data["has_next"] is False
    assert data["next_page"] is None


def test_summary_on_last_full_page():
    p = Paginator(list(range(40)), per_page=20)
    assert summary(p.page(2)) == "Showing 21-40 of 40 results"


def test_links_do_not_include_phantom_page():
    p = Paginator(list(range(200)), per_page=10)
    assert page_links(p.page(10)) == [1, None, 8, 9, 10, 11, 12, None, 20]
    assert page_links(p.page(20))[-1] == 20


def test_orphans_with_exact_multiple():
    p = Paginator(list(range(23)), per_page=10, orphans=3)
    assert p.num_pages == 2
    assert p.page(2).object_list == list(range(10, 23))
