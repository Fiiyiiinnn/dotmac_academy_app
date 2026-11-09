"""Authored facade canaries; hosted installed-package execution remains owed."""

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from dotmac_learning import ChapterFact, CourseFact, SearchAuthority, SearchChapter, SearchResults

from app.services import search as facade


class _NoDatabaseUse:
    def __getattr__(self, name):
        pytest.fail(f"Search presentation facade touched caller database method {name}")


_SEARCH_LIMIT = 17  # Deliberately not the product default, to prove the policy value is what is passed.


@pytest.fixture(autouse=True)
def _search_policy(monkeypatch):
    """The facade reads its limit from the strict settings store on the caller session.

    That read is the one deliberate database touch (C4(14)); stub only the policy
    reader so ``_NoDatabaseUse`` still guards every other use of the session.
    """

    def effective(db, *, require_store):
        assert require_store is True, "the search limit must come from the strict store"
        return {"learning_search_limit": _SEARCH_LIMIT}

    monkeypatch.setattr(facade, "effective", effective)


def _course(tenant, index=1):
    now = datetime(2026, 10, 18, tzinfo=UTC)
    return CourseFact(
        id=uuid4(),
        tenant_id=tenant,
        slug=f"course-{index}",
        title=f"Title {index}",
        discipline="networking",
        source_ref="synthetic",
        version=1,
        status="published",
        listed=True,
        created_at=now,
        updated_at=now,
    )


def _chapter(course, number=7, body="<p>Prefix Needle suffix.</p>"):
    return SearchChapter(
        chapter=ChapterFact(
            id=uuid4(),
            tenant_id=course.tenant_id,
            course_id=course.id,
            number=number,
            title=f"Chapter {number}",
            part="I",
            body_md="",
            body_html=body,
            source_hash="synthetic",
            order_index=number,
        ),
        course_slug=course.slug,
    )


@pytest.mark.parametrize("is_staff", [False, True])
def test_same_session_raw_query_once_and_exact_trusted_authority(monkeypatch, is_staff):
    db, tenant, person = _NoDatabaseUse(), uuid4(), uuid4()
    raw_query = " \tNeedle\n "
    calls = []
    course = _course(tenant)
    facts = SearchResults("Needle", (course,), (_chapter(course),))

    def delegated(session, **kwargs):
        calls.append((session, kwargs))
        return facts

    monkeypatch.setattr(facade, "search_learning", delegated)
    result = facade.search(db, tenant_id=tenant, person_id=person, q=raw_query, is_staff=is_staff)
    assert len(calls) == 1
    session, values = calls[0]
    assert session is db
    assert values == {
        "tenant_id": tenant,
        "person_id": person,
        "q": raw_query,
        "authority": SearchAuthority(tenant, person, is_staff),
        "limit": _SEARCH_LIMIT,
    }
    assert type(values["authority"]) is SearchAuthority
    assert result == {
        "courses": [{"slug": course.slug, "title": course.title}],
        "chapters": [
            {
                "title": "Chapter 7",
                "course_slug": course.slug,
                "chapter_number": 7,
                "snippet": "Prefix Needle suffix.",
            }
        ],
    }


@pytest.mark.parametrize("query,term", [("", ""), (" \t\n", ""), ("unavailable", "unavailable")])
def test_empty_and_no_access_results_are_owned_by_learning(monkeypatch, query, term):
    db, tenant, person = _NoDatabaseUse(), uuid4(), uuid4()
    calls = []

    def delegated(session, **kwargs):
        calls.append((session, kwargs))
        return SearchResults(term, (), ())

    monkeypatch.setattr(facade, "search_learning", delegated)
    result = facade.search(db, tenant_id=tenant, person_id=person, q=query, is_staff=False)
    assert len(calls) == 1
    assert calls[0][0] is db
    assert calls[0][1]["q"] == query
    assert result == {"courses": [], "chapters": []}
    assert type(result) is dict and type(result["courses"]) is list and type(result["chapters"]) is list


