"""Optional ``access:`` block of lab.yaml: parsing, validation, persistence."""

from __future__ import annotations

import pytest

from app.models.course import Course
from app.models.lab import LabTemplate
from app.services.lab_content import import_labs, parse_access, parse_lab

TOPO = """\
name: t
topology:
  nodes:
    router:
      kind: vr-ros
    client-a:
      kind: linux
      exec:
        - ip addr add 10.{{octet}}.0.1/24 dev eth1
"""

GOOD = [
    {"node": "router", "protocol": "webfig", "username": "admin", "default_password": "admin",
     "note": "Default login of the practice router."},
    {"node": "client-a", "protocol": "console", "note": "No login needed."},
]


def test_valid_access_is_normalised():
    out = parse_access(GOOD, topology_text=TOPO)
    assert out == GOOD
    assert parse_access(None, topology_text=TOPO) == []


@pytest.mark.parametrize(
    "bad",
    [
        "router",                                                          # not a list
        ["router"],                                                        # entry not a mapping
        [{"node": "router", "protocol": "webfig", "token": "x"}],          # unknown key
        [{"node": "ghost", "protocol": "console"}],                        # not in topology
        [{"node": "router", "protocol": "telnet"}],                        # protocol
        [{"node": "../router", "protocol": "console"}],                    # node syntax
        [{"node": "router", "protocol": "ssh", "default_password": "x"}],  # password w/o user
        [{"node": "router", "protocol": "ssh", "username": "a", "default_password": ""}],
        [{"node": "router", "protocol": "ssh", "username": "a", "default_password": "x" * 65}],
        [{"node": "router", "protocol": "ssh", "username": "<b>a</b>"}],   # markup
        [{"node": "router", "protocol": "ssh", "username": "a", "note": "{{secret}}"}],
        [{"node": "router", "protocol": "ssh", "username": "a", "default_password": "$ENV"}],
        [{"node": "router", "protocol": "ssh", "username": "a", "note": "x\ny"}],
        [{"node": "router", "protocol": "ssh"}, {"node": "router", "protocol": "ssh"}],
        [{"node": "router", "protocol": "console"}] * 33,                  # too many
    ],
)
def test_invalid_access_is_refused(bad):
    with pytest.raises(ValueError):
        parse_access(bad, topology_text=TOPO)


def _lab_dir(tmp_path, access_yaml: str):
    lab = tmp_path / "labs" / "01-x"
    lab.mkdir(parents=True)
    (lab / "lab.yaml").write_text(
        "slug: x\ntitle: X\nchapter_ref: 1\n" + access_yaml, encoding="utf-8"
    )
    (lab / "topo.clab.yml").write_text(TOPO, encoding="utf-8")
    (lab / "instructions.md").write_text("# X\n", encoding="utf-8")
    return tmp_path / "labs"


def test_parse_lab_reads_access_and_absent_means_empty(tmp_path):
    with_access = _lab_dir(
        tmp_path / "a",
        "access:\n  - {node: router, protocol: webfig, username: admin, default_password: admin}\n",
    )
    spec = parse_lab(with_access / "01-x")
    assert spec.access == [
        {"node": "router", "protocol": "webfig", "username": "admin", "default_password": "admin"}
    ]
    without = _lab_dir(tmp_path / "b", "")
    assert parse_lab(without / "01-x").access == []


def test_parse_lab_rejects_bad_access(tmp_path):
    labs = _lab_dir(tmp_path, "access:\n  - {node: nope, protocol: console}\n")
    with pytest.raises(ValueError, match="not in the topology"):
        parse_lab(labs / "01-x")


def _course(db, tenant_id):
    c = Course(tenant_id=tenant_id, slug="foundation", title="F", discipline="networking",
               source_ref="x", version=1, status="published", listed=False)
    db.add(c)
    db.flush()
    return c


