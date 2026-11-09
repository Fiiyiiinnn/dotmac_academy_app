# Ported at new-composition genesis (web layer wave).
# app/web/learn.py
from __future__ import annotations

import re
from collections.abc import Sequence
from html.parser import HTMLParser
from typing import Any, cast
from uuid import UUID

from dotmac_learning import (
    bank_questions,
    course_structure,
    get_activity,
    get_completion,
    get_course,
    progress_overview,
    questions_for,
)
from dotmac_learning.curriculum_models import Chapter
from dotmac_learning.models import Activity, Course, Score, Submission
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import JsonValue
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_tenant
from app.services import announcements as ann_svc
from app.services import (
    bank_authoring,
    exam_engine,
    insights,
    learner_dashboard,
    learning_events,
    learning_policy,
    reading_progress,
)
from app.services import catalog as catalog_service
from app.services.assessment import attempts_used, best_scores_for, reveal_feedback, submit_activity
from app.services.attempts import close_open_attempt, get_open_attempt, open_or_create_attempt
from app.services.certificates import issue_certificate, render_certificate_pdf
from app.services.entitlements import require_course_open, visible_course_ids
from app.services.facilitator_access import facilitator_access
from app.services.identity import AcademyCurrentAccount
from app.services.pacing import require_activity_readable, require_activity_submittable
from app.services.settings_store import effective
from app.services.web_auth import optional_web_user, require_web_user
from app.web.templating import templates

router = APIRouter(dependencies=[Depends(require_tenant)])


class _HeadingExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.headings: list[dict[str, str]] = []
        self._current_tag: str | None = None
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"h2", "h3"}:
            self._current_tag = tag
            self._parts = []

    def handle_data(self, data: str) -> None:
        if self._current_tag:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == self._current_tag:
            title = " ".join("".join(self._parts).split())
            if title:
                self.headings.append({"level": tag, "title": title})
            self._current_tag = None
            self._parts = []


def _sitting_view(person, questions: Sequence) -> list[dict]:
    """Legacy-strip question facts for the pre-versioning fallback only.

    Versioned banks render through Learning ``sitting_questions`` via
    ``bank_authoring.activity_sitting``; this remains for banks with no
    published version yet. Options are ordered per learner (a fixed
    order leaks "the answer is the third one"); deterministic in
    (learner, question) and safe because answers post by option text.
    """
    return [
        {
            "ext_id": q.ext_id,
            "stem": q.stem,
            "type": q.type,
            "options": exam_engine.present_options(person, q),
        }
        for q in questions
    ]


def _slugify_heading(value: str) -> str:
    return (
        re.sub(r"-+", "-", re.sub(r"\s+", "-", re.sub(r"[^\w\s-]", "", (value or "").lower().strip())))[:64]
        or "section"
    )


_GROUPED_INTRO_HEADINGS = {"why-this-matters", "catch-up-sidebar"}


def _grouped_heading_key(value: str) -> str:
    return _slugify_heading(value).casefold()


def _chapter_subtopics(body_html: str) -> list[dict[str, str]]:
    parser = _HeadingExtractor()
    parser.feed(body_html or "")
    used: set[str] = set()
    subtopics: list[dict[str, str]] = []
    for heading in parser.headings:
        if _grouped_heading_key(heading["title"]) in _GROUPED_INTRO_HEADINGS:
            continue
        base = _slugify_heading(heading["title"])
        slug = base
        counter = 2
        while slug in used:
            slug = f"{base}-{counter}"
            counter += 1
        used.add(slug)
        subtopics.append({**heading, "slug": slug})
    return subtopics


def _enrolled_courses(db: Session, tid: UUID, person_id: UUID) -> list[Course]:
    """Courses the person can study, resolved Enrollment -> CourseOffering -> Course.

    Access requires an explicit CourseOffering linking the person's cohort to the
    course; sharing a ``discipline`` string no longer grants access.
    """
    course_ids = visible_course_ids(db, tenant_id=tid, person_id=person_id)
    if not course_ids:
        return []
    return list(
        db.scalars(
            select(Course).where(Course.tenant_id == tid).where(Course.id.in_(course_ids)).order_by(Course.title)
        ).all()
    )


