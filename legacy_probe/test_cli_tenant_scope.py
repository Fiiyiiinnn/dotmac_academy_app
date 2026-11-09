"""A CLI session with no tenant scope sees nothing, and says nothing about it.

This is the bug these tests pin. `get_db` set `app.current_tenant` per request;
nothing set it for the CLI, which opens `SessionLocal()` directly. RLS fails
closed, so `audit-banks --tenant-slug dotmac` printed `TOTAL 0 0` against a
production database holding 333 banks for that exact tenant — a compliance tool
reporting a clean estate because it could not see the estate.

The tenant lookup is what made it invisible: `tenants` is not tenant-scoped, so
resolving the slug succeeds and the command never errors. Everything after it is
filtered to nothing in silence.

`test_unscoped_session_is_blind` is the important one. It fails on the fixed code
if anyone ever makes RLS fail *open*, and it documents why the scope is not
optional — without it, a passing `audit-banks` means nothing at all.
"""

from __future__ import annotations

import uuid

from dotmac_kernel.session_runtime import resolve_database_runtime
from sqlalchemy import text

from app.models.assessment import Question, QuestionBank
from app.models.course import Course


def _seed_bank(admin_session, tenant):
    course = Course(
        tenant_id=tenant.id,
        slug=f"pwr-{uuid.uuid4().hex[:8]}",
        title="Power",
        discipline="power-site-infrastructure",
        source_ref="x",
        version=1,
        status="published", listed=False,
    )
    admin_session.add(course)
    admin_session.flush()
    bank = QuestionBank(tenant_id=tenant.id, course_id=course.id, chapter_number=1, kind="chapter", version=1)
    admin_session.add(bank)
    admin_session.flush()
    admin_session.add(
        Question(
            tenant_id=tenant.id,
            bank_id=bank.id,
            ext_id=f"pwr-ch01-q01-{uuid.uuid4().hex[:6]}",
            stem="?",
            type="single",
            options=["a", "b", "c", "d"],
            correct=["a"],
            rubric_category="recall",
            category="power",
            explanation="",
            weight=1,
        )
    )
    admin_session.commit()
    return course, bank


def _cleanup(admin_session, course, bank):
    admin_session.query(Question).filter(Question.bank_id == bank.id).delete()
    admin_session.query(QuestionBank).filter(QuestionBank.id == bank.id).delete()
    admin_session.query(Course).filter(Course.id == course.id).delete()
    admin_session.commit()


def test_unscoped_session_is_blind(admin_session, app_user_session, tenant_a):
    """No scope set: the bank exists, and the session reports zero.

    This is what `audit-banks` was doing on production. Note there is no error —
    a caller cannot distinguish this from a genuinely empty estate.
    """
    course, bank = _seed_bank(admin_session, tenant_a)
    try:
        assert app_user_session.query(QuestionBank).count() == 0
    finally:
        app_user_session.rollback()
        _cleanup(admin_session, course, bank)


def test_a_transaction_local_scope_does_not_survive_a_commit(admin_session, app_user_session, tenant_a):
    """The regression. A CLI command that commits in a loop loses its scope.

    `SET LOCAL` dies with the transaction, `expire_on_commit` reloads attributes
    on the next statement, and that statement runs unscoped — so a row the same
    session just wrote comes back invisible. `load-banks` hit exactly this on
    the first bank it committed, as `ObjectDeletedError`.
    """
    course, bank = _seed_bank(admin_session, tenant_a)
    try:
        resolve_database_runtime().set_tenant(app_user_session, tenant_a.id, transaction_local=True)
        assert app_user_session.query(QuestionBank).filter(QuestionBank.id == bank.id).count() == 1
        app_user_session.commit()
        assert app_user_session.query(QuestionBank).filter(QuestionBank.id == bank.id).count() == 0
    finally:
        app_user_session.rollback()
        _cleanup(admin_session, course, bank)


def test_a_session_level_scope_survives_a_commit(admin_session, app_user_session, tenant_a):
    """What the CLI needs, and what it now asks for."""
    course, bank = _seed_bank(admin_session, tenant_a)
    try:
        resolve_database_runtime().set_tenant(app_user_session, tenant_a.id, transaction_local=False)
        assert app_user_session.query(QuestionBank).filter(QuestionBank.id == bank.id).count() == 1
        app_user_session.commit()
        assert app_user_session.query(QuestionBank).filter(QuestionBank.id == bank.id).count() == 1
        app_user_session.commit()
        assert app_user_session.query(QuestionBank).filter(QuestionBank.id == bank.id).count() == 1
    finally:
        app_user_session.execute(text("RESET app.current_tenant"))
        app_user_session.rollback()
        _cleanup(admin_session, course, bank)


def test_set_tenant_makes_the_rows_visible(admin_session, app_user_session, tenant_a):
    """The fix: same session, same query, scope applied."""
    course, bank = _seed_bank(admin_session, tenant_a)
    try:
        resolve_database_runtime().set_tenant(app_user_session, tenant_a.id)
        assert app_user_session.query(QuestionBank).filter(QuestionBank.id == bank.id).count() == 1
        assert app_user_session.query(Course).filter(Course.id == course.id).count() == 1
    finally:
        app_user_session.execute(text("RESET app.current_tenant"))
        app_user_session.rollback()
        _cleanup(admin_session, course, bank)


def test_set_tenant_does_not_widen_beyond_the_tenant(admin_session, app_user_session, tenant_a, tenant_b):
    """Applying a scope must not become a way to see everything.

    Worth pinning separately: the natural over-correction for "the CLI sees
    nothing" is to run it as a BYPASSRLS role, which would make every CLI command
    cross-tenant by default.
    """
    course, bank = _seed_bank(admin_session, tenant_a)
    try:
        resolve_database_runtime().set_tenant(app_user_session, tenant_b.id)
        assert app_user_session.query(QuestionBank).filter(QuestionBank.id == bank.id).count() == 0
    finally:
        app_user_session.execute(text("RESET app.current_tenant"))
        app_user_session.rollback()
        _cleanup(admin_session, course, bank)
