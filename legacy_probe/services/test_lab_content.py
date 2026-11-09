"""TDD tests for the labs-as-code loader (Task 5)."""

from pathlib import Path

from app.models.assessment import Activity
from app.models.course import Course
from app.models.lab import LabInstance, LabTemplate
from app.services.lab_content import import_labs, parse_lab, render_instance_instructions

LAB_YAML = """\
slug: vlan-segmentation
title: VLAN Segmentation
chapter_ref: 14
topology: topo.clab.yml
instructions: instructions.md
seed_spec:
  lan_octet:
    type: int
    min: 2
    max: 9
limits:
  time_minutes: 45
  ready_wait_seconds: 120
checks:
  - id: c1
    type: probe
    node: client
    weight: 1
    probe: ping
  - id: c2
    type: config_grep
    node: router
    weight: 1
    file: /etc/config
    contains: vlan
"""

TOPO = """\
name: vlan
topology:
  nodes:
    router:
      kind: vr-ros
    client:
      kind: linux
"""

INSTRUCTIONS = """\
# VLAN Segmentation

Configure the LAN.

$include: chapter-14.md#vlan-basics

Good luck.
"""

CHAPTER = """\
# Chapter 14

## Learning objectives

stuff

## VLAN basics

A VLAN is a virtual LAN.

It segments broadcast domains.

## Next section

other content
"""


def _make_lab_dir(tmp_path: Path) -> Path:
    lab = tmp_path / "labs" / "03-vlan-segmentation"
    lab.mkdir(parents=True)
    (lab / "lab.yaml").write_text(LAB_YAML, encoding="utf-8")
    (lab / "topo.clab.yml").write_text(TOPO, encoding="utf-8")
    (lab / "instructions.md").write_text(INSTRUCTIONS, encoding="utf-8")
    chapters = tmp_path / "chapters"
    chapters.mkdir()
    (chapters / "chapter-14.md").write_text(CHAPTER, encoding="utf-8")
    return tmp_path


def test_parse_lab(tmp_path):
    root = _make_lab_dir(tmp_path)
    spec = parse_lab(root / "labs" / "03-vlan-segmentation", chapters_dir=root / "chapters")
    assert spec.slug == "vlan-segmentation"
    assert spec.title == "VLAN Segmentation"
    assert spec.chapter_number == 14
    assert spec.checks[0]["type"] == "probe"
    assert "kind: vr-ros" in spec.topology_text
    assert "Configure the LAN." in spec.instructions_md
    assert spec.seed_spec["lan_octet"]["max"] == 9
    assert spec.limits["time_minutes"] == 45


def test_parse_lab_resolves_include(tmp_path):
    root = _make_lab_dir(tmp_path)
    spec = parse_lab(root / "labs" / "03-vlan-segmentation", chapters_dir=root / "chapters")
    # The anchored chapter slice is spliced in, not the whole chapter.
    assert "A VLAN is a virtual LAN." in spec.instructions_md
    assert "Learning objectives" not in spec.instructions_md
    assert "other content" not in spec.instructions_md
    # The $include directive line itself is gone.
    assert "$include" not in spec.instructions_md


def test_parse_lab_missing_include_does_not_crash(tmp_path):
    root = _make_lab_dir(tmp_path)
    (root / "labs" / "03-vlan-segmentation" / "instructions.md").write_text(
        "intro\n$include: nope.md#whatever\nend\n", encoding="utf-8"
    )
    spec = parse_lab(root / "labs" / "03-vlan-segmentation", chapters_dir=root / "chapters")
    assert "intro" in spec.instructions_md and "end" in spec.instructions_md


def _course(db, tenant_id):
    c = Course(tenant_id=tenant_id, slug="foundation", title="F", discipline="networking", source_ref="x", version=1, status="published", listed=False)
    db.add(c)
    db.flush()
    return c


def test_import_labs_creates_activity_and_template(admin_session, tenant_a, tmp_path):
    root = _make_lab_dir(tmp_path)
    c = _course(admin_session, tenant_a.id)
    templates = import_labs(
        admin_session,
        tenant_id=tenant_a.id,
        course_id=c.id,
        labs_dir=root / "labs",
        chapters_dir=root / "chapters",
    )
    admin_session.flush()
    assert len(templates) == 1
    lt = templates[0]
    assert lt.slug == "vlan-segmentation"

    activities = admin_session.query(Activity).filter(Activity.course_id == c.id, Activity.type == "lab").all()
    assert len(activities) == 1
    assert activities[0].id == lt.activity_id
    assert activities[0].bank_id is None
    assert lt.instructions_html.startswith("<")
    admin_session.rollback()


def test_import_labs_is_idempotent(admin_session, tenant_a, tmp_path):
    root = _make_lab_dir(tmp_path)
    c = _course(admin_session, tenant_a.id)
    t1 = import_labs(
        admin_session, tenant_id=tenant_a.id, course_id=c.id, labs_dir=root / "labs", chapters_dir=root / "chapters"
    )
    admin_session.flush()
    v1 = t1[0].version
    t2 = import_labs(
        admin_session, tenant_id=tenant_a.id, course_id=c.id, labs_dir=root / "labs", chapters_dir=root / "chapters"
    )
    admin_session.flush()
    assert t1[0].id == t2[0].id
    assert t2[0].version == v1
    assert admin_session.query(LabTemplate).filter(LabTemplate.course_id == c.id).count() == 1
    assert admin_session.query(Activity).filter(Activity.course_id == c.id, Activity.type == "lab").count() == 1
    admin_session.rollback()