@router.get("/", response_class=HTMLResponse, name="home")
@router.get("/learn", response_class=HTMLResponse, name="learn_home")
def home(
    request: Request,
    person: AcademyCurrentAccount | None = Depends(optional_web_user),
    db: Session = Depends(get_db),
):
    """Signed-in: Learning workspace. Anonymous root: public landing page.

    The landing (and /courses) is the academy's public web presence — the
    catalog projection replaces the hand-maintained marketing-site copy. Pure
    instructors still land in Teaching from ``/`` after login, while ``/learn``
    is their explicit route into their own enrolled learning.
    """
    tenant = require_tenant(request)
    if person is None:
        listed = catalog_service.public_catalog(db, tenant_id=tenant.id)
        mgmt = [i for i in listed if i["course"].discipline == "management"]
        tech = [i for i in listed if i["course"].discipline != "management"]
        return templates.TemplateResponse(
            request,
            "public/landing.html",
            {
                "tech_count": len(tech),
                "mgmt_count": len(mgmt),
                "chapter_count": sum(i["chapter_count"] for i in listed),
                "management_contact_email": effective(db).management_inquiry_recipient,
            },
        )
    access = facilitator_access(db, tenant_id=tenant.id, person_id=person.id)
    if request.url.path == "/" and access.can_teach and not access.is_admin:
        return RedirectResponse("/instructor", status_code=303)

    # Dashboard projection (roadmap P1a): the service owns state derivation.
    # learner_home fans out once and returns cards + cross-course continue.
    dashboard = learner_dashboard.learner_home(db, tenant_id=tenant.id, person_id=person.id)
    state_filter = request.query_params.get("filter", "")
    cards = dashboard["cards"]
    if state_filter in dashboard["filters"]:
        cards = [c for c in cards if c["state"] == state_filter]

    # Continue: the actual last meaningful activity, resolved server-side.
    continue_to = dashboard["continue_to"]

    # Recent results: the person's latest few scores (title + pass/fail + %).
    recent_rows = db.execute(
        select(Score, Activity.title)
        .join(
            Submission,
            (Submission.id == Score.submission_id) & (Submission.tenant_id == Score.tenant_id),
        )
        .join(
            Activity,
            (Activity.id == Submission.activity_id) & (Activity.tenant_id == Submission.tenant_id),
        )
        .where(Score.tenant_id == tenant.id)
        .where(Submission.person_id == person.id)
        .order_by(Score.created_at.desc())
        .limit(6)
    ).all()
    recent = [{"title": title, "passed": s.passed, "pct": round(100 * s.fraction)} for s, title in recent_rows]

    latest_announcements = ann_svc.for_person(db, tenant_id=tenant.id, person_id=person.id, limit=3)

    return templates.TemplateResponse(
        request,
        "learn/home.html",
        {
            "request": request,
            "person": person,
            "cards": cards,
            "counts": dashboard["counts"],
            "total_cards": len(dashboard["cards"]),
            "state_filter": state_filter,
            "continue_to": continue_to,
            "recent": recent,
            "announcements": latest_announcements,
        },
    )


