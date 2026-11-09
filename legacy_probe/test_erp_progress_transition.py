"""Pure conservation/refusal tests; positive hosted catalog proof remains open."""

import copy
import json
from types import SimpleNamespace

import pytest

from app.maintenance import erp_progress_transition as transition
from app.maintenance import erp_successor_catalog as catalog
from app.maintenance.product_adoption import ProductAdoptionRefused
from legacy_probe.test_catalog_json_codec import wire_row


def _packets():
    before = {
        cat: {}
        for cat in (
            "pg_class",
            "pg_type",
            "pg_proc",
            "pg_constraint",
            "pg_trigger",
            "pg_rewrite",
            "pg_policy",
            "pg_attribute",
            "pg_attrdef",
            "pg_index",
        )
    }
    before.update(global_=None)
    before.pop("global_")
    before["global"] = {"roles": (("app_admin", False),), "namespaces": (("public",),)}
    before["dependencies"] = ()
    before["pg_class"][100] = json.dumps(
        wire_row(
            "pg_class", 100, relname="lab_templates", relnatts=1, relhastriggers=False, relhasindex=False, relowner="10"
        )
    )
    before["pg_attribute"][(100, 1)] = json.dumps(wire_row("pg_attribute", (100, 1), attname="id"))
    after = copy.deepcopy(before)
    rel = json.loads(after["pg_class"][100])
    rel["relnatts"] = 2
    after["pg_class"][100] = json.dumps(rel)
    after["pg_attribute"][(100, 2)] = json.dumps(wire_row("pg_attribute", (100, 2), attname="access"))
    after["pg_attrdef"][200] = json.dumps(wire_row("pg_attrdef", 200, adrelid="100", adnum=2))
    return before, after


def test_only_validated_column_addition_is_inverse_projected():
    before, after = _packets()
    appended = catalog._validate_new_raw(before, after, "a007_lab_template_access", {}, set())
    assert appended == {(100, 2)}
    catalog.conserve(before, after, stage="a007_lab_template_access", appended=appended)


@pytest.mark.parametrize(
    ("category", "raw"),
    [
        ("pg_proc", {"proname": "unexpected_function"}),
        ("pg_policy", {"polname": "unexpected_policy", "polrelid": 100}),
        ("pg_constraint", {"conname": "unexpected_constraint", "contype": "c"}),
        ("pg_type", {"typname": "unexpected_type"}),
        ("pg_class", {"relname": "unexpected_index", "relkind": "i"}),
        ("pg_trigger", {"tgname": "unexpected_trigger", "tgrelid": 100}),
        ("pg_rewrite", {"rulename": "unexpected_rule"}),
    ],
)
def test_extra_objects_cannot_disappear_in_inverse_projection(category, raw):
    before, after = _packets()
    raw = wire_row(category, 999, **raw)
    if category == "pg_policy":
        raw["polrelid"] = "100"
    if category == "pg_trigger":
        raw["tgrelid"] = "100"
    after[category][999] = json.dumps(raw)
    with pytest.raises(ProductAdoptionRefused):
        catalog._validate_new_raw(before, after, "a007_lab_template_access", {}, set())


@pytest.mark.parametrize("change", ["owner", "dropped_oid", "old_attribute", "role", "namespace"])
def test_previous_objects_and_authority_are_conserved(change):
    before, after = _packets()
    if change == "owner":
        rel = json.loads(after["pg_class"][100])
        rel["relowner"] = "999"
        after["pg_class"][100] = json.dumps(rel)
    elif change == "dropped_oid":
        after["pg_class"][999] = after["pg_class"].pop(100)
    elif change == "old_attribute":
        after["pg_attribute"][(100, 1)] = json.dumps(wire_row("pg_attribute", (100, 1), attname="wrong"))
    elif change == "role":
        after["global"]["roles"] = (("app_admin", True),)
    else:
        after["global"]["namespaces"] = (("public",), ("unexpected",))
    with pytest.raises(ProductAdoptionRefused):
        catalog.conserve(before, after, stage="a007_lab_template_access", appended={(100, 2)})


@pytest.mark.parametrize("default", ["'{}'::jsonb", "dangerous()", "'[]'::jsonb || '[1]'::jsonb"])
def test_wrong_defaults_refuse_before_catalog_side_effects(default):
    row = (2, "access", "jsonb", False, default, "", "", None, False, False, None, None, 0)
    with pytest.raises(ProductAdoptionRefused):
        catalog._validate_column(None, row, ("access", "jsonb", False, "'[]'::jsonb"), appended=True)


def test_unissued_continuation_cannot_authorize_ddl():
    db = SimpleNamespace(info={})
    with pytest.raises(ProductAdoptionRefused):
        transition._consume(db)
    with pytest.raises(RuntimeError, match="privately issued"):
        transition._Continuation()


