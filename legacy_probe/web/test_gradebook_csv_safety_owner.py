"""Genuine owner-backed Gradebook CSV regression; authored for hosted CI, NOT RUN."""

import csv
import io
from uuid import uuid4

import pytest
from dotmac_kernel.models import Party, PartyPerson
from dotmac_learning.models import Activity
from sqlalchemy import select, update

from app.services.gradebook_access import require_gradebook_access
from app.services.web_auth import require_web_user
from app.web import gradebook as routes
from legacy_probe.services.test_canonical_current_user import current_account
from legacy_probe.web.test_bookmarks_owner import _request
from legacy_probe.web.test_catalog_gradebook_owner import _allow, _restore_policy, _seed
from legacy_probe.web.test_reports_access_policy import _nominate_cohort

assert current_account and _restore_policy


@pytest.mark.parametrize(
    "prefix,escaped_name,escaped_text",
    [("=", "'=", "'="), ("+", "'+", "'+"), ("-", "'-", "'-"),
     ("@", "'@", "'@"), ("\t", "", "'\t"), ("\r", "", "'\r"),
     ("", "", "")],
)
def test_actual_gradebook_csv_neutralizes_source_text_preserving_schema_numbers_and_quotes(
    current_account, prefix, escaped_name, escaped_text,
):
    a = current_account
    _allow(a)
    course, cohort, people = _seed(a)
    # Cohort data needs a cohort scope (#188): hold an active nomination for it.
    _nominate_cohort(a, cohort.id)
    title = 'Quiz, "quoted"'
    first_name = 'First, "quoted"'
    last_name = 'Last, "quoted"'
    email_tail = f"csv-{uuid4().hex}@example.test"
    # These are recipient facts, not the authenticated account's login email.
    # The owner fixture writes raw historical text, including tab/CR email facts.
    a.admin.execute(update(Party).where(Party.id == people[0].id).values(email=prefix + email_tail))
    a.admin.execute(update(PartyPerson).where(PartyPerson.party_id == people[0].id)
                    .values(first_name=prefix + first_name, last_name=last_name))
    activities = a.admin.scalars(select(Activity).where(Activity.tenant_id == a.tid,
        Activity.course_id == course.id).order_by(Activity.chapter_number)).all()
    activities[0].title = prefix + title
    a.admin.commit()
    request = _request(a.tenant, a.token)
    account = require_gradebook_access(request, require_web_user(request, a.db), a.db)
    root = a.db.get_transaction()
    response = routes.gradebook_csv(cohort.id, request, account, a.db)
    rows = list(csv.reader(io.StringIO(response.body.decode(), newline="")))
    assert rows[0] == ["name", "email", escaped_text + title + " (w=1.0)", "Quiz 1 (w=3.0)", "final_pct"]
    changed = next(row for row in rows[1:] if row[1] == escaped_text + email_tail)
    assert changed[0] == escaped_name + first_name + " " + last_name
    assert changed[2:] == ["100", "0", "25"]
    ordinary = next(row for row in rows[1:] if row[1] == people[1].email)
    assert ordinary == ["Notification Recipient", people[1].email, "0", "0", "0"]
    assert len(rows) == 3
    assert response.headers["content-disposition"] == f"attachment; filename=gradebook-{cohort.id}.csv"
    assert a.db.get_transaction() is root
