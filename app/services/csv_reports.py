"""Scheduled CSV report builders (roadmap P3b, item 23).

Pure projections: every function renders canonical state (roster, completion,
work states, Success Queue) into a CSV string. Download routes and the weekly
outbox attachment both call these builders — one producer, two transports.
"""

# Ported at new-composition genesis (learning-facing wave 2).

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID

import dotmac_learning as learning
from pydantic import JsonValue
from sqlalchemy.orm import Session

from app.services import insights
from app.services.reports import cohort_matrix

_CSV_INJECTION_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def sanitize_cell(value: object) -> object:
    """Neutralise CSV formula injection: a text cell starting with =, +, -, @,
    tab or CR is prefixed with a single quote so spreadsheet apps treat it as
    text, never a formula. Non-string cells pass through unchanged."""
    if isinstance(value, str) and value.startswith(_CSV_INJECTION_PREFIXES):
        return "'" + value
    return value


def _csv(rows: list[list]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerows([sanitize_cell(c) for c in row] for row in rows)
    return buf.getvalue()


def cohort_roster_csv(db: Session, *, tenant_id: UUID, cohort_id: UUID, now: datetime | None = None) -> tuple[str, str]:
    """Per-learner progress: completion, grade, last activity, open queue reasons."""
    fact = learning.cohort_roster_progress(db, tenant_id=tenant_id, cohort_id=cohort_id, now=now)
    cohort = fact.cohort
    rows: list[list] = [
        [
            "name",
            "email",
            "courses",
            "avg_completion_pct",
            "avg_grade_pct",
            "last_activity_at",
            "days_inactive",
            "open_queue_reasons",
        ]
    ]
    for row in fact.rows:
        rows.append(
            [
                f"{row.person.first_name} {row.person.last_name}".strip(),
                row.person.email,
                row.courses,
                row.avg_completion_pct,
                row.avg_grade_pct,
                row.last_activity_at.isoformat() if row.last_activity_at else "",
                row.days_inactive if row.days_inactive is not None else "",
                ";".join(row.open_queue_reasons),
            ]
        )
    safe = cohort.name.lower().replace(" ", "-")[:40]
    return f"roster-progress-{safe}.csv", _csv(rows)


def cohort_funnel_csv(db: Session, *, tenant_id: UUID, cohort_id: UUID, now: datetime | None = None) -> tuple[str, str]:
    """Completion funnel + work states, straight from the insights projection."""
    cohort = cohort_matrix(db, tenant_id=tenant_id, cohort_id=cohort_id)["cohort"]
    ov = insights.cohort_overview(db, tenant_id=tenant_id, cohort_id=cohort_id, now=now)
    funnel = ov["funnel"]
    rows = [
        ["metric", "value"],
        ["enrolled", funnel["enrolled"]],
        ["started", funnel["started"]],
        ["half_way", funnel["half"]],
        ["completed", funnel["completed"]],
        ["certified", funnel["certified"]],
        ["active_last_7d", ov["active"]],
        ["inactive", ov["inactive"]],
        ["submitted_activities", ov["submitted"]],
        ["overdue_activities", ov["overdue"]],
        ["not_attempted_activities", ov["not_attempted"]],
        ["avg_score_pct", ov["avg_score_pct"] if ov["avg_score_pct"] is not None else ""],
        ["events_this_week", ov["week_events"]],
        ["events_prior_4wk_avg", ov["prior_week_avg"]],
    ]
    safe = cohort.name.lower().replace(" ", "-")[:40]
    return f"funnel-{safe}.csv", _csv(rows)


def queue_summary_csv(db: Session, *, tenant_id: UUID, cohort_id: UUID | None = None) -> tuple[str, str]:
    """Open/acknowledged Success Queue entries, most severe first."""
    rows: list[list] = [
        [
            "name",
            "email",
            "cohort",
            "reason",
            "severity",
            "status",
            "detected_at",
            "recommended_action",
            "facts",
        ]
    ]
    for row in learning.queue_summary(db, tenant_id=tenant_id, cohort_id=cohort_id):
        entry, person, cohort_name = row.entry, row.person, row.cohort_name
        rows.append(
            [
                f"{person.first_name} {person.last_name}".strip(),
                person.email,
                cohort_name or "",
                entry.reason_kind,
                entry.severity,
                entry.status,
                entry.detected_at.isoformat(),
                entry.recommended_action,
                ";".join(f"{k}={v}" for k, v in sorted(cast(dict[str, JsonValue], entry.supporting_facts).items())),
            ]
        )
    return "success-queue.csv", _csv(rows)


def academy_summary_csv(db: Session, *, tenant_id: UUID, now: datetime | None = None) -> tuple[str, str]:
    """Academy-level comparison: one row per cohort."""
    comparison = insights.cohorts_comparison(db, tenant_id=tenant_id, now=now)
    rows: list[list] = [
        [
            "cohort",
            "learners",
            "active_last_7d",
            "completed",
            "completion_pct",
        ]
    ]
    for row in comparison:
        rows.append(
            [
                row["cohort"].name,
                row["learners"],
                row["active"],
                row["completed"],
                row["completion_pct"],
            ]
        )
    return "academy-summary.csv", _csv(rows)


def weekly_report_attachments(db: Session, *, tenant_id: UUID, cohort_id: UUID) -> list[tuple[str, bytes, str]]:
    """The weekly instructor email's attachments, built at delivery time."""
    out: list[tuple[str, bytes, str]] = []
    for name, text in (
        cohort_roster_csv(db, tenant_id=tenant_id, cohort_id=cohort_id),
        cohort_funnel_csv(db, tenant_id=tenant_id, cohort_id=cohort_id),
        queue_summary_csv(db, tenant_id=tenant_id, cohort_id=cohort_id),
    ):
        out.append((name, text.encode("utf-8"), "text/csv"))
    # Guard: a report window with zero learners still yields valid header-only CSVs.
    if not out:  # pragma: no cover - construction above is static
        raise ValueError("no report attachments produced")
    return out


def week_key(now: datetime | None = None) -> str:
    """Stable idempotency component: ISO year-week of the send."""
    now = now or datetime.now(UTC)
    year, week, _ = (now - timedelta(hours=0)).isocalendar()
    return f"{year}W{week:02d}"
