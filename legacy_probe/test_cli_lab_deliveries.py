"""D8 transaction isolation: failed materialization must not stop account mail."""

import argparse
from contextlib import contextmanager

from app import cli


def test_materialization_failure_still_delivers_password_reset(monkeypatch, capsys, caplog):
    events = []
    class DB:
        def commit(self):
            events.append("commit")
        def rollback(self):
            events.append("rollback")

    @contextmanager
    def session():
        events.append("open")
        yield DB()
        events.append("close")

    def fail(*a, **kw):
        raise RuntimeError("DETAIL learner@example.edu Secret course")

    def deliver(*a, **kw):
        assert events == ["open", "rollback", "close", "open"]
        events.append("password_reset_sent")
        return {"sent": 1, "retried": 0, "failed": 0}

    monkeypatch.setattr("app.services.lab_jobs.admin_session", session)
    monkeypatch.setattr("app.services.lab_result_delivery.materialize_pending", fail)
    monkeypatch.setattr("app.services.email_outbox.deliver_pending", deliver)
    cli._email_outbox(argparse.Namespace(limit=100, requeue_failed=False, requeue_failed_lab_deliveries=False))
    assert "password_reset_sent" in events
    assert "email-outbox: sent=1" in capsys.readouterr().out
    assert "RuntimeError:materialize" in caplog.text
    assert all(value not in caplog.text for value in ("DETAIL", "learner@example.edu", "Secret course"))
