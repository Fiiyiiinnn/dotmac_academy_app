import uuid

from sqlalchemy import func, select, text

from app.models.assessment import QuestionBank
from app.models.course import Course


def test_question_bank_invisible_across_tenants(admin_session, app_user_session, tenant_a, tenant_b):
    # Create a Course in tenant_a (required FK for QuestionBank)
    c = Course(
        tenant_id=tenant_a.id,
        slug=f"foundation-{uuid.uuid4().hex[:8]}",
        title="F",
        discipline="networking",
        source_ref="x",
        version=1,
        status="published", listed=False,
    )
    admin_session.add(c)
    admin_session.flush()

    # Create a QuestionBank in tenant_a
    bank = QuestionBank(tenant_id=tenant_a.id, course_id=c.id, chapter_number=1, kind="chapter", version=1)
    admin_session.add(bank)
    admin_session.commit()

    # Simulate an app_user session scoped to tenant_b.
    # SET command does not accept bound parameters in PostgreSQL — safe to interpolate UUID.
    app_user_session.execute(text(f"SET app.current_tenant = '{tenant_b.id}'"))
    rows = app_user_session.execute(select(func.count()).select_from(QuestionBank)).scalar()
    app_user_session.execute(text("RESET app.current_tenant"))
    assert rows == 0

    # Cleanup
    admin_session.query(QuestionBank).filter(QuestionBank.id == bank.id).delete()
    admin_session.query(Course).filter(Course.id == c.id).delete()
    admin_session.commit()
