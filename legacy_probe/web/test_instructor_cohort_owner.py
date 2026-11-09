"""DB-free caller proof using Learning's installed public cohort command.

The genuine owner stages into an unbound SQLAlchemy session. Hosted tests still
own persistence, RLS and HTTP authentication/CSRF qualification.
"""

import ast
import inspect
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import dotmac_learning as learning
import pytest
from fastapi import HTTPException, Request
from sqlalchemy.orm import Session

from app.web import instructor


def _request(*, htmx=False, tenant=True):
    request = Request({"type": "http", "headers": [(b"hx-request", b"true")] if htmx else []})
    if tenant:
        request.state.tenant = SimpleNamespace(id=uuid4())
    return request


def _access(kind):
    return instructor.FacilitatorAccess(
        is_admin=kind == "admin",
        is_legacy_instructor=kind == "instructor",
        cohort_ids=frozenset({uuid4()}) if kind == "scoped" else frozenset(),
        offering_ids=frozenset(),
        course_cohort_ids=frozenset(),
        legacy_offering_ids=frozenset(),
        cohort_offering_ids=frozenset(),
    )


@pytest.mark.parametrize("kind", ["admin", "instructor"])
@pytest.mark.parametrize("htmx", [False, True])
def test_create_delegates_to_genuine_owner_in_same_transaction(monkeypatch, kind, htmx):
    request = _request(htmx=htmx)
    person = SimpleNamespace(id=uuid4())
    calls = []
    # This seam must remain the real installed public owner, not a local facade.
    assert instructor.create_learning_cohort is learning.create_cohort

    def access(db, **scope):
        calls.append((db, scope))
        return _access(kind)

    def unexpected(*args, **kwargs):
        pytest.fail("cohort creation must only stage in the caller transaction")

    monkeypatch.setattr(instructor, "facilitator_access", access)
    with Session() as db, db.begin():
        root = db.get_transaction()
        monkeypatch.setattr(db, "execute", unexpected)
        monkeypatch.setattr(db, "flush", unexpected)
        monkeypatch.setattr(db, "commit", unexpected)
        response = instructor.create_cohort(
            request, name="  Abuja 2026  ", discipline="", person=person, db=db
        )
        assert calls == [(db, {"tenant_id": request.state.tenant.id, "person_id": person.id})]
        assert db.get_transaction() is root
        assert db.get_nested_transaction() is None
        (cohort,) = db.new
        assert type(cohort) is instructor.Cohort
        assert cohort.tenant_id == request.state.tenant.id
        assert cohort.name == "  Abuja 2026  "
        assert cohort.discipline == ""
        assert cohort.status == "active"
        assert cohort.id is None
        assert cohort.created_at is None
        assert response.status_code == (200 if htmx else 303)
        assert response.headers["hx-redirect" if htmx else "location"] == "/instructor/cohorts"
        # Prevent this test's own context manager from flushing the staged row.
        db.rollback()


@pytest.mark.parametrize("kind", ["scoped", "student"])
def test_create_denies_non_global_actor_before_owner(monkeypatch, kind):
    request = _request()
    person = SimpleNamespace(id=uuid4())
    db = object()
    calls = []

    def access(actual_db, **scope):
        calls.append((actual_db, scope))
        return _access(kind)

    def unexpected(*args, **kwargs):
        pytest.fail("owner must not run for a denied actor")

    monkeypatch.setattr(instructor, "facilitator_access", access)
    monkeypatch.setattr(instructor, "create_learning_cohort", unexpected)
    with pytest.raises(HTTPException) as observed:
        instructor.create_cohort(request, name="Denied", discipline="net", person=person, db=db)
    assert observed.value.status_code == 403
    assert calls == [(db, {"tenant_id": request.state.tenant.id, "person_id": person.id})]