def test_owner_fact_order_and_population_are_not_sorted_or_recapped(monkeypatch):
    tenant, person = uuid4(), uuid4()
    courses = tuple(_course(tenant, index) for index in range(23, 0, -1))
    chapters = tuple(_chapter(course, index) for index, course in enumerate(courses, 1))
    facts = SearchResults("Needle", courses, chapters)
    seen = []

    def owner(*args, **kwargs):
        seen.append(kwargs)
        return facts

    monkeypatch.setattr(facade, "search_learning", owner)
    # The limit is a policy value handed to the owner, never re-applied here.
    monkeypatch.setattr(facade, "effective", lambda db, **kw: {"learning_search_limit": 7})
    result = facade.search(_NoDatabaseUse(), tenant_id=tenant, person_id=person, q="Needle", is_staff=True)
    assert [kwargs["limit"] for kwargs in seen] == [7]
    assert len(result["courses"]) == 23 and len(result["chapters"]) == 23
    assert [row["slug"] for row in result["courses"]] == [course.slug for course in courses]
    assert [row["course_slug"] for row in result["chapters"]] == [hit.course_slug for hit in chapters]
    assert [row["chapter_number"] for row in result["chapters"]] == list(range(1, 24))
    assert all(type(row) is dict and set(row) == {"slug", "title"} for row in result["courses"])
    assert all(
        type(row) is dict and set(row) == {"title", "course_slug", "chapter_number", "snippet"}
        for row in result["chapters"]
    )


def test_snippet_uses_module_trimmed_term_and_same_html(monkeypatch):
    tenant, person = uuid4(), uuid4()
    course = _course(tenant)
    hit = _chapter(course, body="<p>" + "x" * 150 + " Needle " + "y" * 150 + "</p>")
    facts = SearchResults("Needle", (), (hit,))
    original_snippet = facade._extract_snippet
    calls = []

    def snippet(html, term, radius=100):
        calls.append((html, term, radius))
        return original_snippet(html, term, radius)

    monkeypatch.setattr(facade, "search_learning", lambda *args, **kwargs: facts)
    monkeypatch.setattr(facade, "_extract_snippet", snippet)
    result = facade.search(_NoDatabaseUse(), tenant_id=tenant, person_id=person, q="  Needle\n", is_staff=False)
    assert calls == [(hit.chapter.body_html, "Needle", 100)]
    assert result["chapters"][0]["snippet"].startswith("…")
    assert result["chapters"][0]["snippet"].endswith("…")
    assert "<p>" not in result["chapters"][0]["snippet"]


def test_mutable_legacy_containers_do_not_mutate_frozen_nested_facts(monkeypatch):
    tenant, person = uuid4(), uuid4()
    course = _course(tenant)
    hit = _chapter(course)
    facts = SearchResults("Needle", (course,), (hit,))
    monkeypatch.setattr(facade, "search_learning", lambda *args, **kwargs: facts)
    result = facade.search(_NoDatabaseUse(), tenant_id=tenant, person_id=person, q="Needle", is_staff=False)
    result["courses"][0]["title"] = "Presentation mutation"
    result["chapters"][0]["chapter_number"] = 999
    result["courses"].append({"slug": "extra", "title": "Extra"})
    result["chapters"].clear()
    assert facts.courses == (course,) and facts.chapters == (hit,)
    assert course.title == "Title 1" and hit.chapter.number == 7
    with pytest.raises(FrozenInstanceError):
        course.title = "forbidden"
    with pytest.raises(FrozenInstanceError):
        hit.chapter.number = 999


def test_learning_failure_propagates_unchanged(monkeypatch):
    failure = ValueError("canonical search failure")
    calls = []

    def delegated(session, **kwargs):
        calls.append((session, kwargs))
        raise failure

    monkeypatch.setattr(facade, "search_learning", delegated)
    db, tenant, person = _NoDatabaseUse(), uuid4(), uuid4()
    with pytest.raises(ValueError) as raised:
        facade.search(db, tenant_id=tenant, person_id=person, q="Needle", is_staff=False)
    assert raised.value is failure
    assert len(calls) == 1 and calls[0][0] is db