@router.get("/courses/{slug}/chapters/{n}", response_class=HTMLResponse)
def chapter(
    slug: str,
    n: int,
    request: Request,
    person: AcademyCurrentAccount = Depends(require_web_user),
    db: Session = Depends(get_db),
):
    tenant = require_tenant(request)
    course = db.scalars(select(Course).where(Course.tenant_id == tenant.id).where(Course.slug == slug)).first()
    if course is None:
        raise HTTPException(status_code=404)
    require_course_open(db, tenant_id=tenant.id, person_id=person.id, course_id=course.id)
    ch = db.scalars(
        select(Chapter)
        .where(Chapter.tenant_id == tenant.id)
        .where(Chapter.course_id == course.id)
        .where(Chapter.number == n)
    ).first()
    if ch is None:
        raise HTTPException(status_code=404)
    learning_events.emit(
        db,
        tenant_id=tenant.id,
        person_id=person.id,
        kind="course_viewed",
        course_id=course.id,
        subject_id=course.id,
    )
    learning_events.emit(
        db,
        tenant_id=tenant.id,
        person_id=person.id,
        kind="chapter_viewed",
        course_id=course.id,
        subject_id=ch.id,
    )
    activities = list(
        db.scalars(
            select(Activity)
            .where(Activity.tenant_id == tenant.id)
            .where(Activity.course_id == course.id)
            .where(Activity.chapter_number == n)
        ).all()
    )
    activities.sort(key=lambda a: (0 if a.type == "mcq_test" else 1 if a.type == "lab" else 2, a.title))
    act = next((a for a in activities if a.type == "mcq_test"), activities[0] if activities else None)
    total_chapters = int(
        db.scalar(
            select(func.count())
            .select_from(Chapter)
            .where(Chapter.tenant_id == tenant.id)
            .where(Chapter.course_id == course.id)
        )
        or 0
    )
    chapters = db.scalars(
        select(Chapter)
        .where(Chapter.tenant_id == tenant.id)
        .where(Chapter.course_id == course.id)
        .order_by(Chapter.number)
    ).all()
    chapter_ids = [chapter.id for chapter in chapters]
    # Server-rendered subtopic state: the sidebar must arrive already unlocked
    # for work the learner has done, on whatever device they opened it.
    subtopics_done = reading_progress.completed_slugs_by_chapter(
        db, tenant_id=tenant.id, person_id=person.id, chapter_ids=chapter_ids
    )
    chapter_modules = [
        {
            "chapter": course_chapter,
            "subtopics": _chapter_subtopics(course_chapter.body_html),
            "done_slugs": subtopics_done.get(course_chapter.id, set()),
        }
        for course_chapter in chapters
    ]
    completed_chapter_ids = reading_progress.completed_chapter_ids(
        db, tenant_id=tenant.id, person_id=person.id, chapter_ids=chapter_ids
    )
    prev_ch = db.scalars(
        select(Chapter)
        .where(Chapter.tenant_id == tenant.id)
        .where(Chapter.course_id == course.id)
        .where(Chapter.number < n)
        .order_by(Chapter.number.desc())
    ).first()
    next_ch = db.scalars(
        select(Chapter)
        .where(Chapter.tenant_id == tenant.id)
        .where(Chapter.course_id == course.id)
        .where(Chapter.number > n)
        .order_by(Chapter.number)
    ).first()
    words = len(re.sub(r"<[^>]+>", " ", ch.body_html or "").split())
    reading_minutes = learning_policy.reading_minutes(db, words)
    # Preserve the separate current-chapter observation at its original point.
    # Under READ COMMITTED it can see a completion newer than the sidebar batch.
    completed = ch.id in reading_progress.completed_chapter_ids(
        db, tenant_id=tenant.id, person_id=person.id, chapter_ids=[ch.id]
    )
    best = best_scores_for(db, tenant_id=tenant.id, person_id=person.id, course_id=course.id)
    activity_items: list[dict[str, Any]] = []
    for item in activities:
        score = best.get(item.id)
        used = attempts_used(db, tenant_id=tenant.id, person_id=person.id, activity_id=item.id)
        activity_items.append(
            {
                "activity": item,
                "href": f"/labs/{item.id}" if item.type == "lab" else f"/activities/{item.id}",
                "attempts": used,
                "passed": bool(score.passed) if score is not None else False,
                "pct": round(100 * score.fraction) if score is not None else None,
            }
        )
    activity_taken = False
    if act is not None:
        primary_item = next((item for item in activity_items if item["activity"].id == act.id), None)
        activity_taken = bool(primary_item and primary_item["attempts"] > 0)
    this_chapter_subtopics = _chapter_subtopics(ch.body_html)
    this_chapter_done = subtopics_done.get(ch.id, set())
    # The gate the activity links honour, decided here rather than in the
    # browser: every subtopic of THIS chapter recorded as read.
    chapter_subtopics_complete = all(s["slug"] in this_chapter_done for s in this_chapter_subtopics)
    return templates.TemplateResponse(
        request,
        "chapter.html",
        {
            "request": request,
            "course": course,
            "chapter": ch,
            "activity": act,
            "activity_items": activity_items,
            "total_chapters": total_chapters,
            "previous_chapter": prev_ch,
            "next_chapter": next_ch,
            "reading_minutes": reading_minutes,
            "completed": bool(completed),
            "activity_taken": activity_taken,
            "chapters": chapters,
            "chapter_modules": chapter_modules,
            "completed_chapter_ids": completed_chapter_ids,
            "completed_chapters": len(completed_chapter_ids),
            "chapter_subtopics_complete": chapter_subtopics_complete,
        },
    )