def test_create_requires_tenant_before_authorization_or_owner(monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("missing tenant must fail before authority or writes")

    monkeypatch.setattr(instructor, "facilitator_access", unexpected)
    monkeypatch.setattr(instructor, "create_learning_cohort", unexpected)
    with pytest.raises(HTTPException) as observed:
        instructor.create_cohort(
            _request(tenant=False), name="Missing", discipline="net", person=SimpleNamespace(id=uuid4()), db=object()
        )
    assert observed.value.status_code == 404


def test_create_propagates_genuine_owner_transaction_failure_without_fallback(monkeypatch):
    monkeypatch.setattr(instructor, "facilitator_access", lambda *args, **kwargs: _access("instructor"))
    with Session() as db:
        # Genuine a3 requires an existing caller transaction; no autobegin fallback.
        with pytest.raises(learning.TransactionChanged):
            instructor.create_cohort(
                _request(), name="No transaction", discipline="net", person=SimpleNamespace(id=uuid4()), db=db
            )
        assert not db.new
        assert db.get_transaction() is None


def test_route_has_no_local_write_and_retains_instructor_dependency():
    source = inspect.getsource(instructor.create_cohort)
    route = ast.parse(source).body[0]
    assert isinstance(route, ast.FunctionDef)
    calls = [node for node in ast.walk(route) if isinstance(node, ast.Call)]
    assert any(isinstance(call.func, ast.Name) and call.func.id == "create_learning_cohort" for call in calls)
    assert not any(
        isinstance(call.func, ast.Attribute) and isinstance(call.func.value, ast.Name) and call.func.value.id == "db"
        for call in calls
    )
    assert not any(isinstance(call.func, ast.Name) and call.func.id == "Cohort" for call in calls)
    assert 'Depends(require_web_role("instructor"))' in source


@pytest.mark.parametrize("status_value", ["draft", "published", "completed"])
@pytest.mark.parametrize("htmx", [False, True])
def test_course_status_uses_genuine_selective_owner_after_actor_gate(monkeypatch, status_value, htmx):
    request = _request(htmx=htmx)
    person = SimpleNamespace(id=uuid4())
    course_id = uuid4()
    calls = []
    assert instructor.patch_course is learning.patch_course
    # Complete detached fact fixture for the real owner's projection; no DB.
    values = dict(
        id=course_id, tenant_id=request.state.tenant.id, slug="course", title="Course",
        discipline="net", source_ref="author", version=5, status="published", listed=True,
        created_at=datetime.now(UTC), updated_at=datetime.now(UTC), completion_fraction=0.8,
        certificate_auto_issue=False, certificate_validity_days=60, prerequisite_composition="any",
    )
    row = SimpleNamespace(**values)

    def authorize(db, **scope):
        calls.append(("authorize", db, scope))
        return row

    def unexpected(*args, **kwargs):
        pytest.fail("status update must not flush or commit caller work")

    monkeypatch.setattr(instructor, "_authorable_course_or_404", authorize)
    with Session() as db, db.begin():
        root = db.get_transaction()

        def scalar(statement):
            assert len(calls) == 1  # authorization precedes owner lookup
            assert db.autoflush is False
            compiled = statement.compile()
            assert set(compiled.params.values()) == {course_id, request.state.tenant.id}
            calls.append(("owner", db))
            return row

        monkeypatch.setattr(db, "scalar", scalar)
        monkeypatch.setattr(db, "flush", unexpected)
        monkeypatch.setattr(db, "commit", unexpected)
        response = instructor.set_course_status(
            course_id, request, status_value=status_value, person=person, db=db
        )
        assert calls == [
            ("authorize", db, {"tenant_id": request.state.tenant.id, "person_id": person.id, "course_id": course_id}),
            ("owner", db),
        ]
        assert vars(row) == {**values, "status": status_value}
        assert db.get_transaction() is root
        assert db.autoflush is True
        assert response.status_code == (200 if htmx else 303)
        assert response.headers["hx-redirect" if htmx else "location"] == f"/instructor/courses/{course_id}/edit"
        db.rollback()


@pytest.mark.parametrize("status_code", [403, 404])
def test_course_status_authorization_failure_prevents_owner(monkeypatch, status_code):
    failure = HTTPException(status_code=status_code)

    def authorize(*args, **kwargs):
        raise failure

    def unexpected(*args, **kwargs):
        pytest.fail("owner must not run when course access fails")

    monkeypatch.setattr(instructor, "_authorable_course_or_404", authorize)
    monkeypatch.setattr(instructor, "patch_course", unexpected)
    with pytest.raises(HTTPException) as observed:
        instructor.set_course_status(
            uuid4(), _request(), status_value="draft", person=SimpleNamespace(id=uuid4()), db=object()
        )
    assert observed.value is failure


def test_course_status_invalid_value_fails_before_authorization(monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("invalid status must fail before course access")

    monkeypatch.setattr(instructor, "_authorable_course_or_404", unexpected)
    with pytest.raises(HTTPException) as observed:
        instructor.set_course_status(
            uuid4(), _request(), status_value="archived", person=SimpleNamespace(id=uuid4()), db=object()
        )
    assert observed.value.status_code == 400
    assert observed.value.detail == "invalid status"


def test_course_status_owner_failure_propagates_without_local_write(monkeypatch):
    row = SimpleNamespace(status="published")
    monkeypatch.setattr(instructor, "_authorable_course_or_404", lambda *args, **kwargs: row)
    with Session() as db:
        monkeypatch.setattr(db, "scalar", lambda statement: None)
        with pytest.raises(HTTPException) as observed:
            instructor.set_course_status(
                uuid4(), _request(), status_value="draft", person=SimpleNamespace(id=uuid4()), db=db
            )
        assert observed.value.status_code == 404
        assert isinstance(observed.value.__cause__, learning.ReferenceNotFound)
        assert row.status == "published"
        assert not db.new
        assert db.autoflush is True


def test_course_status_route_has_no_local_model_assignment():
    route = ast.parse(inspect.getsource(instructor.set_course_status)).body[0]
    assert not any(
        isinstance(node, ast.Assign) and any(isinstance(target, ast.Attribute) for target in node.targets)
        for node in ast.walk(route)
    )
