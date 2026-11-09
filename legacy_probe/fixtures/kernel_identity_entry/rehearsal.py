"""Proposed mechanics confined to tests pending hosted PostgreSQL evidence.

No identity import, FK rebind, runtime composition, or future-floor claim.
Only the eight colliding legacy relations and their genuine Alembic history
move. PostgreSQL retains their OIDs and existing incoming references.
"""

from __future__ import annotations

import ast
import hashlib
import importlib.metadata
import json
from dataclasses import dataclass, field
from pathlib import Path

from dotmac_kernel.migrations import versions_dir
from sqlalchemy import text

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

FIXTURE = Path(__file__).resolve().parent
LEGACY_HEAD = "0061_lab_completion_grants"
MANIFEST_SHA256 = "0c6f477bcdb8477a6e34c6b15809f71f7382c2cc8ce3b43e72c387daa6879a37"
COMPATIBILITY_SCHEMA = "academy_identity_compat"
COLLISIONS = (
    "tenants", "tenant_domains", "people", "user_credentials",
    "auth_sessions", "roles", "person_roles", "audit_events",
)
RELOCATIONS = (*COLLISIONS, "alembic_version")


@dataclass(frozen=True)
class SourceRevision:
    revision: str
    sha256: str


@dataclass(frozen=True)
class InstalledKernel:
    version: str
    head: str
    revisions: tuple[SourceRevision, ...]


@dataclass(frozen=True, repr=False)
class Catalog:
    # Digests stay private in assertion rendering, including synthetic auth rows.
    relations: tuple = field(repr=False)
    constraints: tuple = field(repr=False)
    columns: tuple = field(repr=False)
    policies: tuple = field(repr=False)
    indexes: tuple = field(repr=False)
    rows: tuple = field(repr=False)
    versions: tuple[str, ...]


@dataclass(frozen=True)
class EntryPlan:
    source_version: str
    manifest_sha256: str
    compatibility_schema: str
    catalog: Catalog = field(repr=False)


def _fixture_config(connection, script_root, applied, fail_after, cfg):
    """Keep the genuine supplied-connection adapter and revision callbacks."""
    script_root.mkdir(parents=True, exist_ok=True)
    (script_root / "env.py").write_bytes((FIXTURE / "env.py").read_bytes())
    cfg.set_main_option("script_location", str(script_root))
    cfg.attributes["connection"] = connection

    def on_apply(*, ctx, step, heads, run_args):
        for revision in step.up_revision_ids:
            script = ScriptDirectory.from_config(cfg).get_revision(revision)
            applied.append(SourceRevision(revision, hashlib.sha256(Path(script.path).read_bytes()).hexdigest()))
        if fail_after is not None and len(applied) >= fail_after:
            raise RuntimeError("injected canonical replay failure")

    cfg.attributes["on_version_apply"] = on_apply
    return cfg


def config_for(connection, directory: Path, script_root: Path, applied: list, fail_after=None):
    """Legacy-only or installed-Kernel replay; never a composed graph entry."""
    cfg = Config()
    cfg.set_main_option("version_locations", str(directory))
    return _fixture_config(connection, script_root, applied, fail_after, cfg)


def composed_config_for(connection, script_root: Path, applied: list, fail_after=None):
    """Configure every installed owner/binding before fixture graph discovery."""
    from app.migration_graph import ordinary_migration_config

    cfg = ordinary_migration_config()
    return _fixture_config(connection, script_root, applied, fail_after, cfg)


def verify_legacy_source():
    content = (FIXTURE / "manifest.json").read_bytes()
    if hashlib.sha256(content).hexdigest() != MANIFEST_SHA256:
        raise ValueError("legacy source manifest drift")
    data = json.loads(content)
    files = [item for item in data["files"] if item["path"].startswith("alembic/versions/")]
    if len(files) != 62:
        raise ValueError("legacy revision closure drift")
    for item in files:
        raw = (FIXTURE / item["path"]).read_bytes()
        if len(raw) != item["bytes"] or hashlib.sha256(raw).hexdigest() != item["sha256"]:
            raise ValueError("legacy migration bytes drift")


def installed_kernel(connection, script_root):
    cfg = config_for(connection, Path(versions_dir()), script_root, [])
    scripts = ScriptDirectory.from_config(cfg)
    heads = scripts.get_heads()
    if len(heads) != 1:
        raise ValueError("installed Kernel must have one head")
    revisions = tuple(
        SourceRevision(script.revision, hashlib.sha256(Path(script.path).read_bytes()).hexdigest())
        for script in reversed(list(scripts.walk_revisions()))
    )
    return InstalledKernel(importlib.metadata.version("dotmac-kernel"), heads[0], revisions)


def frozen_rows(rows):
    """Keep catalog contracts plain immutable tuples, including PG arrays."""
    return tuple(tuple(tuple(value) if isinstance(value, list) else value for value in row) for row in rows)