@router.post("/courses/{slug}/chapters/{n}/subtopics/{subtopic_slug}/complete")
def subtopic_complete(
    slug: str,
    n: int,
    subtopic_slug: str,
    request: Request,
    person: AcademyCurrentAccount = Depends(require_web_user),
    db: Session = Depends(get_db),
):
    """Record one completed subtopic for the current learner.

    Gated like its GET sibling: without ``require_course_open`` a learner could
    write progress for a course they cannot access.
    """
    tenant = require_tenant(request)
    course = db.scalars(select(Course).where(Course.tenant_id == tenant.id).where(Course.slug == slug)).first()
    if course is None:
        raise HTTPException(status_code=404)
    require_course_open(db, tenant_id=tenant.id, person_id=person.id, course_id=course.id)
    ch = db.scalars(
        select(Chapter)
        .where(Chapter.tenant_id == tenant.id)
        .where(Chapter.course_id == course.id)
        .where(Chapter.number == n)
    ).first()
    if ch is None:
        raise HTTPException(status_code=404)
    # Only slugs this chapter actually has: the slug is content-derived and
    # arrives from the client, so it is validated against the real headings
    # rather than stored on trust.
    known = {s["slug"] for s in _chapter_subtopics(ch.body_html)}
    if subtopic_slug not in known:
        raise HTTPException(status_code=404)
    reading_progress.mark_complete(
        db, tenant_id=tenant.id, person_id=person.id, chapter_id=ch.id, subtopic_slug=subtopic_slug
    )
    done = reading_progress.completed_slugs(db, tenant_id=tenant.id, person_id=person.id, chapter_id=ch.id)
    return JSONResponse({"completed": sorted(done), "chapter_complete": known.issubset(done)})


@router.post("/courses/{slug}/chapters/{n}/complete", response_class=HTMLResponse)
def chapter_complete(
    slug: str,
    n: int,
    request: Request,
    person: AcademyCurrentAccount = Depends(require_web_user),
    db: Session = Depends(get_db),
):
    """Toggle a chapter's read-completion for the current learner (htmx)."""
    tenant = require_tenant(request)
    course = db.scalars(select(Course).where(Course.tenant_id == tenant.id).where(Course.slug == slug)).first()
    if course is None:
        raise HTTPException(status_code=404)
    # Gate this state-changing route like its GET sibling (:209): without it a
    # learner can mark chapters complete in a course they can't access, writing
    # ChapterRead rows and chapter_completed ledger events that then feed
    # analytics, inactivity, and the Success Queue.
    require_course_open(db, tenant_id=tenant.id, person_id=person.id, course_id=course.id)
    ch = db.scalars(
        select(Chapter)
        .where(Chapter.tenant_id == tenant.id)
        .where(Chapter.course_id == course.id)
        .where(Chapter.number == n)
    ).first()
    if ch is None:
        raise HTTPException(status_code=404)
    done = reading_progress.toggle_chapter_read(
        db, tenant_id=tenant.id, person_id=person.id, course_id=course.id, chapter_id=ch.id
    )
    return templates.TemplateResponse(
        request,
        "_mark_complete.html",
        {"request": request, "course": course, "chapter": ch, "completed": done},
    )