@pytest.mark.parametrize("fault", [RuntimeError("late Alembic callback"), KeyboardInterrupt()])
def test_runner_poison_covers_baseexception_and_disposes(monkeypatch, fault):
    owner = object()
    entry = object.__new__(transition._Continuation)
    packet = {"observed": ()}
    transition._Continuation.__init__(
        entry, None, object(), (), (), (None, None, None, None, ((), ())), 1, owner, True, object(), (), 2, 3, packet
    )
    db = SimpleNamespace(info={transition._KEY: entry})
    entry.connection = db
    poisoned = []
    monkeypatch.setattr(transition, "_verify_config", lambda *a: None)
    monkeypatch.setattr(transition, "_prepare", lambda *a, **k: entry)
    monkeypatch.setattr(transition, "_check", lambda *a, **k: None)
    monkeypatch.setattr(transition, "_config", lambda *a: SimpleNamespace(attributes={"connection": db}))
    monkeypatch.setattr(transition, "_poison", lambda connection, current: poisoned.append((connection, current)))
    monkeypatch.setattr(transition.sa.event, "listen", lambda *a: None)
    monkeypatch.setattr(transition.sa.event, "remove", lambda *a: None)
    monkeypatch.setattr(catalog, "observe", lambda *a: packet)
    monkeypatch.setattr(catalog, "row_evidence", lambda *a: ())

    def callback(*a, **k):
        raise fault

    monkeypatch.setattr(transition.command, "upgrade", callback)
    with pytest.raises(type(fault)):
        transition._run(db, owner, historical=True)
    assert poisoned == [(db, entry)]
    assert entry.runner is None and not entry.packet
    assert transition._KEY not in db.info


def test_dependency_multiset_cannot_lose_one_identical_policy_edge():
    before, after = _packets()
    edge = (1, 2, 0, 3, 4, 0, "n")
    before["dependencies"] = (edge, edge)
    after["dependencies"] = (edge,)
    with pytest.raises(ProductAdoptionRefused):
        catalog.conserve(before, after, stage="a007_lab_template_access", appended={(100, 2)})


@pytest.mark.parametrize("mutation", ["learning_fact", "membership", "progress_marker"])
def test_successful_late_callback_is_rechecked_after_final_version_write(monkeypatch, mutation):
    """Mock only orchestration prerequisites; exercise the real final row gate."""
    from uuid import uuid4

    tenant, completion = uuid4(), uuid4()
    owner = object()
    packet = {"observed": ()}
    state = {"rows": ("unchanged",), "membership": ((tenant, completion),), "markers_empty": True, "version": None}
    entry = object.__new__(transition._Continuation)
    transition._Continuation.__init__(
        entry,
        None,
        object(),
        (),
        (),
        (None, None, None, None, ((), ())),
        1,
        owner,
        True,
        object(),
        ((tenant, completion, None),),
        2,
        3,
        packet,
    )
    db = SimpleNamespace(info={transition._KEY: entry}, scalar=lambda statement: state["markers_empty"])
    entry.connection = db
    poisoned = []
    monkeypatch.setattr(transition, "_verify_config", lambda *a: None)
    monkeypatch.setattr(transition, "_prepare", lambda *a, **k: entry)
    monkeypatch.setattr(transition, "_check", lambda *a, **k: None)
    monkeypatch.setattr(transition, "_config", lambda *a: SimpleNamespace(attributes={"connection": db}))
    monkeypatch.setattr(transition, "_poison", lambda connection, current: poisoned.append(current))
    monkeypatch.setattr(transition.sa.event, "listen", lambda *a: None)
    monkeypatch.setattr(transition.sa.event, "remove", lambda *a: None)
    monkeypatch.setattr(catalog, "observe", lambda *a: packet)
    monkeypatch.setattr(catalog, "row_evidence", lambda *a, **k: state["rows"])
    monkeypatch.setattr(catalog, "grandfather_keys", lambda *a: ())
    monkeypatch.setattr(catalog, "validate_stage", lambda *a, **k: None)
    monkeypatch.setattr(transition.retained, "_stage_versions", lambda graph, target, **k: (target,))
    monkeypatch.setattr(transition.retained, "_version_rows", lambda *a: (state["version"],))
    monkeypatch.setattr(transition.history, "_query", lambda *a, **k: state["membership"])

    def upgrade(config, target):
        state["version"] = target
        if target == transition._A011:
            entry.consumed = entry.recorded = True
            entry.row_packet = packet
            entry.row_oracle = state["rows"]
            # These simulate callbacks which return success after the migration's
            # own conservation check; the final version row is already written.
            if mutation == "learning_fact":
                state["rows"] = ("changed",)
            elif mutation == "membership":
                state["membership"] = ()
            else:
                state["markers_empty"] = False

    monkeypatch.setattr(transition.command, "upgrade", upgrade)
    with pytest.raises(ProductAdoptionRefused):
        transition._run(db, owner, historical=True)
    assert state["version"] == transition._A011
    assert poisoned == [entry]
    assert transition._KEY not in db.info


def test_fresh_a006_counter_alone_cannot_authorize_any_learned_catalog(monkeypatch):
    from app.maintenance import fresh_product as fresh

    owner = SimpleNamespace(prepared=True, next_stage=len(fresh._STAGES))
    db = SimpleNamespace(info={})
    monkeypatch.setattr(fresh, "_issued", lambda connection: owner)
    monkeypatch.setattr(fresh, "_check", lambda *a: None)
    monkeypatch.setattr(catalog, "observe", lambda *a: pytest.fail("must not learn unverified a006 catalog"))
    with pytest.raises(ProductAdoptionRefused):
        transition._prepare(db, owner, historical=False)