_SEEDED_INSTRUCTIONS = """\
# VLAN Segmentation

Configure the client at 10.0.{{lan_octet}}.10.

<script>alert('xss')</script>
"""


def _make_seeded_lab_dir(tmp_path: Path) -> Path:
    """A lab dir whose instructions.md carries a `{{lan_octet}}` placeholder
    and an unsafe `<script>` tag, for interpolation + sanitization tests."""
    lab = tmp_path / "labs" / "03-vlan-segmentation"
    lab.mkdir(parents=True)
    (lab / "lab.yaml").write_text(LAB_YAML, encoding="utf-8")
    (lab / "topo.clab.yml").write_text(TOPO, encoding="utf-8")
    (lab / "instructions.md").write_text(_SEEDED_INSTRUCTIONS, encoding="utf-8")
    return tmp_path


def test_import_labs_stores_resolved_raw_markdown_with_seed_placeholder(admin_session, tenant_a, tmp_path):
    root = _make_seeded_lab_dir(tmp_path)
    c = _course(admin_session, tenant_a.id)
    templates = import_labs(
        admin_session,
        tenant_id=tenant_a.id,
        course_id=c.id,
        labs_dir=root / "labs",
    )
    admin_session.flush()
    lt = templates[0]
    assert lt.instructions_md is not None
    assert "{{lan_octet}}" in lt.instructions_md
    admin_session.rollback()


def test_source_hash_changes_when_only_raw_markdown_changes(admin_session, tenant_a, tmp_path):
    root = _make_seeded_lab_dir(tmp_path)
    c = _course(admin_session, tenant_a.id)
    t1 = import_labs(admin_session, tenant_id=tenant_a.id, course_id=c.id, labs_dir=root / "labs")
    admin_session.flush()
    v1 = t1[0].version
    h1 = t1[0].source_hash

    # Edit the markdown source in a way that does not change the rendered
    # HTML at all (python-markdown collapses any run of blank lines between
    # blocks to the same single paragraph break) — only the raw markdown
    # text differs. A hash keyed only on rendered instructions_html would
    # treat this as unchanged; the raw markdown must be included so it is
    # picked up as a real content change.
    edited = _SEEDED_INSTRUCTIONS.replace("\n\n<script", "\n\n\n\n<script")
    assert edited != _SEEDED_INSTRUCTIONS
    (root / "labs" / "03-vlan-segmentation" / "instructions.md").write_text(edited, encoding="utf-8")
    t2 = import_labs(admin_session, tenant_id=tenant_a.id, course_id=c.id, labs_dir=root / "labs")
    admin_session.flush()
    assert t2[0].source_hash != h1
    assert t2[0].version == v1 + 1
    admin_session.rollback()


def test_render_instance_instructions_interpolates_per_instance_seed(admin_session, tenant_a, tmp_path):
    root = _make_seeded_lab_dir(tmp_path)
    c = _course(admin_session, tenant_a.id)
    lt = import_labs(admin_session, tenant_id=tenant_a.id, course_id=c.id, labs_dir=root / "labs")[0]
    admin_session.flush()

    inst_a = LabInstance(
        tenant_id=tenant_a.id,
        activity_id=lt.activity_id,
        person_id=c.id,
        instance_name="dal-a",
        seed={"lan_octet": 5},
        status="active",
        consoles={},
    )
    inst_b = LabInstance(
        tenant_id=tenant_a.id,
        activity_id=lt.activity_id,
        person_id=c.id,
        instance_name="dal-b",
        seed={"lan_octet": 9},
        status="active",
        consoles={},
    )

    html_a = render_instance_instructions(lt, inst_a)
    html_b = render_instance_instructions(lt, inst_b)

    assert "10.0.5.10" in html_a
    assert "{{lan_octet}}" not in html_a
    assert "10.0.9.10" in html_b
    # Two learners, same template, different seeds -> different rendered HTML.
    assert html_a != html_b
    admin_session.rollback()


def test_render_instance_instructions_sanitizes_unsafe_html(admin_session, tenant_a, tmp_path):
    root = _make_seeded_lab_dir(tmp_path)
    c = _course(admin_session, tenant_a.id)
    lt = import_labs(admin_session, tenant_id=tenant_a.id, course_id=c.id, labs_dir=root / "labs")[0]
    admin_session.flush()

    inst = LabInstance(
        tenant_id=tenant_a.id,
        activity_id=lt.activity_id,
        person_id=c.id,
        instance_name="dal-xss",
        seed={"lan_octet": 5},
        status="active",
        consoles={},
    )
    html = render_instance_instructions(lt, inst)
    assert "<script" not in html
    assert "alert(" not in html
    admin_session.rollback()


def test_render_instance_instructions_falls_back_to_legacy_html_when_md_is_none(admin_session, tenant_a, tmp_path):
    root = _make_seeded_lab_dir(tmp_path)
    c = _course(admin_session, tenant_a.id)
    lt = import_labs(admin_session, tenant_id=tenant_a.id, course_id=c.id, labs_dir=root / "labs")[0]
    admin_session.flush()
    # Simulate a pre-migration/backfill-absent row.
    lt.instructions_md = None
    admin_session.flush()

    inst = LabInstance(
        tenant_id=tenant_a.id,
        activity_id=lt.activity_id,
        person_id=c.id,
        instance_name="dal-legacy",
        seed={"lan_octet": 5},
        status="active",
        consoles={},
    )
    html = render_instance_instructions(lt, inst)
    assert html == lt.instructions_html
    admin_session.rollback()