@router.get("/activities/{activity_id}", response_class=HTMLResponse)
def activity(
    activity_id: UUID,
    request: Request,
    person: AcademyCurrentAccount = Depends(require_web_user),
    db: Session = Depends(get_db),
):
    tenant = require_tenant(request)
    act = db.scalars(select(Activity).where(Activity.id == activity_id).where(Activity.tenant_id == tenant.id)).first()
    if act is None:
        raise HTTPException(status_code=404)
    require_course_open(db, tenant_id=tenant.id, person_id=person.id, course_id=act.course_id)
    require_activity_readable(db, tenant_id=tenant.id, person_id=person.id, course_id=act.course_id, activity_id=act.id)
    if act.type == "lab":
        # A lab has no question bank; rendering it here produced an empty form
        # whose submission scored 0/0 and burned an attempt. Send the learner to
        # the workspace that can actually grade it.
        return RedirectResponse(f"/labs/{act.id}", status_code=303)
    # Key-free sitting projection from Learning (options only; the
    # SittingQuestion type cannot carry correct/explanation). The draw
    # list comes from the same projection, and the attempt pins the
    # served bank version so a mid-sitting publish cannot move the paper.
    only_ext_ids: list[str] | None = None
    pinned_version_id = None
    if act.question_count is not None:
        # Random pool: an open attempt already fixed its subset+order and
        # its bank-version pin. Re-render from that pin (mirroring the
        # submit path's attempt_pin) so the served paper equals the
        # graded paper after a publish/repoint; activity resolution is
        # used only to create a new draw. A retired attempt pin refusing
        # inside Learning remains the already-declared B2 behavior and
        # is not worked around here.
        existing = get_open_attempt(db, tenant_id=tenant.id, person_id=person.id, activity_id=act.id)
        if existing is not None:
            only_ext_ids = list(cast("list[str]", existing.question_ext_ids))
            pinned_version_id = existing.bank_version_id
        else:
            version_id, all_ext_ids = bank_authoring.activity_ext_ids(
                db, tenant_id=tenant.id, activity=act, sitter_id=person.id
            )
            attempt = open_or_create_attempt(
                db,
                tenant_id=tenant.id,
                person_id=person.id,
                activity_id=act.id,
                all_ext_ids=all_ext_ids,
                count=act.question_count,
                bank_version_id=version_id,
            )
            only_ext_ids = list(cast("list[str]", attempt.question_ext_ids))
            pinned_version_id = attempt.bank_version_id if attempt.bank_version_id is not None else version_id
    _version_id, questions = bank_authoring.activity_sitting(
        db,
        tenant_id=tenant.id,
        activity=act,
        sitter=person,
        only_ext_ids=only_ext_ids,
        pinned_version_id=pinned_version_id,
    )
    return templates.TemplateResponse(
        request,
        "activity.html",
        {"request": request, "activity": act, "questions": questions},
    )


