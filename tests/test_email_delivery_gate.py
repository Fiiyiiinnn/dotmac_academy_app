"""Hosted Linux qualification of the root-owned durable mail sender gate.

These tests require the hosted runner's passwordless sudo. They deliberately
exercise real root-owned files and independent processes, without changing the
shared production-path gate used by other test partitions.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import fcntl
import json
import os
import selectors
import subprocess
import sys
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.models.email_outbox import EmailOutbox
from app.services import email, email_outbox, settings_store
from app.services import email_delivery_gate as gate

# No arbitrary paths or executable input cross the sudo boundary. This helper
# owns only its fresh UUID directory, never the application gate or its parent.
_ROOT_HELPER = r"""
import json, os, pathlib, re, shutil, sys
os.umask(0o022)
request = json.loads(sys.stdin.read())
path = pathlib.Path(request["path"])
assert re.fullmatch(r"/var/lib/dotmac-academy-ci-[0-9a-f]{32}", str(path))
action = request["action"]
if action == "create":
    path.mkdir(mode=0o755)
    path.chmod(0o755)
    for name, data in (("lock", b""), ("state", b"OPEN\n")):
        fd = os.open(path / name, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        os.fchmod(fd, 0o644)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
elif action == "remove":
    shutil.rmtree(path)
elif action in ("OPEN", "HELD", "malformed", "empty"):
    data = {"OPEN": b"OPEN\n", "HELD": b"HELD\n", "malformed": b"OPEN\nextra", "empty": b""}[action]
    replacement = path / "replacement"
    replacement.write_bytes(data)
    replacement.chmod(0o644)
    replacement.replace(path / "state")
elif action == "missing_state":
    (path / "state").unlink()
elif action == "missing_lock":
    (path / "lock").unlink()
elif action == "state_symlink":
    (path / "state").rename(path / "target")
    (path / "state").symlink_to("target")
elif action == "lock_symlink":
    (path / "lock").rename(path / "target")
    (path / "lock").symlink_to("target")
elif action == "state_writable":
    (path / "state").chmod(0o666)
elif action == "lock_writable":
    (path / "lock").chmod(0o666)
elif action == "directory_writable":
    path.chmod(0o777)
elif action == "state_unowned":
    os.chown(path / "state", 65534, 65534)
elif action == "state_hardlink":
    os.link(path / "state", path / "alias")
elif action == "replace_lock":
    (path / "lock").rename(path / "old_lock")
    (path / "lock").touch(mode=0o644)
else:
    raise AssertionError("unsupported fixture operation")
"""
_LOCK_HOLDER = r"""
import fcntl, sys
with open(sys.argv[1], "rb") as stream:
    print("waiting", flush=True)
    fcntl.flock(stream, fcntl.LOCK_EX)
    print("ready", flush=True)
    sys.stdin.readline()
"""


def _root(path: Path, action: str) -> None:
    subprocess.run(  # noqa: S603 - fixed helper and a validated disposable path
        ["sudo", "-n", "/usr/bin/python3", "-I", "-S", "-c", _ROOT_HELPER],  # noqa: S607
        input=json.dumps({"path": str(path), "action": action}),
        text=True,
        check=True,
        capture_output=True,
        timeout=10,
    )


@pytest.fixture
def isolated_gate(monkeypatch):
    path = Path(f"/var/lib/dotmac-academy-ci-{uuid.uuid4().hex}")
    _root(path, "create")
    monkeypatch.setattr(gate, "GATE_DIRECTORY", path)
    monkeypatch.setattr(gate, "LOCK_PATH", path / "lock")
    monkeypatch.setattr(gate, "STATE_PATH", path / "state")
    try:
        yield path
    finally:
        _root(path, "remove")


def _line(process, expected: str) -> None:
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        assert selector.select(timeout=5), f"child did not report {expected}"
        assert process.stdout.readline().decode().strip() == expected


@contextlib.contextmanager
def _exclusive_holder(path):
    process = subprocess.Popen(  # noqa: S603 - fixed Python helper
        [sys.executable, "-u", "-c", _LOCK_HOLDER, str(path / "lock")],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
    )
    try:
        _line(process, "waiting")
        yield process
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)


def _send():
    return email.send_email_detailed("learner@example.org", "Gate test", "<p>Hello</p>")


@pytest.fixture
def smtp_calls(monkeypatch):
    calls = []

    class SMTP:
        def __init__(self, *_args, **_kwargs):
            calls.append("connect")

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def send_message(self, _message):
            calls.append("send")

    cfg = SimpleNamespace(
        smtp_host="smtp.example.org",
        smtp_port=25,
        smtp_from="academy@example.org",
        smtp_starttls=False,
        smtp_user="",
        smtp_password="",
    )
    monkeypatch.setattr(settings_store, "effective", lambda _db: cfg)
    monkeypatch.setattr(email.smtplib, "SMTP", SMTP)
    return calls


def test_open_gate_sends_and_controlled_release_restores_delivery(isolated_gate, smtp_calls):
    assert _send().sent is True
    _root(isolated_gate, "HELD")
    result = _send()
    assert result.sent is False
    assert result.deferred is True
    assert email.send_email("learner@example.org", "Held", "<p>Held</p>") is False
    assert smtp_calls == ["connect", "send"]
    _root(isolated_gate, "OPEN")
    assert _send().sent is True
    assert smtp_calls == ["connect", "send", "connect", "send"]


@pytest.mark.parametrize(
    "condition",
    [
        "HELD",
        "malformed",
        "empty",
        "missing_state",
        "missing_lock",
        "state_symlink",
        "lock_symlink",
        "state_writable",
        "lock_writable",
        "directory_writable",
        "state_unowned",
        "state_hardlink",
    ],
)
def test_unsafe_or_held_gate_defers_without_smtp(isolated_gate, smtp_calls, condition):
    _root(isolated_gate, condition)
    with pytest.raises(gate.EmailDeliveryDeferred), gate.delivery_lease():
        pytest.fail("unsafe gate granted a lease")
    result = _send()
    assert result.deferred is True
    assert result.sent is False
    assert smtp_calls == []


def test_shared_lease_delays_exclusive_readiness_and_nested_send_finishes(isolated_gate, smtp_calls):
    with gate.delivery_lease():
        with _exclusive_holder(isolated_gate) as process:
            _root(isolated_gate, "HELD")
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                assert selector.select(timeout=0.2) == []
            # Admission covers the entire send while the operator drains.
            assert _send().sent is True
    assert smtp_calls == ["connect", "send"]
    with pytest.raises(gate.EmailDeliveryDeferred), gate.delivery_lease():
        pytest.fail("HELD admitted a fresh sender")


def test_exclusive_drain_becomes_ready_after_shared_sender_exits(isolated_gate):
    # Enter the process manager within the lease, but keep the process alive
    # beyond it so readiness proves the admitted sender actually drained.
    with contextlib.ExitStack() as processes:
        with gate.delivery_lease():
            process = processes.enter_context(_exclusive_holder(isolated_gate))
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                assert selector.select(timeout=0.2) == []
        _line(process, "ready")
        # A regression to blocking flock must fail within a bound, not wedge
        # the entire hosted suite behind the exclusive holder.
        probe = subprocess.run(  # noqa: S603 - fixed probe of disposable gate
            [
                sys.executable,
                "-c",
                """
import pathlib, sys
from app.services import email_delivery_gate as gate
gate.GATE_DIRECTORY = pathlib.Path(sys.argv[1])
gate.LOCK_PATH = gate.GATE_DIRECTORY / 'lock'
gate.STATE_PATH = gate.GATE_DIRECTORY / 'state'
try:
    with gate.delivery_lease():
        print('admitted')
except gate.EmailDeliveryDeferred:
    print('deferred')
""",
                str(isolated_gate),
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        assert probe.stdout.strip() == "deferred"


def test_killed_exclusive_holder_does_not_release_durable_hold(isolated_gate, smtp_calls):
    _root(isolated_gate, "HELD")
    with _exclusive_holder(isolated_gate) as process:
        _line(process, "ready")
        process.kill()
        process.wait(timeout=5)
    # Kernel locks die with processes; the independent durable state must survive.
    assert (isolated_gate / "state").read_bytes() == b"HELD\n"
    assert _send().deferred is True
    assert smtp_calls == []


def test_replaced_lock_invalidates_active_nested_lease(isolated_gate):
    with gate.delivery_lease():
        _root(isolated_gate, "replace_lock")
        with pytest.raises(gate.EmailDeliveryDeferred), gate.delivery_lease():
            pytest.fail("replaced lock inode reused an old admission")


def test_thread_cannot_reuse_another_threads_active_lease(isolated_gate):
    outcomes = []

    def attempt():
        try:
            with gate.delivery_lease():
                outcomes.append("admitted")
        except gate.EmailDeliveryDeferred:
            outcomes.append("deferred")

    with gate.delivery_lease():
        _root(isolated_gate, "HELD")
        copied = contextvars.copy_context()
        thread = threading.Thread(target=copied.run, args=(attempt,))
        thread.start()
        thread.join(timeout=5)
        assert not thread.is_alive()
    assert outcomes == ["deferred"]


def test_child_async_task_cannot_reuse_parent_task_admission(isolated_gate):
    async def attempt():
        with pytest.raises(gate.EmailDeliveryDeferred), gate.delivery_lease():
            pytest.fail("child task reused its parent's admission")

    async def parent():
        with gate.delivery_lease():
            _root(isolated_gate, "HELD")
            await asyncio.wait_for(asyncio.create_task(attempt()), timeout=5)

    asyncio.run(parent())


def test_fork_cannot_inherit_admission_or_unlock_parent(isolated_gate):
    read_fd, write_fd = os.pipe()
    child = None
    try:
        with gate.delivery_lease():
            _root(isolated_gate, "HELD")
            child = os.fork()
            if child == 0:
                os.close(read_fd)
                try:
                    with gate.delivery_lease():
                        outcome = b"admitted"
                except gate.EmailDeliveryDeferred:
                    outcome = b"deferred"
                except BaseException:
                    outcome = b"error"
                os.write(write_fd, outcome)
                os._exit(0)
            os.close(write_fd)
            write_fd = -1
            with selectors.DefaultSelector() as selector:
                selector.register(read_fd, selectors.EVENT_READ)
                assert selector.select(timeout=5), "forked sender did not finish"
            assert os.read(read_fd, 100) == b"deferred"
            status = os.waitpid(child, 0)[1]
            child = None
            assert status == 0
            # Child cleanup must close its inherited fd, never LOCK_UN the
            # parent's shared open-file description.
            with (isolated_gate / "lock").open("rb") as stream:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        if child is not None:
            os.kill(child, 9)
            os.waitpid(child, 0)
        os.close(read_fd)
        if write_fd != -1:
            os.close(write_fd)


def test_held_outbox_retains_intent_attempts_schedule_error_and_projection(
    isolated_gate,
    smtp_calls,
    genesis_database,
    monkeypatch,
):
    _admin, superuser, name = genesis_database
    url = make_url(superuser).set(database=name).render_as_string(hide_password=False)
    engine = create_engine(url)
    projections = []
    preparations = []
    monkeypatch.setattr(email_outbox, "_apply_delivery_projection", lambda *args: projections.append(args))
    monkeypatch.setattr(email_outbox, "_attachments", lambda *args: preparations.append(args) or [])
    try:
        with Session(engine) as db, db.begin():
            tenant_id = db.execute(
                text("INSERT INTO tenants (id, slug, name) VALUES (gen_random_uuid(), :s, 'T') RETURNING id"),
                {"s": f"gate-{uuid.uuid4().hex}"},
            ).scalar_one()
            _root(isolated_gate, "HELD")
            kwargs = dict(
                tenant_id=tenant_id,
                idempotency_key="held-intent",
                kind="generic",
                recipient="learner@example.org",
                subject="Gate",
                html_body="<p>Hello</p>",
            )
            assert email_outbox.enqueue_email(db, **kwargs) is True
            assert email_outbox.enqueue_email(db, **kwargs) is False
            row = db.scalars(select(EmailOutbox)).one()
            snapshot = {column.name: getattr(row, column.name) for column in EmailOutbox.__table__.columns}
            assert row.attempts == 0
            assert email_outbox.deliver_pending(db, now=datetime.now(UTC)) == {"sent": 0, "retried": 0, "failed": 0}
            db.flush()
            db.expire_all()
            assert {column.name: getattr(row, column.name) for column in EmailOutbox.__table__.columns} == snapshot
            assert smtp_calls == preparations == projections == []
            _root(isolated_gate, "OPEN")
            with monkeypatch.context() as deferred_send:
                deferred_send.setattr(
                    email_outbox, "send_email_detailed", lambda *args, **kwargs: email.EmailResult(False, deferred=True)
                )
                assert email_outbox.deliver_pending(db, now=datetime.now(UTC)) == {"sent": 0, "retried": 0, "failed": 0}
                db.flush()
                db.expire_all()
                assert {column.name: getattr(row, column.name) for column in EmailOutbox.__table__.columns} == snapshot
                assert projections == smtp_calls == []
            preparations.clear()
            assert email_outbox.deliver_pending(db, now=datetime.now(UTC)) == {"sent": 1, "retried": 0, "failed": 0}
            assert row.attempts == 1
            assert row.status == "sent"
            assert smtp_calls == ["connect", "send"]
            assert len(projections) == len(preparations) == 1
    finally:
        engine.dispose()