@pytest.mark.parametrize(
    ("field", "value"), [("procost", 0.01), ("prorows", 12345), ("protrftypes", [999]), ("support_oid", 999)]
)
def test_trigger_function_fixed_create_defaults_are_required(monkeypatch, field, value):
    row = {
        "oid": 123,
        "owner": "app_admin",
        "lanname": "plpgsql",
        "prosecdef": False,
        "provolatile": "v",
        "proparallel": "u",
        "proisstrict": False,
        "proleakproof": False,
        "prokind": "f",
        "proconfig": ["search_path=pg_catalog"],
        "result": "trigger",
        "pronargdefaults": 0,
        "probin": None,
        "prosqlbody": None,
        "prosupport": "-",
        "support_oid": 0,
        "provariadic": 0,
        "prosrc": " BEGIN NEW.last_seen_at := pg_catalog.now(); RETURN NEW; END; ",
        "procost": 100,
        "prorows": 0,
        "protrftypes": None,
    }
    row[field] = value
    monkeypatch.setattr(catalog, "_dicts", lambda *a, **k: (row,))
    db = SimpleNamespace(scalar=lambda *a, **k: 123)
    with pytest.raises(ProductAdoptionRefused):
        catalog._function(db, "a008_lab_console_activity")


@pytest.mark.parametrize(
    "target", ["a004_retained_learning_refs", "a005_erp_completion_checkpoint", "a006_lab_claim_lease"]
)
def test_fresh_successful_callback_learning_column_is_checked_before_ordinal_advances(monkeypatch, target):
    """Keep the real inverse comparison; mock SQL/DDL prerequisites only."""
    from app.maintenance import fresh_product as fresh
    from app.maintenance import product_adoption as product

    before, _ = _packets()
    base = json.loads(before["pg_class"][100])
    base["relname"] = "courses"
    before["pg_class"][100] = json.dumps(base)
    state = {"packet": before, "version": 0}
    owner = SimpleNamespace(prepared=True, next_stage=0, poisoned=False, graph=())
    root = object()
    provenance = (None, None, None, None, ((), ()))
    stage = object.__new__(transition._FreshStages)
    public_schema = object()
    transition._FreshStages.__init__(
        stage, owner, None, root, (), (), provenance, 1, {}, copy.deepcopy(before), public_schema
    )
    db = SimpleNamespace(info={transition._FRESH_KEY: stage})
    stage.connection = db
    monkeypatch.setattr(transition, "_check_fresh_stages", lambda *a: stage)
    monkeypatch.setattr(fresh, "_check", lambda *a: None)
    monkeypatch.setattr(fresh, "_versions", lambda *a: (state["version"],))
    monkeypatch.setattr(fresh, "_cut", lambda graph, name, final: (fresh._STAGES.index(name) + int(final),))
    monkeypatch.setattr(transition, "_config", lambda *a: SimpleNamespace(attributes={"connection": db}))
    monkeypatch.setattr(transition, "_verify_config", lambda *a: None)
    monkeypatch.setattr(transition, "_connection_preflight", lambda *a: root)
    monkeypatch.setattr(transition.tokens, "_physical_context", lambda *a: ())
    monkeypatch.setattr(transition, "_roles", lambda *a: ())
    monkeypatch.setattr(product, "_provenance", lambda: provenance)
    monkeypatch.setattr(transition.checkpoint, "_parent_keys", lambda *a: {})
    monkeypatch.setattr(catalog, "observe", lambda *a: copy.deepcopy(state["packet"]))
    monkeypatch.setattr(catalog, "row_evidence", lambda *a: ())

    def validate(connection, prior, current, name, **kwargs):
        assert kwargs["public_schema"] is public_schema
        catalog.conserve(prior, current, stage=name, appended=set())

    monkeypatch.setattr(catalog, "validate_fresh_stage", validate)

    def upgrade(config, name):
        owner.next_stage += 1
        state["version"] += 1
        if name == target:
            relation = json.loads(state["packet"]["pg_class"][100])
            relation["relnatts"] += 1
            state["packet"]["pg_class"][100] = json.dumps(relation)
            state["packet"]["pg_attribute"][(100, 2)] = json.dumps(
                wire_row("pg_attribute", (100, 2), attname="unreviewed")
            )

    monkeypatch.setattr(transition.command, "upgrade", upgrade)
    with pytest.raises(ProductAdoptionRefused):
        transition._run_fresh_stages(db, owner)
    assert stage.observed == fresh._STAGES.index(target)
    assert owner.poisoned and stage.poisoned


def test_checked_fresh_context_transfers_packet_and_detaches_registry(monkeypatch):
    from app.maintenance import fresh_product as fresh

    owner = SimpleNamespace(prepared=True, next_stage=3, graph=())
    packet = {"catalog": "checked predecessor"}
    stage = SimpleNamespace(observed=3, consumed=False, packet=packet)
    connection = SimpleNamespace(info={transition._FRESH_KEY: stage})
    monkeypatch.setattr(transition, "_check_fresh_stages", lambda *a: stage)
    monkeypatch.setattr(fresh, "_versions", lambda *a: ("a006",))
    monkeypatch.setattr(fresh, "_cut", lambda *a, **k: ("a006",))
    assert transition._consume_fresh_stages(connection, owner) is packet
    assert stage.consumed and stage.packet == {}
    assert transition._FRESH_KEY not in connection.info


def test_real_ordinary_config_cannot_switch_connection_or_locations():
    from app.maintenance import product_adoption as product

    # Genuine callers already ran the ordinary graph to a006, which binds the
    # installed Learning prerequisite edges before capturing provenance.
    transition.ordinary_migration_config()
    db = object()
    entry = SimpleNamespace(provenance=product._provenance())
    cfg = transition._config(db, entry)
    transition._verify_config(db, entry, cfg)
    cfg.attributes["connection"] = object()
    with pytest.raises(ProductAdoptionRefused):
        transition._verify_config(db, entry, cfg)
    cfg.attributes["connection"] = db
    cfg.attributes["academy_migration_locations"] = (
        *cfg.attributes["academy_migration_locations"],
        cfg.attributes["academy_migration_locations"][0],
    )
    with pytest.raises(ProductAdoptionRefused):
        transition._verify_config(db, entry, cfg)


