"""Historical opt-in canaries and genuine pinned-SDK capture canary for hosted CI.

No HTTP transport is used. Captured payloads are never included in assertion output.
"""
from __future__ import annotations

import builtins
import json
import logging
from importlib.metadata import version
from uuid import uuid4

import pytest
from fastapi import Request

from app.config import settings
from app.error_tracking import init_error_tracking, tag_request


@pytest.mark.parametrize("enabled,dsn", [(False, "https://key@telemetry.invalid/1"), (True, "")])
def test_disabled_without_opt_in(monkeypatch, enabled, dsn):
    monkeypatch.setattr(settings, "glitchtip_enabled", enabled)
    monkeypatch.setattr(settings, "glitchtip_dsn", dsn)
    assert init_error_tracking(server_name="dotmac-academy") is False
    tag_request("request")


def test_missing_optional_sdk(monkeypatch):
    original = builtins.__import__

    def missing(name, *args, **kwargs):
        if name == "sentry_sdk":
            raise ImportError("optional dependency absent")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(settings, "glitchtip_enabled", True)
    monkeypatch.setattr(settings, "glitchtip_dsn", "https://key@telemetry.invalid/1")
    monkeypatch.setattr(builtins, "__import__", missing)
    assert init_error_tracking(server_name="dotmac-academy") is False


def test_malformed_dsn_has_fixed_diagnostic(monkeypatch, caplog):
    marker = uuid4().hex
    monkeypatch.setattr(settings, "glitchtip_enabled", True)
    monkeypatch.setattr(settings, "glitchtip_dsn", marker)
    with caplog.at_level(logging.WARNING):
        result = init_error_tracking(server_name="dotmac-academy")
    safe = result is False and marker not in caplog.text
    assert safe, "malformed telemetry configuration leaked or did not fail closed"


