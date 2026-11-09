"""Source-boundary canaries; installed Learning/PostgreSQL qualification is owed.

Academy's legacy admin fixtures are not the owner's canonical learning@head
fixture. These canaries neither install that schema nor claim database parity.
"""

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import dotmac_learning as learning
import pytest

from app.services import scheduling
from app.services.exceptions import BadRequestError, NotFoundError
from app.services.learning_policy import LearningCreationDefaults
from legacy_probe.services.roster_request_fixtures import roster_actors

assert roster_actors


def _creation_defaults(session):
    return LearningCreationDefaults(
        prerequisite_default_kind="course_completed",
        prerequisite_default_composition="all",
        track_default_gating_mode="previous",
        completion_fraction=1.0,
        certificate_auto_issue=True,
        certificate_validity_days=None,
        activity_scoring_policy="best",
        attempt_cooldown_seconds=0,
        session_promotes_delivery_mode=True,
        pacing_default_open=True,
    )


NOW = datetime(2026, 10, 18, tzinfo=UTC)


def _fact(**values):
    return learning.ClassSessionFact(
        **(
            {
                "id": uuid4(),
                "tenant_id": uuid4(),
                "cohort_id": uuid4(),
                "offering_id": None,
                "session_type": "live_class",
                "title": "Session",
                "starts_at": NOW,
                "ends_at": None,
                "instructor_person_id": None,
                "location": None,
                "join_url": None,
                "notes": None,
                "status": "scheduled",
                "created_at": NOW,
                "updated_at": NOW,
            }
            | values
        )
    )


@pytest.mark.parametrize("optional", [None, "", "  raw  "])
def test_create_passes_same_session_raw_typed_fields_and_fact(monkeypatch, optional):
    db = object()
    tenant, cohort, offering, instructor = (uuid4() for _ in range(4))
    result = _fact(tenant_id=tenant, cohort_id=cohort)
    values = dict(
        tenant_id=tenant,
        cohort_id=cohort,
        title="  title  ",
        starts_at=NOW,
        session_type="lab",
        ends_at=None,
        offering_id=offering,
        instructor_person_id=instructor,
        location=optional,
        join_url=optional,
        notes=optional,
    )

    def owner(actual_db, command):
        assert actual_db is db
        assert isinstance(command, learning.CreateSessionCommand)
        assert command.model_dump() == values | {"promote_delivery_mode": True}
        assert command.starts_at is NOW
        return result

    monkeypatch.setattr(scheduling, "creation_defaults", _creation_defaults)
    monkeypatch.setattr(learning, "create_session", owner)
    assert scheduling.create_session(db, **values) is result


@pytest.mark.parametrize("upcoming,now", [(False, None), (True, NOW)])
def test_list_preserves_owner_order_container_fields_and_scope(monkeypatch, upcoming, now):
    db = object()
    tenant, cohort = uuid4(), uuid4()
    first, second = _fact(), _fact(title="Second")

    def owner(actual_db, **values):
        assert actual_db is db
        assert values == dict(tenant_id=tenant, cohort_id=cohort, upcoming_only=upcoming, now=now)
        return (second, first)

    monkeypatch.setattr(learning, "list_cohort_sessions", owner)
    result = scheduling.list_for_cohort(db, tenant_id=tenant, cohort_id=cohort, upcoming_only=upcoming, now=now)
    assert isinstance(result, list)
    assert result == [second, first]
    assert result[0] is second
    assert (
        result[0].title,
        result[0].starts_at,
        result[0].ends_at,
        result[0].created_at,
        result[0].updated_at,
        result[0].location,
        result[0].join_url,
        result[0].notes,
        result[0].status,
        result[0].session_type,
    ) == ("Second", NOW, None, NOW, NOW, None, None, None, "scheduled", "live_class")


@pytest.mark.parametrize("operation", ["get_session", "cancel_session", "set_delivery_mode"])
def test_scoped_public_operations_bind_actual_owner_api(monkeypatch, operation):
    db = object()
    tenant, identity = uuid4(), uuid4()
    result = _fact() if operation != "set_delivery_mode" else learning.CohortDeliveryFact(identity, tenant, "live")
    values = {"tenant_id": tenant, "cohort_id" if operation == "set_delivery_mode" else "session_id": identity}
    if operation == "set_delivery_mode":
        values["mode"] = "live"

    def owner(actual_db, *args, **kwargs):
        assert actual_db is db
        if operation == "get_session":
            assert not args
            assert kwargs == values
        else:
            assert not kwargs
            (command,) = args
            assert isinstance(
                command,
                learning.SetDeliveryModeCommand if operation == "set_delivery_mode" else learning.CancelSessionCommand,
            )
            assert command.model_dump() == values
        return result

    monkeypatch.setattr(learning, operation, owner)
    assert getattr(scheduling, operation)(db, **values) is result


