"""Labs-as-code loader (Task 5).

Parses on-disk lab definitions (``lab.yaml`` + ``topo.clab.yml`` +
``instructions.md``) into :class:`LabSpec` dataclasses and upserts them into the
database as ``Activity(type="lab")`` + :class:`LabTemplate` pairs.

``lab.yaml`` schema (flat top-level keys; a ``lab:`` wrapper is also accepted)::

    slug: vlan-segmentation        # or `id`
    title: VLAN Segmentation
    chapter_ref: 14                # or `chapter_number`
    topology: topo.clab.yml        # file, relative to the lab dir
    instructions: instructions.md  # file, relative to the lab dir
    seed_spec:                     # or `seed`; dict, passed verbatim
      lan_octet: {type: int, min: 2, max: 9}
    limits:                        # dict, passed verbatim
      time_minutes: 45
      ready_wait_seconds: 120
      pass_threshold: 0.7          # optional; Settings supplies the default
    checks:                        # list of check dicts, stored verbatim
      - {id: c1, type: probe, node: client, weight: 1, probe: ping}

``instructions.md`` may contain ``$include: <manual-file>#<anchor>`` directive
lines; each is replaced by the slice of the referenced chapter markdown around
the heading whose slug matches ``<anchor>`` (resolved against ``chapters_dir``).
Unresolvable includes degrade gracefully (whole file, or a neutral note) rather
than crashing.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

import dotmac_learning as learning
import markdown as md
import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.lab import LabInstance, LabTemplate
from app.services import authoring, lab_seed
from app.services.settings_store import EffectiveSettings, effective

# A standalone `$include: file.md#anchor` directive line.
_INCLUDE = re.compile(r"^[ \t]*\$include:[ \t]*(?P<file>[^#\s]+)(?:#(?P<anchor>\S+))?[ \t]*$")
# A markdown ATX heading: capture level (#count) and text.
_HEADING = re.compile(r"^(#{1,6})[ \t]+(.*?)[ \t]*#*$")


@dataclass
class LabSpec:
    """Parsed, file-resolved representation of a single lab directory."""

    slug: str
    title: str
    chapter_number: int | None
    topology_text: str
    instructions_md: str
    checks: list = field(default_factory=list)
    seed_spec: dict = field(default_factory=dict)
    limits: dict = field(default_factory=dict)
    access: list = field(default_factory=list)


_ACCESS_PROTOCOLS = frozenset({"console", "webfig", "ssh"})
_ACCESS_KEYS = frozenset({"node", "protocol", "username", "default_password", "note"})
_ACCESS_MAX_ENTRIES = 32
_NODE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,62}$")
_PLACEHOLDER = re.compile(r"\{\{.*?\}\}")
# Anything that looks like templating, markup or control characters is refused:
# the values are rendered as plain text and must stay that way.
_ACCESS_TEXT_FORBIDDEN = re.compile(r"[\x00-\x1f\x7f<>{}`$]")


#: ``default_password`` is for short, documented lab-device defaults. The loader
#: refuses anything that looks like a real credential, so a pasted secret fails
#: the import instead of being published to every learner:
#: * longer than 32 characters;
#: * a known token shape (GitHub, OpenAI-style ``sk-``, AWS access key id, JWT,
#:   Slack, PEM block);
#: * a long high-entropy string: at least 16 characters AND Shannon entropy of
#:   at least 3.6 bits per character. Random base64/hex of that length sits at
#:   4.0 or more (16 distinct characters give exactly 4.0), while human lab
#:   defaults such as ``admin`` or ``VR-netlab9`` are short or repetitive.
_PASSWORD_MAX = 32
_TOKEN_SHAPES = re.compile(r"ghp_|gho_|ghs_|ghu_|github_pat_|sk-|AKIA|ASIA|eyJ|xox[abprs]-|-----BEGIN", re.I)
_ENTROPY_MIN_LEN = 16
_ENTROPY_MIN_BITS = 3.6


def _entropy_bits_per_char(value: str) -> float:
    counts = {c: value.count(c) for c in set(value)}
    n = len(value)
    return -sum((k / n) * math.log2(k / n) for k in counts.values())


def _check_default_password(value: str, label: str) -> None:
    if len(value) > _PASSWORD_MAX:
        raise ValueError(f"{label}: default_password longer than {_PASSWORD_MAX} characters looks like a real secret")
    if _TOKEN_SHAPES.search(value):
        raise ValueError(f"{label}: default_password looks like an access token or key")
    if len(value) >= _ENTROPY_MIN_LEN and _entropy_bits_per_char(value) >= _ENTROPY_MIN_BITS:
        raise ValueError(f"{label}: default_password looks randomly generated, not a documented lab default")


def _topology_nodes(topology_text: str) -> set[str] | None:
    """Node names declared by a containerlab topology, or None if unparsable."""
    try:
        doc = yaml.safe_load(_PLACEHOLDER.sub("0", topology_text))
        nodes = doc["topology"]["nodes"]
    except Exception:
        return None
    return {str(k) for k in nodes} if isinstance(nodes, dict) else None


def parse_access(raw, *, topology_text: str = "", where: str = "lab") -> list[dict]:
    """Validate the optional ``access:`` list of a ``lab.yaml``.

    Each entry describes how the owner logs in to ONE node of THIS lab:
    ``node`` (must exist in the topology), ``protocol`` (console, webfig, ssh),
    optional ``username``, optional ``default_password`` and optional ``note``.
    The password key is deliberately named ``default_password``: it is only for
    the disposable, documented default of a lab device that lives and dies with
    the learner's own lab, never for infrastructure credentials. Values are
    short plain text (no markup, templating or control characters). Any problem
    raises ``ValueError`` so a bad file fails the import rather than shipping.
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError(f"{where}: access must be a list")
    if len(raw) > _ACCESS_MAX_ENTRIES:
        raise ValueError(f"{where}: access has more than {_ACCESS_MAX_ENTRIES} entries")
    topo_nodes = _topology_nodes(topology_text) if topology_text else None
    if raw and topology_text and topo_nodes is None:
        raise ValueError(f"{where}: topology could not be parsed, so access nodes cannot be checked")
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for i, item in enumerate(raw):
        label = f"{where}: access[{i}]"
        if not isinstance(item, dict):
            raise ValueError(f"{label} must be a mapping")
        unknown = set(item) - _ACCESS_KEYS
        if unknown:
            raise ValueError(f"{label} has unknown key(s): {', '.join(sorted(map(str, unknown)))}")
        node = item.get("node")
        if not isinstance(node, str) or not _NODE_NAME.match(node):
            raise ValueError(f"{label}: node must be a plain node name")
        if topo_nodes is not None and node not in topo_nodes:
            raise ValueError(f"{label}: node {node!r} is not in the topology")
        protocol = item.get("protocol")
        if protocol not in _ACCESS_PROTOCOLS:
            raise ValueError(f"{label}: protocol must be one of {sorted(_ACCESS_PROTOCOLS)}")
        entry: dict = {"node": node, "protocol": protocol}
        for key, limit in (("username", 64), ("default_password", _PASSWORD_MAX), ("note", 200)):
            if key not in item or item[key] is None:
                continue
            val = item[key]
            if not isinstance(val, str) or not val.strip() or len(val) > limit:
                raise ValueError(f"{label}: {key} must be a non-empty string of at most {limit} characters")
            if _ACCESS_TEXT_FORBIDDEN.search(val):
                raise ValueError(f"{label}: {key} contains characters that are not allowed")
            if key == "default_password":
                _check_default_password(val, label)
            entry[key] = val
        if "default_password" in entry and "username" not in entry:
            raise ValueError(f"{label}: default_password requires username")
        if (node, protocol) in seen:
            raise ValueError(f"{label}: duplicate entry for {node!r} over {protocol}")
        seen.add((node, protocol))
        out.append(entry)
    return out


