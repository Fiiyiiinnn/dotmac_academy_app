"""Public-fact boundary checks; run only in the authorized hosted environment."""

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import dotmac_learning as learning
import pytest
from jinja2 import (
    ChoiceLoader,
    DictLoader,
    Environment,
    FileSystemLoader,
    StrictUndefined,
)

from app.services import catalog


@pytest.fixture
def identities():
    return SimpleNamespace(db=object(), tenant_id=uuid4(), person_id=uuid4(), course_id=uuid4())


def course_fact(ids, title="Course", slug="course"):
    now = datetime(2026, 10, 17, tzinfo=timezone.utc)
    return learning.CourseFact(
        id=ids.course_id,
        tenant_id=ids.tenant_id,
        slug=slug,
        title=title,
        discipline="networking",
        source_ref="source",
        version=1,
        status="published",
        listed=True,
        created_at=now,
        updated_at=now,
    )


def chapter_fact(ids, number, title):
    return learning.ChapterFact(
        id=uuid4(),
        tenant_id=ids.tenant_id,
        course_id=ids.course_id,
        number=number,
        title=title,
        part="",
        body_md="",
        body_html="",
        source_hash="hash",
        order_index=number,
    )


def activity_fact(ids, title, chapter_number=None, kind="mcq"):
    now = datetime(2026, 10, 17, tzinfo=timezone.utc)
    return learning.ActivityFact(
        id=uuid4(),
        tenant_id=ids.tenant_id,
        course_id=ids.course_id,
        title=title,
        chapter_number=chapter_number,
        type=kind,
        bank_id=None,
        pass_threshold=0.7,
        max_attempts=None,
        assessment_mode="fixed",
        grading="auto",
        weight=1.0,
        question_count=None,
        created_at=now,
        updated_at=now,
    )


@pytest.mark.parametrize("name", ["my_courses", "all_courses"])
def test_course_lists_preserve_owner_order_and_fact_identity(monkeypatch, identities, name):
    ids = identities
    first = course_fact(ids, "Zulu", "zulu")
    second = course_fact(ids, "Alpha", "alpha")
    owner = Mock(return_value=(first, second))
    monkeypatch.setattr(learning, name, owner)
    kwargs = {"tenant_id": ids.tenant_id}
    if name == "my_courses":
        kwargs["person_id"] = ids.person_id
    result = getattr(catalog, name)(ids.db, **kwargs)
    owner.assert_called_once_with(ids.db, **kwargs)
    assert type(result) is list
    assert result[0] is first
    assert result[1] is second
    result.clear()
    assert owner.return_value == (first, second)


def test_public_catalog_preserves_counts_and_detached_course(monkeypatch, identities):
    ids = identities
    first, second = course_fact(ids, "Zulu"), course_fact(ids, "Alpha")
    owner = Mock(
        return_value=(
            learning.PublicCatalogItem(first, 0),
            learning.PublicCatalogItem(second, 17),
        )
    )
    monkeypatch.setattr(learning, "public_catalog", owner)
    result = catalog.public_catalog(ids.db, tenant_id=ids.tenant_id)
    owner.assert_called_once_with(ids.db, tenant_id=ids.tenant_id)
    assert type(result) is list
    assert all(type(item) is dict for item in result)
    assert result == [
        {"course": first, "chapter_count": 0},
        {"course": second, "chapter_count": 17},
    ]
    assert result[0]["course"] is first
    assert result[1]["course"] is second


@pytest.mark.parametrize("percentage", [0, 17, 100])
def test_completion_is_owner_result_without_rounding(monkeypatch, identities, percentage):
    ids = identities
    owner = Mock(return_value=percentage)
    monkeypatch.setattr(learning, "course_completion", owner)
    assert (
        catalog.course_completion(
            ids.db,
            tenant_id=ids.tenant_id,
            person_id=ids.person_id,
            course_id=ids.course_id,
        )
        == percentage
    )
    owner.assert_called_once_with(
        ids.db,
        tenant_id=ids.tenant_id,
        person_id=ids.person_id,
        course_id=ids.course_id,
    )


@pytest.fixture
def structure_facts(identities):
    ids = identities
    first = chapter_fact(ids, 9, "First chapter")
    empty = chapter_fact(ids, 2, "Empty chapter")
    last = chapter_fact(ids, 1, "Last chapter")
    chapter_activity = activity_fact(ids, "Chapter quiz", 9)
    final = activity_fact(ids, "Zulu final")
    lab = activity_fact(ids, "Alpha lab", kind="lab")
    # Deliberately unsorted: presentation must not redo the owner's ordering.
    return learning.CourseStructure(
        parts=(
            learning.CatalogPart(
                "B",
                (learning.CatalogChapter(first, (learning.CatalogActivity(chapter_activity, True, 83),)),),
            ),
            learning.CatalogPart(
                "",
                (learning.CatalogChapter(empty, ()), learning.CatalogChapter(last, ())),
            ),
        ),
        course_activities=(
            learning.CatalogActivity(final, False, 19),
            learning.CatalogActivity(lab, True, 100),
        ),
        continue_target=empty,
        locked=False,
    )


