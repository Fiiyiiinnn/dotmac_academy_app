"""Console use through the proxy keeps the lab alive (heartbeat, not lifecycle)."""

from __future__ import annotations

import time

import pytest
from fastapi.responses import Response

from app.models.lab import LabConsoleActivity
from app.services import lab_console_activity as lca
from legacy_probe.web.test_console_ws import H, _login, _make_person, _seed_instance, _ws_url


@pytest.fixture(autouse=True)
def _fresh_throttle():
    lca.reset_throttle()
    yield
    lca.reset_throttle()


class _FakeUpstream:
    """Stands in for the ttyd websocket: swallows sends, ends when closed."""

    def __init__(self):
        self.sent: list = []
        self._closed = False

    async def send(self, data):
        self.sent.append(data)

    async def close(self):
        self._closed = True

    def __aiter__(self):
        return self

    async def __anext__(self):
        import asyncio

        while not self._closed:
            await asyncio.sleep(0.01)
        raise StopAsyncIteration


def _patch_upstream(monkeypatch) -> _FakeUpstream:
    up = _FakeUpstream()

    async def _connect(url, subprotocols=None):
        return up

    monkeypatch.setattr("app.web.labs.websockets.connect", _connect)
    return up


def _seen(admin_session, inst, wait=0.0):
    deadline = time.monotonic() + wait
    while True:
        admin_session.expire_all()
        row = admin_session.get(LabConsoleActivity, inst.id)
        if row is not None or time.monotonic() >= deadline:
            return row
        time.sleep(0.05)


def test_typing_in_a_ttyd_console_records_a_heartbeat(app_client, admin_session, tenant_a, monkeypatch):
    p = _make_person(admin_session, tenant_a, "hb1@a.edu")
    li = _seed_instance(admin_session, tenant_a, p.id)
    _login(app_client, "hb1@a.edu")
    up = _patch_upstream(monkeypatch)

    with app_client.websocket_connect(_ws_url(li), headers={**H, "Origin": "http://alpha.localhost"}, subprotocols=["tty"]) as ws:
        ws.send_text("0ls\r")  # ttyd INPUT frame
        assert _seen(admin_session, li, wait=3) is not None
    assert up.sent == ["0ls\r"]  # traffic is still forwarded untouched


def test_binary_input_frame_also_counts(app_client, admin_session, tenant_a, monkeypatch):
    p = _make_person(admin_session, tenant_a, "hb2@a.edu")
    li = _seed_instance(admin_session, tenant_a, p.id)
    _login(app_client, "hb2@a.edu")
    _patch_upstream(monkeypatch)

    with app_client.websocket_connect(_ws_url(li), headers={**H, "Origin": "http://alpha.localhost"}, subprotocols=["tty"]) as ws:
        ws.send_bytes(b"0pwd\r")
        assert _seen(admin_session, li, wait=3) is not None


def test_resize_and_pause_frames_are_not_activity(app_client, admin_session, tenant_a, monkeypatch):
    """An idle open tab (resize/pause/auth frames only) must not keep a lab alive."""
    p = _make_person(admin_session, tenant_a, "hb3@a.edu")
    li = _seed_instance(admin_session, tenant_a, p.id)
    _login(app_client, "hb3@a.edu")
    up = _patch_upstream(monkeypatch)

    with app_client.websocket_connect(_ws_url(li), headers={**H, "Origin": "http://alpha.localhost"}, subprotocols=["tty"]) as ws:
        ws.send_text('{"AuthToken":"","columns":80,"rows":24}')
        ws.send_text('1{"columns":100,"rows":30}')
        ws.send_text("2")
        ws.send_bytes(b"1resize")
        deadline = time.monotonic() + 3
        while len(up.sent) < 4 and time.monotonic() < deadline:
            time.sleep(0.02)
    assert len(up.sent) == 4
    assert _seen(admin_session, li, wait=0.5) is None


def test_many_keystrokes_cause_a_single_write(app_client, admin_session, tenant_a, monkeypatch):
    p = _make_person(admin_session, tenant_a, "hb4@a.edu")
    li = _seed_instance(admin_session, tenant_a, p.id)
    _login(app_client, "hb4@a.edu")
    _patch_upstream(monkeypatch)
    writes = []
    real = lca.record

    def _spy(db, **kw):
        writes.append(kw)
        return real(db, **kw)

    monkeypatch.setattr("app.web.labs.lab_console_activity.record", _spy)

    with app_client.websocket_connect(_ws_url(li), headers={**H, "Origin": "http://alpha.localhost"}, subprotocols=["tty"]) as ws:
        for ch in "abcdefghijklmnopqrstuvwxyz":
            ws.send_text("0" + ch)
        assert _seen(admin_session, li, wait=3) is not None
        time.sleep(0.2)
    assert len(writes) == 1


def test_heartbeat_failure_never_breaks_the_console(app_client, admin_session, tenant_a, monkeypatch):
    p = _make_person(admin_session, tenant_a, "hb5@a.edu")
    li = _seed_instance(admin_session, tenant_a, p.id)
    _login(app_client, "hb5@a.edu")
    up = _patch_upstream(monkeypatch)

    def _boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr("app.web.labs.lab_console_activity.record", _boom)

    with app_client.websocket_connect(_ws_url(li), headers={**H, "Origin": "http://alpha.localhost"}, subprotocols=["tty"]) as ws:
        ws.send_text("0x")
        ws.send_text("0y")
        deadline = time.monotonic() + 3
        while len(up.sent) < 2 and time.monotonic() < deadline:
            time.sleep(0.02)
    assert up.sent == ["0x", "0y"]