def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _slice_chapter(chapter_md: str, anchor: str) -> str | None:
    """Return the markdown from the heading whose slug==anchor up to the next
    heading of equal-or-higher level. ``None`` if no heading matches."""
    lines = chapter_md.splitlines()
    start = None
    start_level = 0
    for i, line in enumerate(lines):
        m = _HEADING.match(line)
        if not m:
            continue
        level = len(m.group(1))
        if start is None:
            if _slugify(m.group(2)) == anchor.lower():
                start = i
                start_level = level
        elif level <= start_level:
            return "\n".join(lines[start:i]).strip()
    if start is None:
        return None
    return "\n".join(lines[start:]).strip()


def _resolve_include(match: re.Match, chapters_dir: Path | None) -> str:
    file_name = match.group("file")
    anchor = match.group("anchor")
    if chapters_dir is None:
        return f"<!-- include {file_name} unavailable (no chapters dir) -->"
    src = Path(chapters_dir) / file_name
    if not src.is_file():
        return f"<!-- include {file_name} not found -->"
    text = src.read_text(encoding="utf-8")
    if anchor:
        sliced = _slice_chapter(text, anchor)
        if sliced is not None:
            return sliced
    # No anchor, or anchor not found: fall back to the whole referenced file.
    return text.strip()


def _resolve_includes(instructions_md: str, chapters_dir: Path | None) -> str:
    out: list[str] = []
    for line in instructions_md.splitlines():
        m = _INCLUDE.match(line)
        if m:
            out.append(_resolve_include(m, chapters_dir))
        else:
            out.append(line)
    return "\n".join(out)