def test_safe_maintenance_diagnostic_follows_suppressed_context_without_values():
    from app.maintenance import product_adoption as product

    marker = "DO_NOT_DISCLOSE_SQL_OR_CREDENTIAL_VALUES"

    def inner():
        raise ValueError(marker)

    def outer():
        try:
            inner()
        except ValueError:
            raise RuntimeError(marker) from None

    # Bind only code-location metadata; no source formatter, exec or local read.
    inner.__code__ = inner.__code__.replace(co_filename=product.__file__)
    outer.__code__ = outer.__code__.replace(co_filename=product.__file__)
    try:
        outer()
    except RuntimeError as error:
        assert error.__suppress_context__
        result = product._maintenance_refusal_locations(error)
    assert marker not in result
    assert "ValueError" not in result and "RuntimeError" not in result
    assert str(product.Path(product.__file__).parent) not in result
    assert result != "unavailable"
    assert len(result.split(" > ")) <= 4
    assert all(
        part.startswith("product_adoption.py:") and part.rsplit(":", 1)[1].isdigit() for part in result.split(" > ")
    )


def test_safe_maintenance_diagnostic_bounds_cyclic_context_and_ignores_other_files():
    from app.maintenance import product_adoption as product

    try:
        raise ValueError("never render this exception")
    except ValueError as error:
        error.__context__ = error
        assert product._maintenance_refusal_locations(error) == "unavailable"


def test_learning_shape_diagnostic_uses_only_genuine_declaration_ordinal():
    from app.maintenance import learning_history as history
    from app.maintenance import product_adoption as product

    marker = "DO_NOT_DISCLOSE_CATALOG_OR_EXCEPTION_VALUES"
    declared = history._TABLES[0]
    with pytest.raises(history.LearningHistoryRefused) as failure:
        # The genuine shape checker refuses before any Connection use. Only
        # its fixed declaration object may select a diagnostic ordinal.
        history._shape(object(), declared, SimpleNamespace(relation=(marker,)), {declared.name: 1}, {})
    wrapped = RuntimeError(marker)
    wrapped.__context__ = failure.value
    wrapped.__suppress_context__ = True
    assert product._learning_shape_refusal_code(wrapped) == "L01"
    assert marker not in product._learning_shape_refusal_code(wrapped)


def test_learning_shape_diagnostic_rejects_caller_supplied_spec_lookalike():
    from app.maintenance import learning_history as history
    from app.maintenance import product_adoption as product

    marker = "DO_NOT_DISCLOSE_CALLER_SUPPLIED_NAME"
    supplied = SimpleNamespace(name=marker)
    with pytest.raises(history.LearningHistoryRefused) as failure:
        history._shape(object(), supplied, SimpleNamespace(relation=(marker,)), {marker: 1}, {})
    assert product._learning_shape_refusal_code(failure.value) is None


def test_learning_shape_diagnostic_ignores_impostor_frame_and_context_cycle():
    from app.maintenance import learning_history as history
    from app.maintenance import product_adoption as product

    def impostor(spec):
        raise ValueError("DO_NOT_DISCLOSE_IMPOSTOR_VALUES")

    impostor.__code__ = impostor.__code__.replace(co_filename=history.__file__)
    with pytest.raises(ValueError) as failure:
        impostor(history._TABLES[0])
    failure.value.__context__ = failure.value
    assert product._learning_shape_refusal_code(failure.value) is None


def _c43_packet(name="applicants", namespace="2200"):
    before, after = _packets()
    before["global"]["namespaces"] = ((2200, "public", 10, None), (2201, "mod_learning", 10, None))
    after["global"] = copy.deepcopy(before["global"])
    for packet in (before, after):
        row = json.loads(packet["pg_class"][100])
        row.update(relname=name, relnamespace=namespace, relpages=0, reltuples=0.0)
        packet["pg_class"][100] = json.dumps(row)
    return before, after


def _c43_refusal(before, after, stage="a009_applicant_bank_version_pin"):
    with pytest.raises(ProductAdoptionRefused) as raised:
        catalog.conserve(before, after, stage=stage, appended={(100, 2)})
    if stage == "a009_applicant_bank_version_pin":
        assert raised.value.__suppress_context__
    return raised.value


@pytest.mark.parametrize(
    "name,namespace,ordinal",
    [
        ("applicants", "2200", 1),
        ("question_bank_versions", "2201", 2),
        ("applicants", "2201", 0),
        ("private_unrecognized_name", "2200", 0),
    ],
)
def test_c43_fixed_ordinals_and_projected_masks_have_no_identifiers_or_values(name, namespace, ordinal, capsys):
    before, after = _c43_packet(name, namespace)
    row = json.loads(after["pg_class"][100])
    row.update(relpages=7, reltuples="PRIVATE_C43_PAYLOAD")
    del row["relname"]
    row["reloptions"] = ["PRIVATE_C43_OPTION"]
    row["PRIVATE_C43_UNKNOWN_KEY"] = {"PRIVATE_C43_TOKEN": ["PRIVATE_C43_ANSWER"]}
    after["pg_class"][100] = json.dumps(row)
    saved = copy.deepcopy((before, after))
    error = _c43_refusal(before, after)
    changed = (1 << 9) | (1 << 10)
    assert str(error) == (
        "Academy ERP successor catalog refused; root rollback required "
        f"[C43 relation={ordinal} changed={changed:09x} missing=000000002 added=080000000 unknown_present=1 unknown_changed=1]"
    )
    import traceback

    assert "PRIVATE" not in "".join(traceback.format_exception(error))
    assert "PRIVATE" not in str(error) and name not in str(error)
    assert (before, after) == saved
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


