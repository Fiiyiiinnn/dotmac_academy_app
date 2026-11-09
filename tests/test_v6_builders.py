"""Transform-builder proof: legacy-shaped rows -> SnapshotV6 -> genesis.

Builds a synthetic legacy public-schema database (column inventory
taken from production metadata), seeds a minimal course/bank/activity
closure, builds the snapshot with `scripts.v6_builders`, imports it
into a genesis destination and verifies. This is the rehearsal core
that the production snapshot run will execute unchanged.
"""

from __future__ import annotations

import collections
import uuid
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]

LEGACY_TABLES = [
    "courses",
    "activities",
    "submissions",
    "scores",
    "course_completions",
    "certificates",
    "learning_events",
    "chapters",
    "question_banks",
    "questions",
    "chapter_reads",
    "subtopic_reads",
    "chapter_bookmarks",
    "chapter_notes",
    "cohorts",
    "enrollments",
    "tracks",
    "track_courses",
    "cohort_tracks",
    "course_offerings",
    "course_prerequisites",
    "activity_attempts",
    "attempt_grants",
    "offering_activities",
    "class_sessions",
    "announcements",
    "success_queue_entries",
]

_TYPE_MAP = {
    "uuid": "uuid",
    "character varying": "text",
    "text": "text",
    "integer": "integer",
    "bigint": "bigint",
    "double precision": "double precision",
    "boolean": "boolean",
    "jsonb": "jsonb",
    "timestamp with time zone": "timestamptz",
    "date": "date",
}


def _legacy_schema() -> str:
    inventory = (ROOT / "legacy_probe" / "prod_columns.txt").read_text().splitlines()
    cols: dict[str, list[str]] = collections.defaultdict(list)
    for line in inventory:
        table, column, dtype, _null = line.split("|")
        if table in LEGACY_TABLES:
            cols[table].append(f"{column} {_TYPE_MAP[dtype]}")
    return "\n".join(f"CREATE TABLE public.{t} ({', '.join(c)});" for t, c in sorted(cols.items()))


def test_builders_roundtrip_from_synthetic_legacy(genesis_database):
    from dotmac_learning import bank_transfer

    from scripts.v6_builders import build_snapshot

    _admin, superuser, dest_name = genesis_database
    dest_url = make_url(superuser).set(database=dest_name).render_as_string(hide_password=False)
    legacy_name = f"academy_legacy_{uuid.uuid4().hex[:12]}"
    server = create_engine(superuser, isolation_level="AUTOCOMMIT")
    tenant_id, course_id, bank_id, activity_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    try:
        with server.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{legacy_name}"'))
        legacy_url = make_url(superuser).set(database=legacy_name).render_as_string(hide_password=False)
        legacy = create_engine(legacy_url)
        with legacy.begin() as conn:
            conn.execute(text(_legacy_schema()))
            conn.execute(
                text(
                    "INSERT INTO courses (id, tenant_id, slug, title, discipline, source_ref, "
                    "version, status, listed, created_at, updated_at) VALUES "
                    "(:id, :t, 'fiber-basics', 'Fiber Basics', 'networking', 'seed:legacy', 1, "
                    "'published', true, now(), now())"
                ),
                {"id": course_id, "t": tenant_id},
            )
            conn.execute(
                text(
                    "INSERT INTO question_banks (id, tenant_id, course_id, chapter_number, kind, "
                    "version, created_at, updated_at) VALUES (:id, :t, :c, 1, 'quiz', 3, now(), now())"
                ),
                {"id": bank_id, "t": tenant_id, "c": course_id},
            )
            conn.execute(
                text(
                    "INSERT INTO questions (id, tenant_id, bank_id, ext_id, stem, type, options, "
                    "correct, rubric_category, explanation, weight, created_at, updated_at, category) "
                    "VALUES (:id, :t, :b, 'Q1', 'Color of fiber?', 'mcq', '[\"a\",\"b\"]'::jsonb, "
                    "'[\"a\"]'::jsonb, 'basics', '', 1, now(), now(), NULL)"
                ),
                {"id": uuid.uuid4(), "t": tenant_id, "b": bank_id},
            )
            conn.execute(
                text(
                    "INSERT INTO activities (id, tenant_id, course_id, chapter_number, type, bank_id, "
                    "title, pass_threshold, created_at, updated_at, max_attempts, grading, "
                    "question_count, weight, assessment_mode) VALUES (:id, :t, :c, 1, 'mcq_test', :b, "
                    "'Quiz 1', 0.7, now(), now(), 3, 'auto', 1, 1.0, 'graded')"
                ),
                {"id": activity_id, "t": tenant_id, "c": course_id, "b": bank_id},
            )
        with legacy.connect() as conn, conn.begin():
            snapshot, exceptions = build_snapshot(conn, (tenant_id,))
        legacy.dispose()
        assert exceptions == []
        assert len(snapshot.courses) == 1
        assert len(snapshot.question_bank_versions) == 1
        version = snapshot.question_bank_versions[0]
        assert version.state == "published" and version.origin == "backfill"
        assert version.version_no == 3
        assert snapshot.questions[0].bank_version_id == version.id
        assert snapshot.activities[0].bank_version_id == version.id
        assert snapshot.question_banks[0].current_version_id == version.id

        dest = create_engine(dest_url)
        with dest.begin() as conn:
            conn.execute(
                text("INSERT INTO tenants (id, slug, name) VALUES (:id, :s, 'Legacy RT')"),
                {"id": tenant_id, "s": f"lrt{uuid.uuid4().hex[:8]}"},
            )
        with dest.connect() as conn, conn.begin():
            observation = bank_transfer.import_snapshot_v6(conn, snapshot)
            bank_transfer.verify_snapshot_v6(conn, snapshot)
        dest.dispose()
        assert sum(r.count for r in observation.inserted_rows) >= 5
    finally:
        with server.connect() as conn:
            conn.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :name AND pid <> pg_backend_pid()"
                ),
                {"name": legacy_name},
            )
            conn.execute(text(f'DROP DATABASE IF EXISTS "{legacy_name}"'))
        server.dispose()