def parse_lab(dir_path, chapters_dir=None) -> LabSpec:
    """Parse a lab directory (``lab.yaml`` + referenced files) into a LabSpec.

    Args:
        dir_path: The lab directory containing ``lab.yaml``.
        chapters_dir: Optional directory used to resolve ``$include:`` directives
            in the instructions markdown.
    """
    dir_path = Path(dir_path)
    data = yaml.safe_load((dir_path / "lab.yaml").read_text(encoding="utf-8")) or {}
    if "lab" in data and isinstance(data["lab"], dict):
        data = data["lab"]

    slug = str(data.get("slug") or data.get("id") or "")
    title = str(data.get("title", ""))

    chapter_raw = data.get("chapter_number", data.get("chapter_ref"))
    chapter_number = int(chapter_raw) if chapter_raw is not None else None

    topo_file = str(data.get("topology", "topo.clab.yml"))
    topology_text = (dir_path / topo_file).read_text(encoding="utf-8")

    instr_file = str(data.get("instructions", "instructions.md"))
    instructions_md = (dir_path / instr_file).read_text(encoding="utf-8")
    instructions_md = _resolve_includes(instructions_md, chapters_dir)

    checks = list(data.get("checks", []) or [])
    seed_spec = dict(data.get("seed_spec", data.get("seed", {})) or {})
    limits = dict(data.get("limits", {}) or {})
    access = parse_access(data.get("access"), topology_text=topology_text, where=f"lab {slug or dir_path.name}")

    return LabSpec(
        slug=slug,
        title=title,
        chapter_number=chapter_number,
        topology_text=topology_text,
        instructions_md=instructions_md,
        checks=checks,
        seed_spec=seed_spec,
        limits=limits,
        access=access,
    )