def test_genuine_sdk_error_and_transaction_projection(monkeypatch):
    import sentry_sdk
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sentry_sdk.transport import Transport
    from sqlalchemy import create_engine, text
    from sqlalchemy.exc import OperationalError

    assert version("sentry-sdk") == "2.66.1"
    captured = []

    class CaptureTransport(Transport):
        def capture_envelope(self, envelope):
            captured.append(envelope)

    # Only replace the SDK transport constructor default; init, integrations,
    # scope handling, request extraction, serializer and callbacks are genuine.
    from sentry_sdk import client as client_module

    monkeypatch.setitem(client_module.DEFAULT_OPTIONS, "transport", CaptureTransport)
    markers = [f"privacy-{uuid4().hex}" for _ in range(9)]
    dsn_secret, token, password, body, sql, local_secret, parameter, attachment_bytes, attachment_filename = markers
    monkeypatch.setattr(settings, "glitchtip_enabled", True)
    monkeypatch.setattr(settings, "glitchtip_dsn", f"https://{dsn_secret}@telemetry.invalid/1")
    monkeypatch.setattr(settings, "glitchtip_traces_sample_rate", 1.0)
    assert init_error_tracking(server_name="dotmac-academy") is True
    sdk_client = sentry_sdk.get_client()
    assert sdk_client.options["send_default_pii"] is False
    assert sdk_client.options["max_request_body_size"] == "never"
    assert sdk_client.options["include_local_variables"] is False
    assert sdk_client.options["traces_sample_rate"] == 1.0
    assert isinstance(sdk_client.transport, CaptureTransport)
    assert callable(sdk_client.options["before_send"])
    assert sdk_client.options["before_send"] is sdk_client.options["before_send_transaction"]
    sql_failure_seen = []
    attachment_hint_seen = []
    attachment_hint_removed = []
    original_before_send = sdk_client.options["before_send"]

    def observe_boundary(event, hint):
        # Observe genuine SDK hint construction and the actual product callback;
        # never substitute event content or reconstruct the exception DTO.
        attachment_hint_seen.append((event.get("type", "event"), bool(hint.get("attachments"))))
        projected = original_before_send(event, hint)
        attachment_hint_removed.append("attachments" not in hint)
        return projected

    monkeypatch.setitem(sdk_client.options, "before_send", observe_boundary)
    monkeypatch.setitem(sdk_client.options, "before_send_transaction", observe_boundary)
    app = FastAPI()
    engine = create_engine("sqlite://")

    @app.post("/canary")
    async def canary(request: Request):
        planted_local = local_secret
        raw_body = await request.body()
        sentry_sdk.set_context("planted", {"password": password, "dsn": settings.glitchtip_dsn})
        sentry_sdk.set_extra("private", planted_local)
        sentry_sdk.set_tag("private", token)
        sentry_sdk.set_user({"id": password})
        sentry_sdk.add_breadcrumb(message=sql, data={"password": password})
        sentry_sdk.get_current_scope().add_attachment(
            bytes=attachment_bytes.encode(), filename=attachment_filename, add_to_transactions=True,
        )
        try:
            with engine.connect() as db:
                db.execute(
                    text(f"SELECT :private_value FROM academy_privacy_missing_table /* {sql} */"),  # noqa: S608 - generated canary SQL comment
                    {"private_value": parameter},
                )
        except OperationalError as error:
            # Verify the actual SQLAlchemy exception carries both planted SQL
            # and parameters before the genuine SDK serializes/captures it.
            # Only booleans reach asserts, so failures never print the payload.
            sql_failure_seen.append(
                sql in (error.statement or "")
                and error.params == (parameter,)
                and bool(raw_body)
            )
            sentry_sdk.capture_exception(error)
        return {"ok": True}

    try:
        with TestClient(app) as client:
            response = client.post("/canary", content=body, headers={"Authorization": f"Bearer {token}", "X-Password": password})
        assert response.status_code == 200
        # Explicit SDK transaction with planted name/span payload proves the
        # transaction callback independently of framework auto instrumentation.
        with sentry_sdk.isolation_scope() as scope:
            scope.add_attachment(
                bytes=attachment_bytes.encode(), filename=attachment_filename, add_to_transactions=True,
            )
            with sentry_sdk.start_transaction(name=body, op="http.server", sampled=True) as transaction:
                with transaction.start_child(op="db", description=sql) as span:
                    span.set_data("password", password)
        sentry_sdk.flush()
        events = [item.get_event() for envelope in captured for item in envelope.items if item.headers.get("type") == "event"]
        transactions = [item.get_transaction_event() for envelope in captured for item in envelope.items if item.headers.get("type") == "transaction"]
        assert events, "SDK failed to capture a genuine exception event"
        assert sql_failure_seen == [True], "database failure did not carry the planted statement and parameter"
        assert ("event", True) in attachment_hint_seen, "SDK failed to construct the actual error attachment hint"
        assert ("transaction", True) in attachment_hint_seen, "SDK failed to construct the actual transaction attachment hint"
        assert attachment_hint_removed and all(attachment_hint_removed), "product callback failed to remove attachment hints"
        assert all(item.headers.get("type") != "attachment" for envelope in captured for item in envelope.items)
        assert transactions, "SDK failed to capture a genuine transaction"
        # Whole envelopes include the SDK's trace envelope header. The product
        # projection strips dynamic sampling payload along with unknown context.
        outbound = b"\n".join(envelope.serialize() for envelope in captured)
        leaked = any(marker.encode() in outbound for marker in markers)
        assert not leaked, "SDK outbound privacy canary failed; inspect securely"
        assert any(value.get("type") == "OperationalError" for event in events for value in event.get("exception", {}).get("values", []))
        assert all(event.get("server_name") == "dotmac-academy" for event in events + transactions)
        assert all(transaction["transaction"] == "academy.request" for transaction in transactions)
        # Ensure the serializer produced real JSON items, not hand-built stand-ins.
        assert all(isinstance(json.loads(item.get_bytes()), dict) for envelope in captured for item in envelope.items if item.headers.get("type") in ("event", "transaction"))
    finally:
        sdk_client.close()
        engine.dispose()
