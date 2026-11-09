"""Bookmarks & personal notes web router (roadmap P1 item 11).

``/bookmarks`` is the learner's collection page; the chapter-scoped partials
are lazily loaded into the chapter page via htmx, so the chapter route itself
stays untouched. Thin adapters: all writes live in ``services.bookmarks``.
"""

# Ported at new-composition genesis (web layer wave).

from __future__ import annotations

from dotmac_learning import ChapterFact, CourseFact
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_tenant
from app.services import bookmarks as svc
from app.services import catalog
from app.services.entitlements import require_course_access
from app.services.identity import AcademyCurrentAccount
from app.services.web_auth import require_web_user
from app.web.templating import templates

router = APIRouter(dependencies=[Depends(require_tenant)])


def _resolve(db: Session, tenant_id, slug: str, n: int, person_id) -> tuple[CourseFact, ChapterFact]:
    course = catalog.catalog_course_by_slug(db, tenant_id=tenant_id, slug=slug)
    if course is None:
        raise HTTPException(status_code=404)
    require_course_access(db, tenant_id=tenant_id, person_id=person_id, course_id=course.id)
    chapter = catalog.catalog_chapter_by_number(db, tenant_id=tenant_id, course_id=course.id, number=n)
    if chapter is None:
        raise HTTPException(status_code=404)
    return course, chapter


def _toggle_partial(request: Request, course: CourseFact, chapter: ChapterFact, bookmarked: bool):
    return templates.TemplateResponse(
        request,
        "bookmarks/_toggle.html",
        {"course": course, "chapter": chapter, "bookmarked": bookmarked},
    )


def _note_partial(request: Request, course: CourseFact, chapter: ChapterFact, note, saved: bool = False):
    return templates.TemplateResponse(
        request,
        "bookmarks/_note.html",
        {"course": course, "chapter": chapter, "note": note, "saved": saved},
    )


@router.get("/bookmarks", response_class=HTMLResponse)
def bookmarks_index(
    request: Request,
    person: AcademyCurrentAccount = Depends(require_web_user),
    db: Session = Depends(get_db),
):
    tenant = require_tenant(request)
    groups = svc.list_for_person(db, tenant_id=tenant.id, person_id=person.id)
    return templates.TemplateResponse(request, "bookmarks/index.html", {"groups": groups})


@router.get("/bookmarks/chapters/{slug}/{n}/toggle", response_class=HTMLResponse)
def bookmark_toggle_ui(
    slug: str,
    n: int,
    request: Request,
    person: AcademyCurrentAccount = Depends(require_web_user),
    db: Session = Depends(get_db),
):
    tenant = require_tenant(request)
    course, chapter = _resolve(db, tenant.id, slug, n, person.id)
    state = svc.get_state(db, tenant_id=tenant.id, person_id=person.id, chapter_id=chapter.id)
    return _toggle_partial(request, course, chapter, state["bookmarked"])


@router.post("/bookmarks/chapters/{slug}/{n}/toggle", response_class=HTMLResponse)
def bookmark_toggle(
    slug: str,
    n: int,
    request: Request,
    person: AcademyCurrentAccount = Depends(require_web_user),
    db: Session = Depends(get_db),
):
    tenant = require_tenant(request)
    course, chapter = _resolve(db, tenant.id, slug, n, person.id)
    bookmarked = svc.toggle_bookmark(
        db,
        tenant_id=tenant.id,
        person_id=person.id,
        course_id=course.id,
        chapter_id=chapter.id,
    )
    return _toggle_partial(request, course, chapter, bookmarked)


@router.get("/bookmarks/chapters/{slug}/{n}/note", response_class=HTMLResponse)
def note_ui(
    slug: str,
    n: int,
    request: Request,
    person: AcademyCurrentAccount = Depends(require_web_user),
    db: Session = Depends(get_db),
):
    tenant = require_tenant(request)
    course, chapter = _resolve(db, tenant.id, slug, n, person.id)
    state = svc.get_state(db, tenant_id=tenant.id, person_id=person.id, chapter_id=chapter.id)
    return _note_partial(request, course, chapter, state["note"])


@router.post("/bookmarks/chapters/{slug}/{n}/note", response_class=HTMLResponse)
def note_save(
    slug: str,
    n: int,
    request: Request,
    body: str = Form(""),
    person: AcademyCurrentAccount = Depends(require_web_user),
    db: Session = Depends(get_db),
):
    tenant = require_tenant(request)
    course, chapter = _resolve(db, tenant.id, slug, n, person.id)
    note = svc.save_note(
        db,
        tenant_id=tenant.id,
        person_id=person.id,
        course_id=course.id,
        chapter_id=chapter.id,
        body=body,
    )
    return _note_partial(request, course, chapter, note, saved=True)