def test_import_persists_access_and_bumps_version_only_on_change(admin_session, tenant_a, tmp_path):
    c = _course(admin_session, tenant_a.id)
    labs = _lab_dir(tmp_path, "")
    t = import_labs(admin_session, tenant_id=tenant_a.id, course_id=c.id, labs_dir=labs)[0]
    admin_session.flush()
    assert t.access == [] and t.version == 1
    h_without = t.source_hash

    labs = _lab_dir(
        tmp_path / "n",
        "access:\n  - {node: client-a, protocol: console, note: No login needed.}\n",
    )
    t = import_labs(admin_session, tenant_id=tenant_a.id, course_id=c.id, labs_dir=labs)[0]
    admin_session.flush()
    assert t.access == [{"node": "client-a", "protocol": "console", "note": "No login needed."}]
    assert t.version == 2 and t.source_hash != h_without

    again = import_labs(admin_session, tenant_id=tenant_a.id, course_id=c.id, labs_dir=labs)[0]
    assert again.version == 2
    assert admin_session.query(LabTemplate).filter(LabTemplate.course_id == c.id).count() == 1
    admin_session.rollback()


def test_hash_of_lab_without_access_is_unchanged_by_this_feature():
    """Existing labs must not be version-bumped just because the column exists."""
    from app.services.lab_content import LabSpec, _source_hash

    spec = LabSpec(slug="s", title="t", chapter_number=1, topology_text="x", instructions_md="m")
    import hashlib
    import json

    legacy = hashlib.sha256(json.dumps(
        {"slug": "s", "title": "t", "chapter_number": 1, "topology": "x", "instructions_html": "h",
         "instructions_md": "m", "checks": [], "seed_spec": {}, "limits": {}},
        sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
    assert _source_hash(spec, "h") == legacy


def _pw(value):
    return [{"node": "router", "protocol": "ssh", "username": "admin", "default_password": value}]


@pytest.mark.parametrize("ok", ["admin", "VR-netlab9", "labpw-1", "password", "changeme123", "a" * 32])
def test_documented_lab_defaults_are_accepted(ok):
    assert parse_access(_pw(ok), topology_text=TOPO)[0]["default_password"] == ok


@pytest.mark.parametrize(
    "bad",
    [
        "ghp_" + "a1B2" * 4,
        "github_pat_11ABCDEFG",
        "sk-abc123",
        "AKIAIOSFODNN7EXAMPLE",
        "eyJhbGciOiJIUzI1NiJ9",
        "xoxb-1234-abcd",
        "-----BEGIN KEY",
        "x" * 33,                      # over the 32 character cap
        "Zk9qL2vXp4RtYw8N",            # 16 distinct characters: entropy 4.0
        "q7Gd2Lm9Vx4Tz8Ws1Bc5",        # random-looking 20 characters
    ],
)
def test_secret_shaped_default_passwords_are_refused(bad):
    with pytest.raises(ValueError):
        parse_access(_pw(bad), topology_text=TOPO)


def test_unparsable_topology_is_an_error_not_a_silent_skip():
    with pytest.raises(ValueError, match="could not be parsed"):
        parse_access(GOOD, topology_text="topology: [unclosed")


def test_no_access_with_unparsable_topology_is_fine():
    assert parse_access(None, topology_text="topology: [unclosed") == []


def test_reimport_refreshes_access_without_a_version_bump(admin_session, tenant_a, tmp_path):
    """Access wiped out of band (e.g. 0062 down/up) is restored by the next import."""
    c = _course(admin_session, tenant_a.id)
    labs = _lab_dir(
        tmp_path, "access:\n  - {node: client-a, protocol: console, note: No login needed.}\n"
    )
    t = import_labs(admin_session, tenant_id=tenant_a.id, course_id=c.id, labs_dir=labs)[0]
    admin_session.flush()
    v = t.version
    t.access = []  # what a down/up cycle leaves behind
    admin_session.flush()
    t = import_labs(admin_session, tenant_id=tenant_a.id, course_id=c.id, labs_dir=labs)[0]
    assert t.access == [{"node": "client-a", "protocol": "console", "note": "No login needed."}]
    assert t.version == v
    admin_session.rollback()