def snapshot(connection, schema="public"):
    if schema not in {"public", COMPATIBILITY_SCHEMA}:
        raise ValueError("unsupported catalog namespace")
    relations = frozen_rows(connection.execute(text("""
        SELECT c.oid, c.relname, c.relkind, c.relowner, c.relrowsecurity,
               c.relforcerowsecurity, c.relacl::text
        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname=:schema AND c.relname=ANY(:names) ORDER BY c.relname
    """), {"schema": schema, "names": list(RELOCATIONS)}).tuples())
    oids = [row[0] for row in relations]
    constraints = frozen_rows(connection.execute(text("""
        SELECT oid, conname, contype, conrelid, confrelid, conkey, confkey,
               confupdtype, confdeltype, confmatchtype, condeferrable,
               condeferred, convalidated
        FROM pg_constraint WHERE conrelid=ANY(:oids) OR confrelid=ANY(:oids)
        ORDER BY oid
    """), {"oids": oids}).tuples())
    columns = frozen_rows(connection.execute(text("""
        SELECT attrelid, attnum, attname, atttypid, atttypmod, attnotnull,
               attidentity, attgenerated, attisdropped, d.adbin::text
        FROM pg_attribute a LEFT JOIN pg_attrdef d
          ON d.adrelid=a.attrelid AND d.adnum=a.attnum
        WHERE attrelid=ANY(:oids) AND attnum>0
        ORDER BY attrelid, attnum
    """), {"oids": oids}).tuples())
    policies = frozen_rows(connection.execute(text("""
        SELECT oid, polrelid, polname, polcmd, polroles, polqual::text, polwithcheck::text
        FROM pg_policy WHERE polrelid=ANY(:oids) ORDER BY oid
    """), {"oids": oids}).tuples())
    indexes = frozen_rows(connection.execute(text("""
        SELECT indexrelid, indrelid, indisunique, indisprimary, indisvalid,
               indkey, indexprs::text, indpred::text
        FROM pg_index WHERE indrelid=ANY(:oids) ORDER BY indexrelid
    """), {"oids": oids}).tuples())
    rows = []
    for name in RELOCATIONS:
        # Hash on the server; never fetch credential/session row values.
        count, digest = connection.execute(text(
            f'SELECT count(*), md5(coalesce(string_agg(row_digest, \'\' ORDER BY row_digest), \'\')) '  # noqa: S608 -- closed schema and relation allowlists
            f'FROM (SELECT md5(to_jsonb(t)::text) AS row_digest FROM "{schema}"."{name}" t) s'
        )).one()
        rows.append((name, count, digest))
    version_sql = f'SELECT version_num FROM "{schema}".alembic_version ORDER BY version_num'  # noqa: S608 -- closed schema allowlist above
    versions = tuple(connection.execute(text(version_sql)).scalars())
    return Catalog(relations, constraints, columns, policies, indexes, tuple(rows), versions)


def plan_entry(connection):
    verify_legacy_source()
    catalog = snapshot(connection)
    if catalog.versions != (LEGACY_HEAD,):
        raise ValueError("legacy source version drift")
    if len(catalog.relations) != len(RELOCATIONS) or any(row[2] != "r" for row in catalog.relations):
        raise ValueError("legacy relation shape drift")
    validate_owner_and_grants(connection, catalog)
    return EntryPlan(LEGACY_HEAD, MANIFEST_SHA256, COMPATIBILITY_SCHEMA, catalog)


def validate_owner_and_grants(connection, catalog):
    """Bounded fixture preflight, not a production privilege certification."""
    oids = [row[0] for row in catalog.relations]
    safe = connection.execute(text("""
        SELECT bool_and(pg_get_userbyid(c.relowner)='app_admin')
        AND NOT EXISTS (
            SELECT 1 FROM pg_class t,
            LATERAL aclexplode(coalesce(t.relacl,acldefault('r',t.relowner))) acl
            WHERE t.oid=ANY(:oids) AND acl.grantee=0
        ) FROM pg_class c WHERE c.oid=ANY(:oids)
    """), {"oids": oids}).scalar_one()
    if not safe:
        raise ValueError("legacy owner or PUBLIC grant drift")
    online_safe = connection.execute(text("""
        SELECT count(*)=2 AND bool_and(NOT rolsuper AND NOT rolbypassrls)
        FROM pg_roles WHERE rolname IN ('app_user','platform_api')
    """)).scalar_one()
    if not online_safe:
        raise ValueError("online role privilege drift")


def relocate(connection, plan: EntryPlan):
    if not connection.in_transaction():
        raise ValueError("caller-owned transaction required")
    if (plan.source_version, plan.manifest_sha256, plan.compatibility_schema) != (
        LEGACY_HEAD, MANIFEST_SHA256, COMPATIBILITY_SCHEMA,
    ):
        raise ValueError("entry contract drift")
    verify_legacy_source()
    if connection.execute(text("SELECT current_user")).scalar_one() != "app_admin":
        raise ValueError("migration owner must be app_admin")
    if connection.execute(text("SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname=:name)"),
                          {"name": COMPATIBILITY_SCHEMA}).scalar_one():
        raise ValueError("compatibility namespace already exists")
    connection.execute(text("LOCK TABLE " + ", ".join(f'public."{name}"' for name in RELOCATIONS) + " IN ACCESS EXCLUSIVE MODE"))
    if snapshot(connection) != plan.catalog:
        raise ValueError("legacy catalog or row drift")
    validate_owner_and_grants(connection, plan.catalog)
    connection.execute(text(f'CREATE SCHEMA "{COMPATIBILITY_SCHEMA}" AUTHORIZATION app_admin'))
    for name in RELOCATIONS:
        connection.execute(text(f'ALTER TABLE public."{name}" SET SCHEMA "{COMPATIBILITY_SCHEMA}"'))


def replay_kernel(connection, script_root, applied, fail_after=None):
    # The published package's entire graph is used, never a copied/subset chain.
    cfg = config_for(connection, Path(versions_dir()), script_root, applied, fail_after)
    command.upgrade(cfg, "heads")


def revision_assignments(path):
    values = {}
    for node in ast.parse(path.read_bytes()).body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in {"revision", "down_revision"}:
                    values[target.id] = ast.literal_eval(node.value)
    return values
