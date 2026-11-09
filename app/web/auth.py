"""Web auth routes: login form, login POST, logout, and account probe.

Routes
------
GET  /login   — render login form
POST /login   — validate credentials, set HttpOnly session cookie, 303 → /
POST /logout  — revoke session, clear cookie, 303 → /login

(The real /account page lives in app/web/account.py — Task 7.)
"""

# Ported at new-composition genesis (web layer wave).

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_tenant
from app.services import web_auth
from app.services.facilitator_access import facilitator_access
from app.web.responses import hx_redirect
from app.web.templating import templates

router = APIRouter(dependencies=[Depends(require_tenant)])


@router.get("/login")
def login_form(request: Request):
    return templates.TemplateResponse(request, "login.html", {"request": request, "error": None})


@router.post("/login")
def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    tenant = require_tenant(request)
    result = web_auth.sign_in(db, tenant_id=tenant.id, email=email, password=password)
    hx = request.headers.get("HX-Request")
    if result is None:
        if hx:
            return PlainTextResponse("Invalid credentials", status_code=200)
        return templates.TemplateResponse(
            request,
            "login.html",
            {"request": request, "error": "Invalid credentials"},
            status_code=401,
        )
    access = facilitator_access(db, tenant_id=tenant.id, person_id=result.account.id)
    redirect_to = "/instructor" if access.can_teach and not access.is_admin else "/"
    # No db.commit() here — get_db commits at request end (and a mid-route commit
    # would clear the transaction-scoped app.current_tenant GUC).
    resp = hx_redirect(request, redirect_to, hx_status=204)
    resp.set_cookie(
        web_auth.COOKIE,
        result.session_token,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
    )
    return resp


@router.post("/logout")
def logout(request: Request, db: Session = Depends(get_db)):
    tenant = require_tenant(request)
    web_auth.revoke_session(db, tenant.id, request.cookies.get(web_auth.COOKIE))
    # No db.commit() here — get_db commits at request end.
    resp = hx_redirect(request, "/login", hx_status=204)
    resp.delete_cookie(web_auth.COOKIE)
    return resp