def test_c43_masks_observe_final_projection_and_type_sensitive_json_comparison():
    before, after = _c43_packet()
    prior = json.loads(before["pg_class"][100])
    prior["relpages"] = False
    before["pg_class"][100] = json.dumps(prior)
    row = json.loads(after["pg_class"][100])
    row.update(relpages=0, reltuples=2.0, relhasindex=True, relhastriggers=True)
    after["pg_class"][100] = json.dumps(row)
    after["pg_index"][700] = json.dumps(wire_row("pg_index", 700, indrelid="100"))
    after["pg_trigger"][701] = json.dumps(wire_row("pg_trigger", 701, tgrelid="100"))
    error = _c43_refusal(before, after)
    assert "changed=000000600" in str(error)
    assert "missing=000000000 added=000000000" in str(error)


@pytest.mark.parametrize("kind", ["unknown_same", "unknown_changed"])
def test_c43_unknown_fields_emit_presence_and_change_only(kind):
    before, after = _c43_packet()
    for packet in (before, after):
        row = json.loads(packet["pg_class"][100])
        row["PRIVATE_UNKNOWN_FIELD"] = {"PRIVATE_TOKEN": [False, "private_answer"]}
        packet["pg_class"][100] = json.dumps(row)
    row = json.loads(after["pg_class"][100])
    row["relpages"] = 1
    if kind == "unknown_changed":
        row["PRIVATE_UNKNOWN_FIELD"]["PRIVATE_TOKEN"][0] = 0
    after["pg_class"][100] = json.dumps(row)
    error = _c43_refusal(before, after)
    assert f"unknown_present=1 unknown_changed={int(kind == 'unknown_changed')}" in str(error)
    assert "PRIVATE" not in str(error) and "private_answer" not in str(error)


@pytest.mark.parametrize(
    "fault",
    [
        "namespace_type",
        "namespace_bool",
        "namespace_owner",
        "namespace_duplicate",
        "arbitrary_object",
        "nonstring_key",
        "huge",
        "nonfinite",
        "unchanged",
    ],
)
def test_c43_malformed_or_unchanged_diagnostic_input_only_gets_generic_refusal(fault):
    before, after = _c43_packet()
    a, b = (json.loads(packet["pg_class"][100]) for packet in (before, after))
    b["relpages"] = 1

    class PrivateObject:
        def __str__(self):
            pytest.fail("arbitrary object must never be rendered")

        __repr__ = __str__

        def __eq__(self, other):
            pytest.fail("arbitrary object must never be compared")

    if fault == "namespace_type":
        before["global"]["namespaces"] = []
    elif fault == "namespace_bool":
        before["global"]["namespaces"] = ((True, "public", 10, None),)
    elif fault == "namespace_owner":
        before["global"]["namespaces"] = ((2200, "public", "secret-owner", None),)
    elif fault == "namespace_duplicate":
        before["global"]["namespaces"] *= 2
    elif fault == "arbitrary_object":
        b["unknown"] = PrivateObject()
    elif fault == "nonstring_key":
        b[42] = "PRIVATE"
    elif fault == "huge":
        b["unknown"] = "PRIVATE" * 10000
    elif fault == "nonfinite":
        b["reltuples"] = float("nan")
    else:
        b = copy.deepcopy(a)
    with pytest.raises(ProductAdoptionRefused) as raised:
        catalog._class_conservation_refused(before, 100, a, b)
    assert str(raised.value) == "Academy ERP successor catalog refused; root rollback required"
    assert raised.value.__suppress_context__


def test_c43_only_marks_a009_and_does_not_change_any_acceptance():
    before, after = _c43_packet()
    catalog.conserve(before, after, stage="a009_applicant_bank_version_pin", appended={(100, 2)})
    row = json.loads(after["pg_class"][100])
    row["relpages"] = 7
    after["pg_class"][100] = json.dumps(row)
    assert "C43" in str(_c43_refusal(before, after))
    assert "C43" not in str(_c43_refusal(before, after, stage="a008_lab_console_activity"))


