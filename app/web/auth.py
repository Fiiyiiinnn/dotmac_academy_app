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

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import PlainTextResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_tenant
from app.services import oidc_login, web_auth
from app.services.facilitator_access import facilitator_access
from app.web.responses import hx_redirect
from app.web.templating import templates

router = APIRouter(dependencies=[Depends(require_tenant)])


@router.get("/login")
def login_form(request: Request, oidc_error: str = Query(default="")):
    error = "Sign-in was not completed. Try again or use your password." if oidc_error else None
    return templates.TemplateResponse(
        request,
        "login.html",
        {"request": request, "error": error, "oidc_enabled": oidc_login.oidc_enabled()},
    )


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


@router.post("/login/oidc")
def oidc_login_start(request: Request, db: Session = Depends(get_db)):
    """Start the Keycloak ceremony (staff pilot). A POST: it writes state.

    The password form stays; this only adds the federated front door for
    explicitly bound staff. The state cookie proves the callback returns
    to the browser that started the ceremony.
    """
    tenant = require_tenant(request)
    try:
        started = oidc_login.begin_login(db, tenant=tenant)
    except oidc_login.OidcNotConfigured:
        return PlainTextResponse("Federated sign-in is not configured.", status_code=503)
    except oidc_login.OidcLoginRefused:
        return PlainTextResponse("Sign-in could not be started.", status_code=502)
    resp = hx_redirect(request, started.url, hx_status=200)
    resp.set_cookie(
        oidc_login.STATE_COOKIE,
        started.state,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        path="/login/oidc",
        expires=started.expires_at,
    )
    return resp


@router.get("/login/oidc/callback")
def oidc_login_callback(
    request: Request,
    code: str = Query(default=""),
    state: str = Query(default=""),
    error: str = Query(default=""),
    db: Session = Depends(get_db),
):
    tenant = require_tenant(request)

    def _refused() -> RedirectResponse:
        resp = RedirectResponse("/login?oidc_error=1", status_code=303)
        resp.delete_cookie(oidc_login.STATE_COOKIE, path="/login/oidc")
        return resp

    if error or not code or not state:
        return _refused()
    try:
        completed = oidc_login.complete_login(
            db,
            tenant=tenant,
            code=code,
            state=state,
            stored_state=request.cookies.get(oidc_login.STATE_COOKIE),
        )
    except (oidc_login.OidcLoginRefused, oidc_login.OidcNotConfigured):
        return _refused()
    access = facilitator_access(db, tenant_id=tenant.id, person_id=completed.account.id)
    redirect_to = "/instructor" if access.can_teach and not access.is_admin else "/"
    resp = RedirectResponse(redirect_to, status_code=303)
    resp.delete_cookie(oidc_login.STATE_COOKIE, path="/login/oidc")
    resp.set_cookie(
        web_auth.COOKIE,
        completed.session_token,
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
