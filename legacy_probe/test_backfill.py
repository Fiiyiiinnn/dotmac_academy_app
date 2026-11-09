"""Backfill of ERP applicants into academy admissions."""

from __future__ import annotations

from sqlalchemy import text

from scripts.backfill_applicants import backfill


def test_backfill_imports_and_is_idempotent(app_client, tenant_a, admin_session):
    rows = [
        {
            "external_ref": "erp-1",
            "email": "A@x.ex",
            "first_name": "A",
            "last_name": "One",
            "phone": "0801",
            "applied_on": "2026-05-01",
        },
        {
            "external_ref": "erp-2",
            "email": "b@x.ex",
            "first_name": "B",
            "last_name": "Two",
            "applied_on": "2026-05-02T09:00:00+00:00",
        },
        {"external_ref": "erp-3", "email": "", "first_name": "No", "last_name": "Email"},  # skipped
    ]

    admin_session.rollback()
    s1 = backfill(admin_session, tenant_id=tenant_a.id, rows=rows)
    admin_session.commit()
    assert s1 == {"imported": 2, "skipped": 1, "total": 3}

    got = admin_session.execute(
        text(
            "SELECT email, source, external_ref, program, status, applied_on "
            "FROM applicants ORDER BY email"
        )
    ).all()
    assert len(got) == 2
    emails = {r[0] for r in got}
    assert emails == {"a@x.ex", "b@x.ex"}  # normalised lower-case
    for r in got:
        assert r[1] == "erp_backfill"
        assert r[2] in ("erp-1", "erp-2")
        assert r[3] == "Fiber Academy"
        assert r[4] == "applied"
    # applied_on parsed from both plain date and ISO-datetime forms
    dates = {str(r[5]) for r in got}
    assert dates == {"2026-05-01", "2026-05-02"}

    # Re-run: no duplicates (idempotent on email).
    admin_session.rollback()
    s2 = backfill(admin_session, tenant_id=tenant_a.id, rows=rows)
    admin_session.commit()
    assert s2["imported"] == 2
    n = admin_session.execute(text("SELECT count(*) FROM applicants")).scalar()
    assert n == 2