def test_real_c43_refusal_survives_actual_erp_runner_poison_and_bare_reraise(monkeypatch):
    owner = object()
    entry = object.__new__(transition._Continuation)
    before, after = _c43_packet()
    row = json.loads(after["pg_class"][100])
    row["relpages"] = 7
    after["pg_class"][100] = json.dumps(row)
    packet = {"observed": ()}
    transition._Continuation.__init__(
        entry, None, object(), (), (), (None, None, None, None, ((), ())), 1, owner, True, object(), (), 2, 3, packet
    )
    db = SimpleNamespace(info={transition._KEY: entry})
    entry.connection = db
    poisoned, emitted = [], []
    monkeypatch.setattr(transition, "_verify_config", lambda *a: None)
    monkeypatch.setattr(transition, "_prepare", lambda *a, **k: entry)
    monkeypatch.setattr(transition, "_check", lambda *a, **k: None)
    monkeypatch.setattr(transition, "_config", lambda *a: SimpleNamespace(attributes={"connection": db}))
    monkeypatch.setattr(transition, "_poison", lambda conn, current: poisoned.append((conn, current)))
    monkeypatch.setattr(transition.sa.event, "listen", lambda *a: None)
    monkeypatch.setattr(transition.sa.event, "remove", lambda *a: None)
    monkeypatch.setattr(catalog, "observe", lambda *a: packet)
    monkeypatch.setattr(catalog, "row_evidence", lambda *a: ())

    def callback(*a, **k):
        try:
            catalog.conserve(before, after, stage="a009_applicant_bank_version_pin", appended={(100, 2)})
        except ProductAdoptionRefused as error:
            emitted.append(error)
            raise

    monkeypatch.setattr(transition.command, "upgrade", callback)
    with pytest.raises(ProductAdoptionRefused) as raised:
        transition._run(db, owner, historical=True)
    assert raised.value is emitted[0] and "[C43 relation=1 changed=000000200" in str(raised.value)
    assert poisoned == [(db, entry)]
    assert entry.runner is None and not entry.packet and transition._KEY not in db.info


def _c44_verifier_case(monkeypatch, *, count=3, old_tuples=-1, pages=1):
    from sqlalchemy.engine import Engine

    from app.maintenance import fresh_product as fresh
    from app.maintenance import identity_history as identity

    monkeypatch.setattr(Engine, "connect", lambda *a, **k: pytest.fail("pure verifier test cannot connect"))
    before, after = _c43_packet()
    for packet in (before, after):
        row = json.loads(packet["pg_class"][100])
        row.update(
            relkind="r", relispartition=False, relhassubclass=False, relallvisible=0, relpages=0, reltuples=old_tuples
        )
        packet["pg_class"][100] = json.dumps(row)
    row = json.loads(after["pg_class"][100])
    row.update(relpages=pages, reltuples=count, relhasindex=True)
    after["pg_class"][100] = json.dumps(row)
    after["pg_class"][700] = json.dumps(
        wire_row("pg_class", 700, relname="ix_applicants_assessment_bank_version_id", relnamespace="2200", relkind="i")
    )
    after["pg_index"][700] = json.dumps(wire_row("pg_index", 700, indrelid="100"))
    rows = ((100, (("a" * 32, count),) if count else ()),)
    state = dict(
        root=object(),
        physical=("fixed",),
        authority=True,
        isolation="read committed",
        rls=False,
        lock=True,
        count=count,
        size=pages * 8192,
        block=8192,
        cast=True,
        postrows=rows,
        raw=after["pg_class"][100],
    )
    seen = []

    def scalar(query, parameters=None):
        sql = str(query)
        seen.append((sql, parameters))
        if "transaction_isolation" in sql:
            return state["isolation"]
        if "row_security_active" in sql:
            return state["rls"]
        if "pg_locks" in sql:
            return state["lock"]
        if "count(*) FROM ONLY" in sql:
            return state["count"]
        if "pg_relation_size" in sql:
            return state["size"]
        if "block_size" in sql:
            return state["block"]
        assert "reltuples=CAST(CAST(:count AS double precision) AS real)" in sql
        assert parameters == {"count": count, "oid": 100}
        return state["cast"]

    db = SimpleNamespace(scalar=scalar)
    monkeypatch.setattr(identity, "_connection_preflight", lambda conn: state["root"])
    monkeypatch.setattr(transition.tokens, "_physical_context", lambda conn: state["physical"])

    def authority(conn):
        assert conn is db
        if not state["authority"]:
            raise ProductAdoptionRefused("authority refused")

    monkeypatch.setattr(fresh, "_authority", authority)

    def postrows(conn, packet, *, omissions):
        assert conn is db and packet is after
        assert omissions == (("applicants", ("assessment_bank_version_id",)),)
        return state["postrows"]

    monkeypatch.setattr(catalog, "row_evidence", postrows)
    monkeypatch.setattr(catalog, "_raw", lambda conn, category, predicate, **kw: {100: state["raw"]})
    return db, before, after, rows, state, seen


@pytest.mark.parametrize("count", [1, 3, 2**53])
def test_c44_verified_physical_heap_statistics_only_project_two_local_fields(monkeypatch, count):
    db, before, after, rows, state, seen = _c44_verifier_case(monkeypatch, count=count)
    saved = copy.deepcopy((before, after))
    catalog.conserve(
        before, after, stage="a009_applicant_bank_version_pin", appended={(100, 2)}, connection=db, original_rows=rows
    )
    assert (before, after) == saved
    assert any("double precision) AS real" in query for query, _ in seen)
    current = json.loads(after["pg_class"][100])
    current["relallvisible"] = 1
    after["pg_class"][100] = json.dumps(current)
    state["raw"] = after["pg_class"][100]
    with pytest.raises(ProductAdoptionRefused, match="C43.*changed=000000800"):
        catalog.conserve(
            before,
            after,
            stage="a009_applicant_bank_version_pin",
            appended={(100, 2)},
            connection=db,
            original_rows=rows,
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("authority", False),
        ("isolation", "repeatable read"),
        ("rls", True),
        ("rls", 0),
        ("lock", False),
        ("lock", 1),
        ("count", True),
        ("count", 4),
        ("size", True),
        ("size", -1),
        ("size", 8193),
        ("size", 2147483648 * 8192),
        ("block", False),
        ("block", 0),
        ("block", "8192"),
        ("cast", False),
        ("cast", 1),
        ("postrows", ()),
        ("raw", "different raw packet"),
    ],
)
def test_c44_actual_verifier_refuses_count_physical_or_authority_contradictions(monkeypatch, field, value):
    db, before, after, rows, state, seen = _c44_verifier_case(monkeypatch)
    state[field] = value
    with pytest.raises(ProductAdoptionRefused):
        catalog._verify_a009_heap_statistics(db, before, after, rows)


