"""Authoritative ERP applicant-assessment registration and browser handoff."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.config import settings
from app.main import app
from app.models.admissions import Applicant
from app.models.assessment import Question, QuestionBank
from app.models.course import Course
from app.services import entrance_exam
from app.services.entrance_exam import set_academy_defaults
from app.services.erp_integration_security import sign_request

SECRET = "test-inbound-secret"
TOKEN_SECRET = "test-assessment-token-secret"
RETURN_URL = "https://erp.example/recruitment/applications/APP-42"
BASE_URL = "https://academy.example"


def _configure(monkeypatch):
    monkeypatch.setattr(settings, "erp_inbound_hmac_secret", SECRET)
    monkeypatch.setattr(settings, "erp_assessment_token_secret", TOKEN_SECRET)
    monkeypatch.setattr(settings, "erp_inbound_hmac_max_skew_seconds", 300)
    monkeypatch.setattr(settings, "erp_allowed_return_origins", "https://erp.example")
    monkeypatch.setattr(settings, "academy_public_base_url", BASE_URL)


def _bank(admin_session, tenant):
    course = Course(
        tenant_id=tenant.id,
        slug=f"erp-bank-{uuid4().hex[:8]}",
        title="ERP Applicant Assessment",
        discipline="recruitment",
        source_ref="erp-contract-test",
        version=1,
        status="published", listed=False,
    )
    admin_session.add(course)
    admin_session.flush()
    bank = QuestionBank(
        tenant_id=tenant.id,
        course_id=course.id,
        kind="final",
        version=1,
    )
    admin_session.add(bank)
    admin_session.flush()
    admin_session.add(
        Question(
            tenant_id=tenant.id,
            bank_id=bank.id,
            ext_id="aptitude-1",
            stem="Choose A",
            type="single",
            options=["A", "B"],
            correct=["A"],
            rubric_category="application",
            category="aptitude",
            explanation="",
            weight=1,
        )
    )
    set_academy_defaults(admin_session, tenant_id=tenant.id, bank_id=bank.id)
    admin_session.commit()
    return bank


def _body(*, external_ref="APP-42", bank_id=None, return_url=RETURN_URL):
    payload = {
        "external_ref": external_ref,
        "email": "ada@example.com",
        "first_name": "Ada",
        "last_name": "Lovelace",
        "return_url": return_url,
    }
    if bank_id is not None:
        payload["assessment_bank_id"] = str(bank_id)
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def _headers(body: bytes, *, timestamp: int | None = None, signature: str | None = None):
    ts = str(timestamp if timestamp is not None else int(datetime.now(UTC).timestamp()))
    signed = signature or sign_request(secret=SECRET, timestamp=ts, body=body)
    return {
        "Host": "alpha.localhost",
        "Content-Type": "application/json",
        "X-Webhook-Timestamp": ts,
        "X-Webhook-Signature-256": signed,
    }


def _post(client, body: bytes, **header_overrides):
    headers = _headers(body)
    headers.update(header_overrides)
    return client.post(
        "/integrations/erp/applicant-assessments",
        content=body,
        headers=headers,
    )


def test_registration_returns_exact_schema_and_working_url(
    app_client, admin_session, tenant_a, monkeypatch, caplog
):
    _configure(monkeypatch)
    caplog.set_level("INFO")
    bank = _bank(admin_session, tenant_a)
    body = _body(bank_id=bank.id)

    response = _post(app_client, body)

    assert response.status_code == 200
    assert set(response.json()) == {"assessment_url", "expires_at", "state"}
    assert response.json()["state"] == "not_started"
    raw_token = parse_qs(urlsplit(response.json()["assessment_url"]).query)["token"][0]
    assert raw_token not in caplog.text
    assert response.json()["assessment_url"] not in caplog.text
    assert "ada.com" not in caplog.text
    parsed = urlsplit(response.json()["assessment_url"])
    assert f"{parsed.scheme}://{parsed.netloc}" == BASE_URL
    assert parsed.path == "/apply/assessment"
    raw = parse_qs(parsed.query)["token"][0]
    applicant = entrance_exam.applicant_for_token(
        admin_session, tenant_id=tenant_a.id, raw=raw
    )
    assert applicant is not None
    assert applicant.external_ref == "APP-42"
    assert applicant.assessment_bank_id == bank.id
    assert applicant.assessment_return_url == RETURN_URL
    assert applicant.source == "erp_live"


def test_registration_retry_is_same_applicant_and_same_url(
    app_client, admin_session, tenant_a, monkeypatch
):
    _configure(monkeypatch)
    bank = _bank(admin_session, tenant_a)
    body = _body(bank_id=bank.id)

    first = _post(app_client, body)
    second = _post(app_client, body)

    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert admin_session.scalar(
        select(func.count())
        .select_from(Applicant)
        .where(Applicant.tenant_id == tenant_a.id)
        .where(Applicant.external_ref == "APP-42")
    ) == 1


def test_same_email_can_register_two_distinct_erp_applications(
    app_client, admin_session, tenant_a, monkeypatch
):
    _configure(monkeypatch)
    bank = _bank(admin_session, tenant_a)
    assert _post(app_client, _body(external_ref="APP-A", bank_id=bank.id)).status_code == 200
    assert _post(app_client, _body(external_ref="APP-B", bank_id=bank.id)).status_code == 200
    assert admin_session.scalar(
        select(func.count())
        .select_from(Applicant)
        .where(Applicant.tenant_id == tenant_a.id)
        .where(Applicant.email == "ada@example.com")
    ) == 2


def test_concurrent_duplicate_registration_returns_one_stable_url(
    admin_session, tenant_a, monkeypatch
):
    _configure(monkeypatch)
    bank = _bank(admin_session, tenant_a)
    body = _body(bank_id=bank.id)

    def send():
        with TestClient(app) as client:
            response = _post(client, body)
            return response.status_code, response.json()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: send(), range(2)))

    assert [status for status, _ in results] == [200, 200]
    assert results[0][1] == results[1][1]
    admin_session.rollback()
    assert admin_session.scalar(
        select(func.count())
        .select_from(Applicant)
        .where(Applicant.tenant_id == tenant_a.id)
        .where(Applicant.external_ref == "APP-42")
    ) == 1


def test_authentication_and_body_failures_are_precise(
    app_client, admin_session, tenant_a, monkeypatch
):
    _configure(monkeypatch)
    _bank(admin_session, tenant_a)
    body = _body()

    missing = app_client.post(
        "/integrations/erp/applicant-assessments",
        content=body,
        headers={"Host": "alpha.localhost", "Content-Type": "application/json"},
    )
    assert missing.status_code == 401
    assert missing.json()["error"]["code"] == "missing_authentication"

    invalid = _post(
        app_client,
        body,
        **{"X-Webhook-Signature-256": "sha256=" + "0" * 64},
    )
    assert invalid.status_code == 401
    assert invalid.json()["error"]["code"] == "invalid_signature"

    stale_ts = int(datetime.now(UTC).timestamp()) - 301
    stale_headers = _headers(body, timestamp=stale_ts)
    stale = app_client.post(
        "/integrations/erp/applicant-assessments",
        content=body,
        headers=stale_headers,
    )
    assert stale.status_code == 401
    assert stale.json()["error"]["code"] == "stale_timestamp"

    malformed_body = b"{"
    malformed = _post(app_client, malformed_body)
    assert malformed.status_code == 400
    assert malformed.json()["error"]["code"] == "malformed_json"


def test_unknown_bank_and_untrusted_return_url_are_rejected(
    app_client, admin_session, tenant_a, monkeypatch
):
    _configure(monkeypatch)
    _bank(admin_session, tenant_a)

    unknown = _post(app_client, _body(bank_id=uuid4()))
    assert unknown.status_code == 422
    assert unknown.json()["error"]["code"] == "unknown_assessment_bank"

    redirect = _post(
        app_client,
        _body(external_ref="APP-43", return_url="https://erp.example.attacker.test/x"),
    )
    assert redirect.status_code == 422
    assert redirect.json()["error"]["code"] == "invalid_return_url"


def test_omitted_bank_uses_tenant_default(
    app_client, admin_session, tenant_a, monkeypatch
):
    _configure(monkeypatch)
    bank = _bank(admin_session, tenant_a)
    response = _post(app_client, _body())
    assert response.status_code == 200
    applicant = admin_session.scalars(
        select(Applicant).where(Applicant.external_ref == "APP-42")
    ).first()
    assert applicant.assessment_bank_id == bank.id


def test_expired_link_is_closed_and_registration_does_not_create_an_attempt(
    app_client, admin_session, tenant_a, monkeypatch
):
    _configure(monkeypatch)
    _bank(admin_session, tenant_a)
    body = _body()
    response = _post(app_client, body)
    raw = parse_qs(urlsplit(response.json()["assessment_url"]).query)["token"][0]
    applicant = admin_session.scalars(
        select(Applicant).where(Applicant.external_ref == "APP-42")
    ).first()
    applicant.assessment_deadline = datetime.now(UTC) - timedelta(seconds=1)
    admin_session.commit()

    closed = app_client.get(
        f"/apply/assessment?token={raw}",
        headers={"Host": "alpha.localhost"},
    )
    assert closed.status_code == 200
    assert "assessment has closed" in closed.text

    app_client.cookies.clear()
    retry = _post(app_client, body)
    assert retry.status_code == 409
    assert retry.json()["error"]["code"] == "assessment_link_expired"
    assert admin_session.scalar(
        select(func.count())
        .select_from(Applicant)
        .where(Applicant.external_ref == "APP-42")
    ) == 1


def test_submission_redirects_without_putting_results_in_url(
    app_client, admin_session, tenant_a, monkeypatch
):
    _configure(monkeypatch)
    bank = _bank(admin_session, tenant_a)
    response = _post(app_client, _body(bank_id=bank.id))
    raw = parse_qs(urlsplit(response.json()["assessment_url"]).query)["token"][0]
    applicant = admin_session.scalars(
        select(Applicant).where(Applicant.external_ref == "APP-42")
    ).first()
    applicant.assessment_started_at = datetime.now(UTC) - timedelta(minutes=10)
    admin_session.commit()

    page = app_client.get(
        f"/apply/assessment?token={raw}",
        headers={"Host": "alpha.localhost"},
    )
    csrf = page.cookies.get("csrf_token") or app_client.cookies.get("csrf_token", "")
    submitted = app_client.post(
        "/apply/assessment",
        headers={
            "Host": "alpha.localhost",
            "HX-Request": "true",
            "x-csrf-token": csrf,
        },
        data={"token": raw, "aptitude-1": "A"},
        follow_redirects=False,
    )
    assert submitted.status_code == 204
    assert submitted.headers["HX-Redirect"] == RETURN_URL
    assert "score" not in submitted.headers["HX-Redirect"]
    admin_session.refresh(applicant)
    assert applicant.assessment_taken_at is not None
    assert applicant.assessment_result_version == 1
    assert applicant.assessment_erp_synced_at is None

    completed = app_client.get(
        f"/apply/assessment?token={raw}",
        headers={"Host": "alpha.localhost"},
    )
    assert "Already completed" in completed.text
