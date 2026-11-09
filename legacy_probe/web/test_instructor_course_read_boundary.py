"""Instructor course-read boundary at the #188 facilitator authority.

``_assigned_course_ids`` (MAIN 444ce556 parity, restored by the C3 P1 fix)
authorizes exactly at the facilitator boundary: ``_access_or_403`` over the
module-level ``facilitator_access`` seam must pass (``can_teach``) before
``authorized_offering_ids`` or any local course query runs, and an owner
failure propagates with no local fallback. DB-free: both authority seams are
stubbed at the module-level names ``app.web.instructor`` resolves, and the
db is a fake whose ``scalars`` calls are counted.
"""

from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.web import instructor


def _access(*, offering_ids=frozenset()):
    return instructor.FacilitatorAccess(
        is_admin=False,
        is_legacy_instructor=False,
        cohort_ids=frozenset(),
        offering_ids=offering_ids,
        course_cohort_ids=frozenset(),
        legacy_offering_ids=frozenset(),
        cohort_offering_ids=frozenset(),
    )


class _Scalars:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeDb:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.scalars_calls = []

    def scalars(self, statement):
        self.scalars_calls.append(statement)
        return _Scalars(self.rows)


def test_assigned_courses_raise_403_without_can_teach_and_never_query(monkeypatch):
    db = _FakeDb()
    tenant_id, instructor_id = uuid4(), uuid4()
    facilitator_calls = []
    offering_calls = []

    def facilitator(actual_db, **scope):
        facilitator_calls.append((actual_db, scope))
        return _access()  # no assignment, no staff role: can_teach is False

    def offerings(actual_db, **scope):
        offering_calls.append((actual_db, scope))
        raise AssertionError("authorized_offering_ids must not run after the 403 gate")

    monkeypatch.setattr(instructor, "facilitator_access", facilitator)
    monkeypatch.setattr(instructor, "authorized_offering_ids", offerings)

    with pytest.raises(HTTPException) as observed:
        instructor._assigned_course_ids(db, tenant_id=tenant_id, instructor_id=instructor_id)

    assert observed.value.status_code == 403
    assert facilitator_calls == [(db, {"tenant_id": tenant_id, "person_id": instructor_id})]
    assert offering_calls == []
    assert db.scalars_calls == []


def test_assigned_courses_delegate_once_and_keep_mutable_product_set(monkeypatch):
    tenant_id, instructor_id, offering_id, course_id = uuid4(), uuid4(), uuid4(), uuid4()
    access = _access(offering_ids=frozenset({offering_id}))  # can_teach is True
    owner_offering_ids = {offering_id}
    db = _FakeDb(rows=[course_id])
    facilitator_calls = []
    offering_calls = []

    def facilitator(actual_db, **scope):
        facilitator_calls.append((actual_db, scope))
        return access

    def offerings(actual_db, **scope):
        offering_calls.append((actual_db, scope))
        return owner_offering_ids

    monkeypatch.setattr(instructor, "facilitator_access", facilitator)
    monkeypatch.setattr(instructor, "authorized_offering_ids", offerings)

    result = instructor._assigned_course_ids(db, tenant_id=tenant_id, instructor_id=instructor_id)

    assert facilitator_calls == [(db, {"tenant_id": tenant_id, "person_id": instructor_id})]
    assert offering_calls == [(db, {"tenant_id": tenant_id, "access": access})]
    assert len(db.scalars_calls) == 1
    assert type(result) is set
    assert result == {course_id}
    result.clear()
    assert owner_offering_ids == {offering_id}
    assert db.rows == [course_id]


def test_assigned_courses_propagate_owner_failure_without_local_query(monkeypatch):
    db = _FakeDb()
    error = RuntimeError("owner read unavailable")

    def facilitator(actual_db, **scope):
        return _access(offering_ids=frozenset({uuid4()}))  # can_teach is True

    def offerings(*args, **kwargs):
        raise error

    monkeypatch.setattr(instructor, "facilitator_access", facilitator)
    monkeypatch.setattr(instructor, "authorized_offering_ids", offerings)

    with pytest.raises(RuntimeError) as observed:
        instructor._assigned_course_ids(db, tenant_id=uuid4(), instructor_id=uuid4())

    assert observed.value is error
    assert db.scalars_calls == []
