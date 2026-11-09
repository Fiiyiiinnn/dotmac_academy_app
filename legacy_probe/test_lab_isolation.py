"""Cross-tenant isolation canary for lab tables.

Mirrors tests/test_assessment_isolation.py: insert a LabInstance in tenant A,
then prove it's invisible under an app_user session scoped to tenant B.
"""

import uuid

from dotmac_kernel.models import Party, PartyPerson, PartyType
from sqlalchemy import text

from app.models.assessment import Activity
from app.models.course import Course
from app.models.lab import LabInstance


def test_lab_instance_invisible_across_tenants(admin_session, app_user_session, tenant_a, tenant_b):
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

    a = Activity(
        tenant_id=tenant_a.id, course_id=c.id, chapter_number=14, type="lab", title="VLAN lab", pass_threshold=0.6, grading="auto", weight=1.0
    )
    admin_session.add(a)
    admin_session.flush()

    p = Party(
        tenant_id=tenant_a.id, party_type=PartyType.person, display_name="Lab Rat",
        email=f"learner-{uuid.uuid4().hex[:8]}@a.example.com", is_active=True,
    )
    admin_session.add(p)
    admin_session.flush()
    admin_session.add(PartyPerson(party_id=p.id, first_name="Lab", last_name="Rat"))
    admin_session.flush()

    li = LabInstance(
        tenant_id=tenant_a.id,
        activity_id=a.id,
        person_id=p.id,
        instance_name=f"dal-{uuid.uuid4().hex[:8]}",
        seed={"o": 5},
        status="queued",
        consoles={},
    )
    admin_session.add(li)
    admin_session.commit()

    # Simulate an app_user session scoped to tenant_b.
    # SET command does not accept bound parameters in PostgreSQL — safe to interpolate UUID.
    app_user_session.execute(text(f"SET app.current_tenant = '{tenant_b.id}'"))
    rows = app_user_session.execute(text("SELECT count(*) FROM lab_instances")).scalar()
    app_user_session.execute(text("RESET app.current_tenant"))
    assert rows == 0

    # Cleanup
    admin_session.query(LabInstance).filter(LabInstance.id == li.id).delete()
    admin_session.query(PartyPerson).filter(PartyPerson.party_id == p.id).delete()
    admin_session.query(Party).filter(Party.id == p.id).delete()
    admin_session.query(Activity).filter(Activity.id == a.id).delete()
    admin_session.query(Course).filter(Course.id == c.id).delete()
    admin_session.commit()