@pytest.mark.parametrize("value", [True, 0, -1, "3", 3.0, 2**53 + 1])
def test_c44_group_counts_are_native_positive_bounded_integers(monkeypatch, value):
    db, before, after, rows, state, seen = _c44_verifier_case(monkeypatch)
    with pytest.raises(ProductAdoptionRefused):
        catalog._verify_a009_heap_statistics(db, before, after, ((100, (("a" * 32, value),)),))


@pytest.mark.parametrize(
    "attack",
    [
        "missing_rows",
        "duplicate_rows",
        "duplicate_digest",
        "bad_digest",
        "sum_overflow",
        "wrong_index",
        "old_index",
        "wrong_table",
        "inherited",
        "wrong_pages",
        "bool_pages",
        "bool_tuples",
    ],
)
def test_c44_source_and_private_row_identity_remain_exact(monkeypatch, attack):
    db, before, after, rows, state, seen = _c44_verifier_case(monkeypatch)
    if attack == "missing_rows":
        rows = ()
    elif attack == "duplicate_rows":
        rows *= 2
    elif attack == "duplicate_digest":
        rows = ((100, (("a" * 32, 1), ("a" * 32, 2))),)
    elif attack == "bad_digest":
        rows = ((100, (("PRIVATE", 3),)),)
    elif attack == "sum_overflow":
        rows = ((100, (("a" * 32, 2**53), ("b" * 32, 1))),)
    elif attack == "wrong_index":
        index = json.loads(after["pg_index"][700])
        index["indrelid"] = "101"
        after["pg_index"][700] = json.dumps(index)
    elif attack == "old_index":
        before["pg_index"][700] = after["pg_index"][700]
    else:
        packet = before if attack in ("wrong_table", "inherited") else after
        row = json.loads(packet["pg_class"][100])
        row[
            {
                "wrong_table": "relnamespace",
                "inherited": "relhassubclass",
                "wrong_pages": "relpages",
                "bool_pages": "relpages",
                "bool_tuples": "reltuples",
            }[attack]
        ] = {"wrong_table": "2201", "inherited": True, "wrong_pages": 2, "bool_pages": True, "bool_tuples": True}[
            attack
        ]
        packet["pg_class"][100] = json.dumps(row)
        state["raw"] = after["pg_class"][100]
    with pytest.raises(ProductAdoptionRefused):
        catalog._verify_a009_heap_statistics(db, before, after, rows)


@pytest.mark.parametrize("old_tuples", [-1, -1.0, 0, 0.0])
def test_c44_empty_heap_preserves_unknown_statistics_special_case(monkeypatch, old_tuples):
    db, before, after, rows, state, seen = _c44_verifier_case(monkeypatch, count=0, old_tuples=old_tuples, pages=0)
    row = json.loads(after["pg_class"][100])
    row["reltuples"] = old_tuples if old_tuples < 0 else 0
    after["pg_class"][100] = json.dumps(row)
    state["raw"] = after["pg_class"][100]
    assert catalog._verify_a009_heap_statistics(db, before, after, rows) == (None if old_tuples < 0 else 100)
    assert any("double precision) AS real" in query for query, _ in seen) is (old_tuples >= 0)
    if old_tuples < 0:
        row["reltuples"] = 0
        after["pg_class"][100] = json.dumps(row)
        state["raw"] = after["pg_class"][100]
        with pytest.raises(ProductAdoptionRefused):
            catalog._verify_a009_heap_statistics(db, before, after, rows)


@pytest.mark.parametrize("field", ["root", "physical"])
def test_c44_final_read_cannot_replace_root_or_physical_transaction(monkeypatch, field):
    db, before, after, rows, state, seen = _c44_verifier_case(monkeypatch)
    scalar = db.scalar

    def final_read(query, parameters=None):
        value = scalar(query, parameters)
        if "double precision) AS real" in str(query):
            state[field] = object() if field == "root" else ("changed",)
        return value

    db.scalar = final_read
    with pytest.raises(ProductAdoptionRefused):
        catalog._verify_a009_heap_statistics(db, before, after, rows)


def test_c44_empty_unknown_stats_preserve_old_values_even_with_dead_physical_pages(monkeypatch):
    db, before, after, rows, state, seen = _c44_verifier_case(monkeypatch, count=0, old_tuples=-1, pages=1)
    row = json.loads(after["pg_class"][100])
    row.update(relpages=0, reltuples=-1)
    after["pg_class"][100] = json.dumps(row)
    state["raw"] = after["pg_class"][100]
    assert catalog._verify_a009_heap_statistics(db, before, after, rows) is None
    assert state["size"] == 8192
    assert not any("double precision) AS real" in query for query, _ in seen)


def test_c44_other_stages_never_invoke_stat_projection(monkeypatch):
    before, after = _packets()
    monkeypatch.setattr(catalog, "_verify_a009_heap_statistics", lambda *a: pytest.fail("a009 only"))
    catalog.conserve(
        before, after, stage="a007_lab_template_access", appended={(100, 2)}, connection=object(), original_rows=()
    )


