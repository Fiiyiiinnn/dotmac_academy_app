"""PG16 wire-shape validation views; no DB, qualification or owner effects."""

import copy
import json

import pytest
from sqlalchemy.engine import Engine

from app.maintenance import erp_successor_catalog as catalog
from app.maintenance.product_adoption import ProductAdoptionRefused

# Explicit PG16 catalog fixtures, independent of the decoder's field tables.
WIRE_ROWS = {
    "pg_class": dict.fromkeys(
        (
            "oid",
            "relnamespace",
            "reltype",
            "reloftype",
            "relowner",
            "relam",
            "relfilenode",
            "reltablespace",
            "reltoastrelid",
            "relrewrite",
        ),
        "0",
    ),
    "pg_type": dict.fromkeys(
        ("oid", "typnamespace", "typowner", "typrelid", "typelem", "typarray", "typbasetype", "typcollation"), "0"
    ),
    "pg_constraint": {
        **dict.fromkeys(("oid", "connamespace", "conrelid", "contypid", "conindid", "conparentid", "confrelid"), "0"),
        **dict.fromkeys(("conpfeqop", "conppeqop", "conffeqop", "conexclop", "conkey", "confkey", "confdelsetcols")),
    },
    "pg_attrdef": {"oid": "0", "adrelid": "0", "adnum": 1},
    "pg_policy": {"oid": "0", "polrelid": "0", "polroles": ["0"]},
    "pg_trigger": {
        **dict.fromkeys(
            ("oid", "tgrelid", "tgparentid", "tgfoid", "tgconstrrelid", "tgconstrindid", "tgconstraint"), "0"
        ),
        "tgattr": [],
    },
    "pg_attribute": {"attrelid": "0", "atttypid": "0", "attcollation": "0", "attnum": 1},
    "pg_index": {"indexrelid": "0", "indrelid": "0", "indcollation": [], "indclass": [], "indkey": [], "indoption": []},
    "pg_proc": {
        **dict.fromkeys(("oid", "pronamespace", "proowner", "prolang", "provariadic", "prorettype"), "0"),
        "proallargtypes": None,
        "protrftypes": None,
        "proargtypes": [],
        "prosupport": "-",
    },
    "pg_rewrite": {"oid": "0", "ev_class": "0"},
}


def wire_row(category, key, **values):
    row = copy.deepcopy(WIRE_ROWS[category])
    if category == "pg_attribute":
        row.update(attrelid=str(key[0]), attnum=key[1])
    else:
        row["indexrelid" if category == "pg_index" else "oid"] = str(key)
    row.update(values)
    return row


@pytest.fixture(autouse=True)
def no_connections(monkeypatch):
    monkeypatch.setattr(Engine, "connect", lambda *a, **k: pytest.fail("pure codec cannot connect"))


@pytest.mark.parametrize("category", WIRE_ROWS)
def test_complete_pg16_wire_rows_bind_identity_without_mutating_raw_or_unknown_fields(category):
    key = (123, 1) if category == "pg_attribute" else 123
    row = wire_row(
        category, key, unknown={"oid": "001", "nested": [True, None, "123"]}, acl="{private}", expression="'0'::text"
    )
    raw = json.dumps(row)
    view = catalog._catalog_view(category, key, raw)
    assert raw == json.dumps(row)
    assert view["unknown"] == row["unknown"] and view["unknown"] is not row["unknown"]
    assert view["acl"] == "{private}" and view["expression"] == "'0'::text"
    field = "attrelid" if category == "pg_attribute" else "indexrelid" if category == "pg_index" else "oid"
    assert type(view[field]) is int and view[field] == 123
    view["unknown"]["nested"].append("changed")
    assert json.loads(raw) == row


@pytest.mark.parametrize(
    "value",
    [0, 123, True, None, "", "01", "+1", "-1", " 1", "1 ", "1.0", "1e2", "\uff11\uff12", "4294967296", "9" * 100],
)
def test_plain_oid_is_canonical_unsigned_decimal_json_string(value):
    row = wire_row("pg_attrdef", 123, adrelid=value)
    with pytest.raises(ProductAdoptionRefused):
        catalog._catalog_view("pg_attrdef", 123, json.dumps(row))


@pytest.mark.parametrize("value", ["0", "1", "4294967295"])
def test_oid_boundaries_accept_without_general_string_coercion(value):
    view = catalog._catalog_view("pg_attrdef", 123, json.dumps(wire_row("pg_attrdef", 123, adrelid=value)))
    assert view["adrelid"] == int(value)


@pytest.mark.parametrize(
    "category,key",
    [
        ("pg_class", 124),
        ("pg_index", 124),
        ("pg_attribute", (124, 1)),
        ("pg_attribute", (123, 2)),
        ("pg_class", True),
        ("pg_attribute", (123, True)),
    ],
)
def test_catalog_identity_must_match_exact_native_packet_key(category, key):
    row = wire_row(category, (123, 1) if category == "pg_attribute" else 123)
    with pytest.raises(ProductAdoptionRefused):
        catalog._catalog_view(category, key, json.dumps(row))