@pytest.mark.parametrize(
    "operation", ["list_for_cohort", "create_session", "get_session", "cancel_session", "set_delivery_mode"]
)
@pytest.mark.parametrize(
    "error_type,translated",
    [
        (learning.SchedulingBadRequest, BadRequestError),
        (learning.SchedulingNotFound, NotFoundError),
        (RuntimeError, RuntimeError),
    ],
)
def test_domain_translation_and_unexpected_failure_propagation(monkeypatch, operation, error_type, translated):
    error = error_type("owner detail")

    def owner(*args, **kwargs):
        raise error

    monkeypatch.setattr(learning, "list_cohort_sessions" if operation == "list_for_cohort" else operation, owner)
    values = {"tenant_id": uuid4()}
    if operation in {"create_session", "list_for_cohort", "set_delivery_mode"}:
        values["cohort_id"] = uuid4()
    else:
        values["session_id"] = uuid4()
    if operation == "create_session":
        values.update(title="Class", starts_at=NOW, session_type="live_class")
        monkeypatch.setattr(scheduling, "creation_defaults", _creation_defaults)
    if operation == "set_delivery_mode":
        values["mode"] = "live"
    with pytest.raises(translated, match="owner detail") as caught:
        getattr(scheduling, operation)(object(), **values)
    if error_type is RuntimeError:
        assert caught.value is error
    else:
        assert caught.value.__cause__ is error


def test_genuine_update_preserves_trimming_clearing_changes_and_detached_fact(roster_actors):
    from legacy_probe.services.test_genuine_scheduling_context import _case, _invoke

    sid, _ = _case(roster_actors)
    result, changes = _invoke(roster_actors, sid, title=" After ", location=None, notes="")
    assert type(result) is learning.ClassSessionFact
    assert result.title == "After"
    assert result.location is None
    assert result.notes is None
    assert changes == {
        "title": {"from": "Before", "to": "After"},
        "location": {"from": "Room", "to": None},
        "notes": {"from": "Note", "to": None},
    }


@pytest.mark.parametrize(
    "route_name,service_name",
    [
        ("cohort_timetable", "list_for_cohort"),
        ("cancel_class_session", "cancel_session"),
        ("edit_class_session_form", "get_session"),
        ("set_cohort_delivery_mode", "set_delivery_mode"),
    ],
)
def test_routes_forward_authenticated_tenant_without_new_session_lookup(monkeypatch, route_name, service_name):
    from app.web import instructor

    request, db = object(), object()
    tenant, cohort, session_id = uuid4(), uuid4(), uuid4()
    calls = []

    def require_tenant(actual_request):
        assert actual_request is request
        calls.append("tenant")
        return SimpleNamespace(id=tenant)

    def cohort_or_404(actual_db, **values):
        assert actual_db is db
        assert values == {"tenant_id": tenant, "cohort_id": cohort}
        calls.append("cohort")
        return SimpleNamespace(id=cohort)

    def service(actual_db, **values):
        assert actual_db is db
        assert calls[0] == "tenant"
        assert values["tenant_id"] == tenant
        if service_name in {"list_for_cohort", "set_delivery_mode"}:
            assert values["cohort_id"] == cohort
        else:
            assert values["session_id"] == session_id
        if service_name == "set_delivery_mode":
            assert values["mode"] == "live"
        calls.append("service")
        return [] if service_name == "list_for_cohort" else _fact(cohort_id=cohort)

    monkeypatch.setattr(instructor, "require_tenant", require_tenant)
    monkeypatch.setattr(instructor, "cohort_or_404", cohort_or_404)
    monkeypatch.setattr(scheduling, service_name, service)
    monkeypatch.setattr(instructor, "templates", SimpleNamespace(TemplateResponse=lambda *args: args))
    monkeypatch.setattr(instructor, "_timetable_redirect", lambda *args: args)
    values = {"request": request, "db": db}
    values["cohort_id" if service_name in {"list_for_cohort", "set_delivery_mode"} else "session_id"] = (
        cohort if service_name in {"list_for_cohort", "set_delivery_mode"} else session_id
    )
    if service_name == "set_delivery_mode":
        values["mode"] = "live"
    if route_name == "cohort_timetable":
        person_id = uuid4()
        values["person"] = SimpleNamespace(id=person_id)

        def cohort_access(actual_db, **scope):
            assert actual_db is db
            assert scope == {"tenant_id": tenant, "person_id": person_id, "cohort_id": cohort}
            calls.append("access")

        def effective(actual_db, *, require_store):
            assert actual_db is db and require_store is True
            return {"learning_default_session_type": "live_class"}

        monkeypatch.setattr(instructor, "_cohort_access_or_403", cohort_access)
        monkeypatch.setattr(instructor, "effective", effective)
    getattr(instructor, route_name)(**values)
    assert calls.count("tenant") == 1
    assert calls.count("service") == 1
    if route_name == "cohort_timetable":
        assert calls.count("access") == 1


def test_genuine_update_missing_keeps_product_error(roster_actors):
    from legacy_probe.services.test_genuine_scheduling_context import _invoke

    with pytest.raises(NotFoundError, match="Session not found."):
        _invoke(roster_actors, uuid4(), title="Changed")