def test_structure_converts_every_container_and_preserves_decisions(monkeypatch, identities, structure_facts):
    ids, facts = identities, structure_facts
    owner = Mock(return_value=facts)
    monkeypatch.setattr(learning, "course_structure", owner)
    # Only id exists: no model, entitlement, or second course lookup is needed.
    result = catalog.course_structure(
        ids.db,
        tenant_id=ids.tenant_id,
        person_id=ids.person_id,
        course=SimpleNamespace(id=ids.course_id),
    )
    owner.assert_called_once_with(
        ids.db,
        tenant_id=ids.tenant_id,
        person_id=ids.person_id,
        course_id=ids.course_id,
    )
    assert type(result) is dict
    assert set(result) == {"parts", "course_activities", "continue_target", "locked"}
    assert type(result["parts"]) is list
    assert [part["part"] for part in result["parts"]] == ["B", ""]
    for actual_part, part in zip(result["parts"], facts.parts, strict=True):
        assert type(actual_part) is dict
        assert set(actual_part) == {"part", "chapters"}
        assert type(actual_part["chapters"]) is list
        for actual_chapter, chapter in zip(actual_part["chapters"], part.chapters, strict=True):
            assert type(actual_chapter) is dict
            assert set(actual_chapter) == {"chapter", "activities"}
            assert actual_chapter["chapter"] is chapter.chapter
            assert type(actual_chapter["activities"]) is list
            for actual_activity, activity in zip(actual_chapter["activities"], chapter.activities, strict=True):
                assert type(actual_activity) is dict
                assert actual_activity == {
                    "activity": activity.activity,
                    "passed": activity.passed,
                    "pct": activity.pct,
                }
                assert actual_activity["activity"] is activity.activity
    assert result["parts"][1]["chapters"][0]["activities"] == []
    assert type(result["course_activities"]) is list
    assert [item["activity"] for item in result["course_activities"]] == [
        item.activity for item in facts.course_activities
    ]
    for actual, fact in zip(result["course_activities"], facts.course_activities, strict=True):
        assert type(actual) is dict
        assert actual == {
            "activity": fact.activity,
            "passed": fact.passed,
            "pct": fact.pct,
        }
        assert actual["activity"] is fact.activity
    assert result["continue_target"] is facts.continue_target
    assert result["continue_target"] is result["parts"][1]["chapters"][0]["chapter"]
    assert result["locked"] is facts.locked


@pytest.mark.parametrize("locked", [False, True])
def test_empty_structure_retains_null_target_and_lock(monkeypatch, identities, locked):
    ids = identities
    monkeypatch.setattr(
        learning,
        "course_structure",
        Mock(return_value=learning.CourseStructure((), (), None, locked)),
    )
    result = catalog.course_structure(
        ids.db,
        tenant_id=ids.tenant_id,
        person_id=ids.person_id,
        course=course_fact(ids),
    )
    assert result == {
        "parts": [],
        "course_activities": [],
        "continue_target": None,
        "locked": locked,
    }


@pytest.mark.parametrize("name", ["my_courses", "all_courses", "public_catalog"])
def test_empty_catalog_is_a_list(monkeypatch, identities, name):
    ids = identities
    monkeypatch.setattr(learning, name, Mock(return_value=()))
    kwargs = {"tenant_id": ids.tenant_id}
    if name == "my_courses":
        kwargs["person_id"] = ids.person_id
    result = getattr(catalog, name)(ids.db, **kwargs)
    assert result == []
    assert type(result) is list


@pytest.mark.parametrize(
    "name",
    [
        "my_courses",
        "all_courses",
        "public_catalog",
        "course_completion",
        "course_structure",
    ],
)
def test_required_owner_failures_propagate(monkeypatch, identities, name):
    ids = identities
    failure = RuntimeError("owner unavailable")
    owner = Mock(side_effect=failure)
    monkeypatch.setattr(learning, name, owner)
    kwargs = {"tenant_id": ids.tenant_id}
    if name in {"my_courses", "course_completion", "course_structure"}:
        kwargs["person_id"] = ids.person_id
    if name == "course_completion":
        kwargs["course_id"] = ids.course_id
    if name == "course_structure":
        kwargs["course"] = course_fact(ids)
    with pytest.raises(RuntimeError) as caught:
        getattr(catalog, name)(ids.db, **kwargs)
    assert caught.value is failure
    assert owner.call_count == 1


@pytest.mark.parametrize("locked", [False, True])
def test_actual_course_template_renders_fact_attributes(monkeypatch, identities, structure_facts, locked):
    ids, facts = identities, structure_facts
    monkeypatch.setattr(
        learning,
        "course_structure",
        Mock(
            return_value=learning.CourseStructure(facts.parts, facts.course_activities, facts.continue_target, locked)
        ),
    )
    course = course_fact(ids)
    structure = catalog.course_structure(ids.db, tenant_id=ids.tenant_id, person_id=ids.person_id, course=course)
    # Keep the actual production content template; isolate only the site shell.
    env = Environment(
        loader=ChoiceLoader(
            [
                DictLoader({"base.html": "{% block content %}{% endblock %}"}),
                FileSystemLoader(Path(__file__).resolve().parents[2] / "templates"),
            ]
        ),
        undefined=StrictUndefined,
        autoescape=True,
    )
    rendered = env.get_template("learn/course.html").render(course=course, structure=structure, pct=17)
    assert "First chapter" in rendered
    assert "Empty chapter" in rendered
    assert "Chapter quiz" in rendered
    assert "83%" in rendered
    assert "Course assessments" in rendered
    assert rendered.index("Zulu final") < rendered.index("Alpha lab")
    if locked:
        assert "Prerequisite not yet completed" in rendered
        assert "Continue &rarr;" not in rendered
        assert f"/activities/{facts.course_activities[0].activity.id}" not in rendered
        assert f"/labs/{facts.course_activities[1].activity.id}" not in rendered
    else:
        assert "/courses/course/chapters/2" in rendered
        assert "Continue &rarr;" in rendered
        assert f"/activities/{facts.course_activities[0].activity.id}" in rendered
        assert f"/labs/{facts.course_activities[1].activity.id}" in rendered