def _source_hash(spec: LabSpec, instructions_html: str) -> str:
    extra = {"access": spec.access} if spec.access else {}
    payload = json.dumps(
        {
            **extra,
            "slug": spec.slug,
            "title": spec.title,
            "chapter_number": spec.chapter_number,
            "topology": spec.topology_text,
            "instructions_html": instructions_html,
            # Raw Markdown now affects runtime output (per-instance seed
            # interpolation renders from it directly), so it must participate
            # in the hash — otherwise an instructions.md edit that happens to
            # render to identical HTML today (e.g. adding a placeholder that
            # wasn't there before but resolves to the same visible text) would
            # be silently skipped as "unchanged".
            "instructions_md": spec.instructions_md,
            "checks": spec.checks,
            "seed_spec": spec.seed_spec,
            "limits": spec.limits,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def import_labs(db: Session, *, tenant_id, course_id, labs_dir, chapters_dir=None) -> list[LabTemplate]:
    """Upsert all labs under ``labs_dir`` for a course.

    For each ``<labs_dir>/*/lab.yaml``: parse, render instructions to HTML,
    compute a ``source_hash`` over the source, and upsert a paired
    ``Activity(type="lab")`` + :class:`LabTemplate`, keyed by (course_id, slug).
    Idempotent: unchanged labs are skipped; ``version`` is bumped only when the
    ``source_hash`` changes. Does not commit.
    """
    templates: list[LabTemplate] = []
    cfg: EffectiveSettings | None = None

    def lab_settings() -> EffectiveSettings:
        nonlocal cfg
        if cfg is None:
            # Only this new Settings read suppresses autoflush. Existing lab
            # SELECTs and full caller flushes retain their original behavior.
            with db.no_autoflush:
                cfg = effective(db, require_store=True)
        return cfg

    for lab_yaml in sorted(Path(labs_dir).glob("*/lab.yaml")):
        spec = parse_lab(lab_yaml.parent, chapters_dir=chapters_dir)
        if not spec.slug:
            continue

        instructions_html = md.markdown(spec.instructions_md, extensions=["tables", "fenced_code"])
        source_hash = _source_hash(spec, instructions_html)
        threshold = (
            spec.limits["pass_threshold"]
            if "pass_threshold" in spec.limits
            else lab_settings()["learning_lab_pass_threshold"]
        )
        pass_threshold = float(cast(float | int | str, threshold))

        template = db.scalars(
            select(LabTemplate)
            .where(LabTemplate.tenant_id == tenant_id)
            .where(LabTemplate.course_id == course_id)
            .where(LabTemplate.slug == spec.slug)
        ).first()

        if template is None:
            settings = lab_settings()
            activity_id = learning.create_activity_identity(
                db,
                learning.ActivityCreate(
                    tenant_id=tenant_id,
                    course_id=course_id,
                    chapter_number=spec.chapter_number,
                    type="lab",
                    bank_id=None,
                    title=spec.title,
                    pass_threshold=pass_threshold,
                    assessment_mode=cast(str, settings["learning_default_activity_assessment_mode"]),
                    grading=cast(str, settings["learning_default_activity_grading"]),
                    weight=cast(float, settings["learning_default_activity_weight"]),
                    scoring_policy=cast(str, settings["learning_default_activity_scoring_policy"]),
                    min_attempt_interval_seconds=cast(int, settings["learning_default_attempt_cooldown_seconds"]),
                ),
            )
            template = LabTemplate(
                tenant_id=tenant_id,
                course_id=course_id,
                chapter_number=spec.chapter_number,
                activity_id=activity_id,
                slug=spec.slug,
                title=spec.title,
                topology=spec.topology_text,
                instructions_html=instructions_html,
                instructions_md=spec.instructions_md,
                checks=spec.checks,
                seed_spec=spec.seed_spec,
                limits=spec.limits,
                access=spec.access,
                source_hash=source_hash,
                version=1,
            )
            db.add(template)
        elif template.source_hash != source_hash:
            learning.stage_activity_patch(
                db,
                learning.ActivityPatch(
                    tenant_id=tenant_id,
                    id=template.activity_id,
                    title=spec.title,
                    chapter_number=spec.chapter_number,
                    pass_threshold=pass_threshold,
                ),
            )
            template.chapter_number = spec.chapter_number
            template.title = spec.title
            template.topology = spec.topology_text
            template.instructions_html = instructions_html
            template.instructions_md = spec.instructions_md
            template.checks = spec.checks
            template.seed_spec = spec.seed_spec
            template.limits = spec.limits
            template.access = spec.access
            template.source_hash = source_hash
            template.version += 1
        elif template.access != spec.access:
            # Hash unchanged but the stored access differs (for example the
            # column was added or reset after the template was imported): refresh
            # it without bumping the template version.
            template.access = spec.access
        # else: unchanged — skip

        templates.append(template)

    db.flush()
    return templates


def render_instance_instructions(template: LabTemplate, instance: LabInstance) -> str:
    """Render this instance's per-seed instructions HTML from the template.

    Selects ``instructions_md`` (the canonical Markdown source), falling back
    to the legacy ``instructions_html`` only when a template predates the
    ``instructions_md`` backfill (``instructions_md IS NULL``). ``{{key}}``
    placeholders are substituted with THIS instance's seed values before
    Markdown is rendered, so two learners with different seeds — but the same
    template — get different rendered HTML. Rendering never mutates the
    stored template row or the instance's seed.

    Uses ``authoring.render_markdown`` (not a bare ``markdown.markdown`` call)
    so the rendered HTML goes through the same sanitisation the in-app author
    editor already relies on — raw imported Markdown must never reach the
    page unsanitised.
    """
    source = template.instructions_md
    if source is None:
        return template.instructions_html
    interpolated = lab_seed.interpolate(source, instance.seed)
    return authoring.render_markdown(interpolated)