def test_unauthorized_ws_never_records(app_client, admin_session, tenant_a, monkeypatch):
    from starlette.websockets import WebSocketDisconnect

    owner = _make_person(admin_session, tenant_a, "hb6o@a.edu")
    _make_person(admin_session, tenant_a, "hb6x@a.edu")
    li = _seed_instance(admin_session, tenant_a, owner.id)
    _login(app_client, "hb6x@a.edu")
    _patch_upstream(monkeypatch)

    with pytest.raises(WebSocketDisconnect):
        with app_client.websocket_connect(_ws_url(li), headers={**H, "Origin": "http://alpha.localhost"}, subprotocols=["tty"]) as ws:
            ws.send_text("0x")
            ws.receive_text()
    assert _seen(admin_session, li, wait=0.5) is None


# --- RouterOS / HTTP path --------------------------------------------------


def _stub_http(monkeypatch):
    async def _fake(request, target, *, webfig=False, reviewed_document=False):
        return Response(content=b"ok", status_code=200)

    monkeypatch.setattr("app.web.labs._proxy_http", _fake)


def test_opening_a_console_page_counts(app_client, admin_session, tenant_a, monkeypatch):
    p = _make_person(admin_session, tenant_a, "hh1@a.edu")
    li = _seed_instance(admin_session, tenant_a, p.id)
    _login(app_client, "hh1@a.edu")
    h = H
    _stub_http(monkeypatch)

    assert app_client.get(f"/labs/instances/{li.id}/console/r1", headers=h).status_code == 200
    assert _seen(admin_session, li) is not None


def test_subresource_requests_do_not_count(app_client, admin_session, tenant_a, monkeypatch):
    """Assets/tokens/WebFig polling are not user action (an idle page must not keep a lab alive)."""
    p = _make_person(admin_session, tenant_a, "hh2@a.edu")
    li = _seed_instance(admin_session, tenant_a, p.id)
    _login(app_client, "hh2@a.edu")
    h = H
    _stub_http(monkeypatch)

    assert app_client.get(f"/labs/instances/{li.id}/console/r1/poll", headers=h).status_code == 200
    assert _seen(admin_session, li) is None


def test_other_person_http_never_records(app_client, admin_session, tenant_a, monkeypatch):
    owner = _make_person(admin_session, tenant_a, "hh3o@a.edu")
    _make_person(admin_session, tenant_a, "hh3x@a.edu")
    li = _seed_instance(admin_session, tenant_a, owner.id)
    _login(app_client, "hh3x@a.edu")
    h = H
    _stub_http(monkeypatch)

    assert app_client.get(f"/labs/instances/{li.id}/console/r1", headers=h).status_code == 403
    assert _seen(admin_session, li) is None


def test_slow_heartbeat_never_delays_input_forwarding(app_client, admin_session, tenant_a, monkeypatch):
    """The write runs as a background task: a stuck DB must not stall keystrokes."""
    p = _make_person(admin_session, tenant_a, "hb7@a.edu")
    li = _seed_instance(admin_session, tenant_a, p.id)
    _login(app_client, "hb7@a.edu")
    up = _patch_upstream(monkeypatch)

    def _slow(tenant_id, instance_id):
        time.sleep(1.5)

    monkeypatch.setattr("app.web.labs._write_console_heartbeat", _slow)

    with app_client.websocket_connect(_ws_url(li), headers={**H, "Origin": "http://alpha.localhost"}, subprotocols=["tty"]) as ws:
        started = time.monotonic()
        ws.send_text("0a")
        ws.send_text("0b")
        deadline = started + 1.0
        while len(up.sent) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert up.sent == ["0a", "0b"]
        assert time.monotonic() - started < 1.0


def test_heartbeat_failure_is_logged_at_warning_once(app_client, admin_session, tenant_a, monkeypatch):
    import logging

    p = _make_person(admin_session, tenant_a, "hb8@a.edu")
    li = _seed_instance(admin_session, tenant_a, p.id)
    _login(app_client, "hb8@a.edu")
    up = _patch_upstream(monkeypatch)

    def _boom(*a, **k):
        raise RuntimeError("permission denied for table lab_console_activity")

    monkeypatch.setattr("app.web.labs.lab_console_activity.record", _boom)
    # Attach to the module logger directly: other tests reconfigure root logging.
    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = _Capture(level=logging.DEBUG)
    target = logging.getLogger("app.services.lab_console_activity")
    old_level = target.level
    # A background write from an earlier test may still be finishing; let it land
    # (and consume the once-per-window warning) before this test's window starts.
    time.sleep(0.5)
    lca.reset_throttle()
    target.addHandler(handler)
    target.setLevel(logging.DEBUG)
    # alembic's env.py (run in-process by the migration tests) calls fileConfig(),
    # which disables pre-existing loggers.
    was_disabled = target.disabled
    target.disabled = False

    with app_client.websocket_connect(_ws_url(li), headers={**H, "Origin": "http://alpha.localhost"}, subprotocols=["tty"]) as ws:
        ws.send_text("0x")
        deadline = time.monotonic() + 3
        while len(up.sent) < 1 and time.monotonic() < deadline:
            time.sleep(0.02)
        time.sleep(0.3)
    warnings = [r for r in records if r.levelno == logging.WARNING and "heartbeat" in r.getMessage()]
    assert len(warnings) == 1 and "permission denied" in warnings[0].getMessage()
    assert lca._last_warned is not None
    lca.warn_failure(li.id, RuntimeError("again"))  # inside the window: no second warning
    again = [r for r in records if r.levelno == logging.WARNING and "heartbeat" in r.getMessage()]
    assert len(again) == 1
    target.removeHandler(handler)
    target.setLevel(old_level)
    target.disabled = was_disabled