@pytest.mark.parametrize("raw", ["[]", "null", "true", "{}", "{malformed"])
def test_object_and_complete_declared_fields_are_required(raw):
    with pytest.raises(ProductAdoptionRefused):
        catalog._catalog_view("pg_class", 123, raw)


@pytest.mark.parametrize(
    "value,expected",
    [(None, None), ([], []), (["2", None, "1"], [2, None, 1]), ([["2", None], ["1", "0"]], [[2, None], [1, 0]])],
)
def test_declared_sql_oid_arrays_preserve_null_order_and_dimensions(value, expected):
    row = wire_row("pg_constraint", 123, conpfeqop=value)
    assert catalog._catalog_view("pg_constraint", 123, json.dumps(row))["conpfeqop"] == expected


@pytest.mark.parametrize("value", [["1", ["2"]], [["1"], ["2", "3"]], [["1"], None], [1], [True], "1"])
def test_invalid_sql_array_shape_or_leaf_refuses(value):
    row = wire_row("pg_constraint", 123, conpfeqop=value)
    with pytest.raises(ProductAdoptionRefused):
        catalog._catalog_view("pg_constraint", 123, json.dumps(row))


@pytest.mark.parametrize("value", [None, [None], [["1"]], [1], [True]])
def test_oidvectors_are_flat_nonnull_string_arrays(value):
    with pytest.raises(ProductAdoptionRefused):
        catalog._catalog_view("pg_proc", 123, json.dumps(wire_row("pg_proc", 123, proargtypes=value)))


def test_int2_arrays_and_vectors_have_numeric_int16_leaves_and_unchanged_regproc():
    row = wire_row("pg_constraint", 123, conkey=[[-32768, None], [1, 32767]])
    assert catalog._catalog_view("pg_constraint", 123, json.dumps(row))["conkey"] == row["conkey"]
    row = wire_row("pg_index", 123, indkey=[1, 0, -1], indoption=[0, 3])
    assert catalog._catalog_view("pg_index", 123, json.dumps(row))["indkey"] == [1, 0, -1]
    assert catalog._catalog_view("pg_proc", 123, json.dumps(wire_row("pg_proc", 123)))["prosupport"] == "-"


@pytest.mark.parametrize("value", [[True], ["1"], [32768], [-32769], [None], [[1]], None])
def test_int2vectors_reject_wrong_types_bounds_nesting_and_null(value):
    with pytest.raises(ProductAdoptionRefused):
        catalog._catalog_view("pg_index", 123, json.dumps(wire_row("pg_index", 123, indkey=value)))


def test_required_sql_array_preserves_null_members_but_not_null_array():
    row = wire_row("pg_policy", 123, polroles=[["2", None], ["0", "1"]])
    assert catalog._catalog_view("pg_policy", 123, json.dumps(row))["polroles"] == [[2, None], [0, 1]]
    row["polroles"] = None
    with pytest.raises(ProductAdoptionRefused):
        catalog._catalog_view("pg_policy", 123, json.dumps(row))


def test_oidvectors_preserve_order_and_decode_only_their_declared_elements():
    row = wire_row("pg_index", 123, indclass=["2", "1", "0"], indcollation=["0", "4294967295"])
    view = catalog._catalog_view("pg_index", 123, json.dumps(row))
    assert view["indclass"] == [2, 1, 0] and view["indcollation"] == [0, 4294967295]


@pytest.mark.parametrize("field", ["conkey", "conpfeqop", "conexclop", "unknown", "expression", "conacl"])
def test_raw_conservation_rejects_array_unknown_expression_and_acl_drift(field):
    from legacy_probe.test_erp_progress_transition import _packets

    before, after = _packets()
    original = wire_row(
        "pg_constraint",
        500,
        conkey=[1, 2],
        conpfeqop=["3", "4"],
        conexclop=["5"],
        unknown={"oid": "1"},
        expression="(a > 0)",
        conacl=None,
    )
    before["pg_constraint"][500] = json.dumps(original)
    after["pg_constraint"][500] = json.dumps(original)
    saved = copy.deepcopy((before, after))
    catalog.conserve(before, after, stage="a007_lab_template_access", appended={(100, 2)})
    assert (before, after) == saved
    changed = copy.deepcopy(original)
    changed[field] = {
        "conkey": [2, 1],
        "conpfeqop": ["4", "3"],
        "conexclop": None,
        "unknown": {"oid": "01"},
        "expression": "(a > 1)",
        "conacl": ["planted"],
    }[field]
    after["pg_constraint"][500] = json.dumps(changed)
    snapshot = copy.deepcopy((before, after))
    with pytest.raises(ProductAdoptionRefused):
        catalog.conserve(before, after, stage="a007_lab_template_access", appended={(100, 2)})
    assert (before, after) == snapshot