def _c45_expression_identities(monkeypatch):
    queries = []

    def source_identities(connection, query, **params):
        queries.append(query)
        if "pg_catalog.pg_proc" in query:
            assert "'pg_catalog.varchar(character varying,integer,boolean)'::regprocedure" in query
            return ((1001,), (1002,), (1003,))
        if "oprcode::oid" in query:
            return ((2001,),)
        if "pg_catalog.pg_operator" in query:
            return ((2002,),)
        assert "pg_catalog.pg_type" in query
        return ((1043,), (23,), (16,))

    monkeypatch.setattr(catalog, "_rows", source_identities)
    return queries


@pytest.mark.parametrize("function", [1001, 1002, 1003])
def test_c45_fixed_existing_and_varchar_length_coercion_identities_are_allowed(monkeypatch, function):
    import re

    queries = _c45_expression_identities(monkeypatch)
    # Symbolic source IDs stand for dynamically resolved builtins; no DB claim.
    raw = f"{{FUNCEXPR :funcid {function} :args ({{CONST :consttype 1043}} {{CONST :consttype 23}} {{CONST :consttype 16}})}}"
    catalog._expression(object(), "'pending'::character varying", raw)
    assert re.findall(r"'([^']+)'::regprocedure", queries[0]) == [
        "pg_catalog.now()",
        "public.app_current_tenant_id()",
        "pg_catalog.varchar(character varying,integer,boolean)",
    ]
    assert "669" not in queries[0]


@pytest.mark.parametrize(
    "raw",
    [
        "{FUNCEXPR :funcid 1004}",
        "{FUNCEXPR :funcid 669}",
        "{FUNCEXPR :funcid 1003 :args ({FUNCEXPR :funcid 1004})}",
        "{FUNCEXPR :funcid 1003 :consttype 9999}",
        "{FUNCEXPR :funcid 1003 :opno 9999}",
        "{FUNCEXPR :funcid 1003 :opfuncid 9999}",
        "{FUNCEXPR :funcid 1003 :vartype 9999}",
        "{FUNCEXPR :funcid 1003 :args ({COERCEVIAIO})}",
    ],
)
def test_c45_new_coercion_does_not_admit_unknown_functions_types_operators_or_nodes(monkeypatch, raw):
    _c45_expression_identities(monkeypatch)
    with pytest.raises(ProductAdoptionRefused):
        catalog._expression(object(), "'pending'::character varying", raw)


@pytest.mark.parametrize(
    "default", ["'pending'::character varying", "'active'::character varying", "private_status_function()"]
)
def test_c45_default_column_still_requires_exact_source_deparser_and_type(monkeypatch, default):
    _c45_expression_identities(monkeypatch)
    row = (
        1,
        "status",
        "character varying(20)",
        False,
        default,
        "",
        "",
        None,
        False,
        False,
        None,
        "{FUNCEXPR :funcid 1003 :args ({CONST :consttype 1043} {CONST :consttype 23} {CONST :consttype 16})}",
        123,
    )
    db = SimpleNamespace(scalar=lambda *a, **k: 123)
    expected = ("status", "character varying(20)", False, "'pending'::character varying")
    if default == expected[3]:
        catalog._validate_column(db, row, expected)
    else:
        with pytest.raises(ProductAdoptionRefused):
            catalog._validate_column(db, row, expected)


@pytest.mark.parametrize(
    "kind,name,storage,accepted",
    [
        ("i", "varchar", "x", True),
        ("i", "varchar", "p", False),
        ("t", "text", "p", True),
        ("t", "text", "x", False),
        ("r", "varchar", "x", True),
        ("r", "varchar", "p", False),
        ("i", "uuid", "p", True),
        ("i", "uuid", "x", False),
    ],
)
def test_c50_real_attribute_contract_preserves_validated_type_storage_except_toast(
    monkeypatch, kind, name, storage, accepted
):
    from sqlalchemy.engine import Engine

    monkeypatch.setattr(Engine, "connect", lambda *a, **k: pytest.fail("pure attribute test cannot connect"))
    type_oid, length, byval, align, type_storage = {
        "varchar": (1043, -1, False, "i", "x"),
        "text": (25, -1, False, "i", "x"),
        "uuid": (2950, 16, False, "c", "p"),
    }[name]
    attribute = wire_row(
        "pg_attribute",
        (700, 1),
        atttypid=str(type_oid),
        attname="status" if name == "varchar" else "value",
        attidentity="",
        attgenerated="",
        attcompression="",
        attoptions=None,
        attfdwoptions=None,
        attinhcount=0,
        attislocal=True,
        attndims=0,
        attisdropped=False,
        attstattarget=-1,
        attcacheoff=-1,
        attlen=length,
        attbyval=byval,
        attalign=align,
        attstorage=storage,
    )
    before = {"pg_attribute": {}}
    after = {
        "pg_attribute": {(700, 1): json.dumps(attribute)},
        "pg_class": {700: json.dumps(wire_row("pg_class", 700, relkind=kind))},
    }
    saved = copy.deepcopy((before, after))
    observed = []

    def types(connection, query, **params):
        assert "t.typstorage" in query and "WHERE t.oid=:oid" in query
        assert params == {"oid": type_oid}
        observed.append(True)
        return (("pg_catalog", name, length, byval, align, type_storage),)

    monkeypatch.setattr(catalog, "_rows", types)
    if accepted:
        catalog._raw_attribute_contract(object(), before, after)
    else:
        with pytest.raises(ProductAdoptionRefused):
            catalog._raw_attribute_contract(object(), before, after)
    assert observed == [True] and (before, after) == saved
