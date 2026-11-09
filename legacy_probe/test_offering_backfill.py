"""Backfill semantics for the 0008_course_offerings migration.

The migration links every cohort to each course sharing its discipline, so the
cutover from discipline-string matching preserves existing access. This test
mirrors the migration's backfill statement and asserts it (a) creates the links
and (b) is idempotent via the ON CONFLICT guard.
"""

from __future__ import annotations

from sqlalchemy import func, literal, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.models.cohort import Cohort
from app.models.course import Course
from app.models.offering import CourseOffering

# Mirror of alembic/versions/0008_course_offerings.py upgrade() backfill, built from
# the mapped tables so the schema qualification follows the models.
_OFFERINGS = CourseOffering.__table__
_BACKFILL = pg_insert(_OFFERINGS).from_select(
    ["id", "tenant_id", "cohort_id", "course_id", "status", "created_at", "updated_at"],
    select(
        func.gen_random_uuid(),
        Cohort.tenant_id,
        Cohort.id,
        Course.id,
        literal("active"),
        func.now(),
        func.now(),
    ).join_from(Cohort, Course, (Course.tenant_id == Cohort.tenant_id) & (Course.discipline == Cohort.discipline)),
).on_conflict_do_nothing(index_elements=["tenant_id", "cohort_id", "course_id"])


def test_backfill_links_same_discipline_and_is_idempotent(admin_session, tenant_a):
    tid = tenant_a.id
    # Cohort + a matching-discipline course (linked) and a non-matching one (not linked).
    coh = Cohort(tenant_id=tid, name="Net Cohort", discipline="networking", status="active")
    match = Course(tenant_id=tid, slug="net", title="Net", discipline="networking",
                   source_ref="x", version=1, status="published", listed=False)
    other = Course(tenant_id=tid, slug="fiber", title="Fiber", discipline="fiber",
                   source_ref="x", version=1, status="published", listed=False)
    admin_session.add_all([coh, match, other])
    admin_session.flush()

    admin_session.execute(_BACKFILL)

    links = admin_session.scalars(
        select(CourseOffering.course_id).where(CourseOffering.tenant_id == tid)
        .where(CourseOffering.cohort_id == coh.id)
    ).all()
    assert match.id in links
    assert other.id not in links

    # Idempotent: a second run adds nothing (ON CONFLICT DO NOTHING).
    admin_session.execute(_BACKFILL)
    links2 = admin_session.scalars(
        select(CourseOffering.course_id).where(CourseOffering.tenant_id == tid)
        .where(CourseOffering.cohort_id == coh.id)
    ).all()
    assert len(links2) == len(links)
    admin_session.rollback()