@router.post("/activities/{activity_id}/submit", response_class=HTMLResponse)
async def submit(
    activity_id: UUID,
    request: Request,
    person: AcademyCurrentAccount = Depends(require_web_user),
    db: Session = Depends(get_db),
):
    tenant = require_tenant(request)
    act = get_activity(db, tenant_id=tenant.id, activity_id=activity_id)
    if act is None:
        raise HTTPException(status_code=404)
    require_course_open(db, tenant_id=tenant.id, person_id=person.id, course_id=act.course_id)
    require_activity_submittable(
        db, tenant_id=tenant.id, person_id=person.id, course_id=act.course_id, activity_id=act.id
    )
    form = await request.form()
    # Random pool: grade exactly the subset this attempt was shown. Read
    # the attempt WITHOUT closing it: Learning grades against the open
    # attempt's bank-version pin, and closing first would hide that pin.
    # The close follows a successful submit in the same transaction.
    only_ext_ids: list | None = None
    attempt_pin = act.bank_version_id
    if act.question_count is not None:
        attempt = get_open_attempt(db, tenant_id=tenant.id, person_id=person.id, activity_id=act.id)
        only_ext_ids = list(cast("list[str]", attempt.question_ext_ids)) if attempt is not None else []
        if attempt is not None and attempt.bank_version_id is not None:
            attempt_pin = attempt.bank_version_id
    qs = questions_for(
        db, tenant_id=tenant.id, bank_id=act.bank_id, only_ext_ids=only_ext_ids, bank_version_id=attempt_pin
    )
    answers = {q["ext_id"]: form.getlist(q["ext_id"]) for q in qs}
    score = submit_activity(
        db,
        tenant_id=tenant.id,
        person_id=person.id,
        activity=act,
        answers=cast("dict[str, JsonValue]", answers),
        only_ext_ids=only_ext_ids,
    )
    if act.question_count is not None:
        close_open_attempt(db, tenant_id=tenant.id, person_id=person.id, activity_id=act.id)
    if score is None:
        # Manual-grading activity: submission is queued for the instructor.
        return HTMLResponse(
            '<div class="submit-pending" role="status">Submitted — your instructor '
            "will grade this and your result will appear once it's marked.</div>"
        )
    # get_db handles the final db.commit(); calling it here would expire ORM
    # objects and clear the SET LOCAL tenant config. Render from all bank questions
    # so retakes can display the originally recorded score, including random pools.
    display_questions = bank_questions(db, tenant_id=tenant.id, bank_id=act.bank_id, bank_version_id=attempt_pin)
    by_id = {q.ext_id: q for q in display_questions}
    course = get_course(db, tenant_id=tenant.id, course_id=act.course_id)
    next_chapter = None
    course_completion = None
    if course is not None and act.chapter_number is not None:
        structure = course_structure(db, tenant_id=tenant.id, person_id=person.id, course_id=course.id)
        next_chapter = min(
            (
                entry.chapter
                for part in structure.parts
                for entry in part.chapters
                if entry.chapter.number > act.chapter_number
            ),
            key=lambda chapter: chapter.number,
            default=None,
        )
    if course is not None:
        course_completion = get_completion(db, tenant_id=tenant.id, person_id=person.id, course_id=course.id)
    return templates.TemplateResponse(
        request,
        "_activity_result.html",
        {
            "request": request,
            "score": score,
            "questions": by_id,
            "course": course,
            "next_chapter": next_chapter,
            "course_completion": course_completion,
            # Withhold the answer key on graded/exam assessments until the learner
            # has passed or used all their attempts (see assessment.reveal_feedback).
            "reveal": reveal_feedback(
                act,
                terms=learning_policy.feedback_reveal_terms(db),
                passed=score.passed,
                attempts_used=attempts_used(db, tenant_id=tenant.id, person_id=person.id, activity_id=act.id),
            ),
        },
    )


@router.get("/progress", response_class=HTMLResponse)
def progress(
    request: Request,
    person: AcademyCurrentAccount = Depends(require_web_user),
    db: Session = Depends(get_db),
):
    tenant = require_tenant(request)
    overview = progress_overview(
        db,
        tenant_id=tenant.id,
        person_id=person.id,
        progress_trend=learning_policy.progress_trend_terms(db),
        feedback_terms=learning_policy.feedback_reveal_terms(db),
    )
    activity = insights.learner_activity(db, tenant_id=tenant.id, person_id=person.id)
    return templates.TemplateResponse(
        request,
        "progress.html",
        {"request": request, "overview": overview, "activity": activity},
    )


@router.get("/certificates/{course_id}")
def certificate(
    course_id: UUID,
    request: Request,
    person: AcademyCurrentAccount = Depends(require_web_user),
    db: Session = Depends(get_db),
) -> Response:
    """Download the learner's PDF certificate for a completed course (403 if not)."""
    tenant = require_tenant(request)
    course = get_course(db, tenant_id=tenant.id, course_id=course_id)
    if course is None:
        raise HTTPException(status_code=404)
    completion = get_completion(db, tenant_id=tenant.id, person_id=person.id, course_id=course_id)
    if completion is None or completion.status != "completed":
        raise HTTPException(status_code=403)
    cert = issue_certificate(db, tenant_id=tenant.id, person_id=person.id, course_id=course_id)
    pdf = render_certificate_pdf(
        recipient_name=f"{person.first_name} {person.last_name}".strip(),
        course_title=course.title,
        serial=cert.serial,
        issued_at=cert.issued_at,
    )
    filename = f"certificate-{course.slug}-{cert.serial}.pdf"
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