@pytest.mark.parametrize(
    "category,relation_field,flag",
    [("pg_trigger", "tgrelid", "relhastriggers"), ("pg_index", "indrelid", "relhasindex")],
)
def test_decoded_affected_ids_allow_only_original_narrow_class_flag_projection(category, relation_field, flag):
    from legacy_probe.test_erp_progress_transition import _packets

    before, after = _packets()
    after[category][700] = json.dumps(wire_row(category, 700, **{relation_field: "100"}))
    current = json.loads(after["pg_class"][100])
    current[flag] = True
    after["pg_class"][100] = json.dumps(current)
    snapshot = copy.deepcopy((before, after))
    catalog.conserve(before, after, stage="a007_lab_template_access", appended={(100, 2)})
    assert (before, after) == snapshot
    current["relowner"] = "999"
    after["pg_class"][100] = json.dumps(current)
    with pytest.raises(ProductAdoptionRefused):
        catalog.conserve(before, after, stage="a007_lab_template_access", appended={(100, 2)})


def test_row_evidence_uses_decoded_namespace_id_and_retains_packet(monkeypatch):
    from types import SimpleNamespace

    row = wire_row("pg_class", 123, relnamespace="2200", relkind="r", relname="lab_templates")
    packet = {"pg_class": {123: json.dumps(row)}}
    saved = copy.deepcopy(packet)
    seen = []

    def namespace(query, params):
        seen.append(params["oid"])
        assert type(params["oid"]) is int
        return "public"

    monkeypatch.setattr(catalog, "_rows", lambda *a, **k: ())
    assert catalog.row_evidence(SimpleNamespace(scalar=namespace), packet) == ((123, ()),)
    assert seen == [2200] and packet == saved


def test_linked_plain_oid_wire_views_cover_toast_types_policy_and_default_identity():
    packets = {
        "pg_class": {
            100: wire_row("pg_class", 100, relname="platform_settings", reltype="300", reltoastrelid="200"),
            200: wire_row("pg_class", 200, relname="pg_toast_100", relkind="t"),
            201: wire_row("pg_class", 201, relname="pg_toast_100_index", relkind="i"),
        },
        "pg_type": {
            300: wire_row(
                "pg_type", 300, typname="platform_settings", typrelid="100", typarray="301", typinput="record_in"
            ),
            301: wire_row("pg_type", 301, typname="_platform_settings", typelem="300", typinput="array_in"),
        },
        "pg_index": {
            201: wire_row(
                "pg_index",
                201,
                indrelid="200",
                indclass=["3127", "3124"],
                indcollation=["0", "0"],
                indkey=[1, 2],
                indoption=[0, 0],
            )
        },
        "pg_attrdef": {400: wire_row("pg_attrdef", 400, adrelid="100", adnum=2, adbin="raw expression")},
        "pg_policy": {500: wire_row("pg_policy", 500, polrelid="100", polroles=["0"], polname="fixed_policy")},
        "pg_trigger": {600: wire_row("pg_trigger", 600, tgrelid="100", tgconstraint="700", tgfoid="1644", tgattr=[])},
        "pg_attribute": {
            (100, 2): wire_row(
                "pg_attribute", (100, 2), atttypid="3802", attname="value", attmissingval=[{"oid": "001"}]
            )
        },
    }
    raw = {category: {key: json.dumps(row) for key, row in rows.items()} for category, rows in packets.items()}
    snapshot = copy.deepcopy(raw)
    decoded = {
        category: {key: catalog._catalog_view(category, key, value) for key, value in rows.items()}
        for category, rows in raw.items()
    }
    assert decoded["pg_attrdef"][400]["adrelid"] == 100
    assert decoded["pg_class"][100]["reltoastrelid"] == 200
    assert decoded["pg_index"][201]["indrelid"] == 200
    assert decoded["pg_type"][301]["typelem"] == 300
    assert decoded["pg_policy"][500]["polroles"] == [0]
    assert decoded["pg_trigger"][600]["tgconstraint"] == 700
    assert decoded["pg_attribute"][(100, 2)]["attmissingval"] == [{"oid": "001"}]
    assert raw == snapshot


@pytest.mark.parametrize("value", [None, True, False, "1", "-1", 32768, -32769, 1.0])
def test_attrdef_attribute_number_requires_native_int16_without_coercion(value):
    row = wire_row("pg_attrdef", 123, adnum=value)
    raw = json.dumps(row)
    with pytest.raises(ProductAdoptionRefused):
        catalog._catalog_view("pg_attrdef", 123, raw)
    assert json.loads(raw) == row


def test_attrdef_attribute_number_is_required_and_valid_wire_value_stays_native():
    row = wire_row("pg_attrdef", 123, adrelid="100", adnum=2)
    raw = json.dumps(row)
    view = catalog._catalog_view("pg_attrdef", 123, raw)
    assert type(view["adnum"]) is int and view["adnum"] == 2
    assert json.loads(raw) == row
    del row["adnum"]
    with pytest.raises(ProductAdoptionRefused):
        catalog._catalog_view("pg_attrdef", 123, json.dumps(row))
