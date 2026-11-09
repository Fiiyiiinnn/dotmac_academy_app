from app.web.pagination import pagination_context


def test_pagination_context_builds_compact_page_range():
    page = pagination_context(total=101, limit=10, offset=50)

    assert page["current_page"] == 6
    assert page["total_pages"] == 11
    assert page["page_items"] == [1, None, 5, 6, 7, None, 11]
    assert page["page_start"] == 51
    assert page["page_end"] == 60
    assert page["has_prev"] is True
    assert page["has_next"] is True


def test_pagination_context_handles_empty_collection():
    page = pagination_context(total=0, limit=10, offset=0)

    assert page["page_start"] == 0
    assert page["page_end"] == 0
    assert page["page_items"] == [1]
    assert page["has_prev"] is False
    assert page["has_next"] is False
