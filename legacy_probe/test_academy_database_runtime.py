"""Hosted canaries for the genuine Kernel runtime and Academy's binding.

These tests require the future installed owner APIs and complete ordinary
Academy source. They are authored here, never executed in the source capsule.
Subprocesses keep Kernel's irreversible process binding isolated from other tests.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]


def _process(source: str, *, environment: dict[str, str] | None = None) -> None:
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", **(environment or {})}
    # The interpreter and every source program below are repository-owned.
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", textwrap.dedent(source)],
        cwd=_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    # Do not echo child tracebacks: a driver error can include a deployment DSN.
    assert result.returncode == 0, "isolated runtime canary failed; inspect securely in hosted CI"


@pytest.mark.parametrize(
    "module",
    ["app.database_runtime", "app.api.deps", "app.cli", "app.web.context", "app.web.labs"],
)
def test_ordinary_imports_neither_construct_engines_nor_claim_reference_runtime(module):
    _process(
        f"""
        import importlib
        import importlib.abc
        import sys
        import sqlalchemy

        claims = []

        class RejectReference(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname == "dotmac_kernel.db":
                    claims.append(fullname)
                    raise AssertionError("ordinary discovery claimed the reference database")

        def reject_engine(*args, **kwargs):
            raise AssertionError("ordinary discovery constructed a database engine")

        sys.meta_path.insert(0, RejectReference())
        sqlalchemy.create_engine = reject_engine
        importlib.import_module({module!r})
        assert "dotmac_kernel.db" not in sys.modules
        assert claims == []
        """,
        environment={"DATABASE_URL": "", "PLATFORM_DATABASE_URL": ""},
    )


def test_settings_derived_singleton_is_concurrent_lazy_and_strictly_bound():
    _process(
        """
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        import importlib
        import sys
        from sqlalchemy import Engine, event
        from sqlalchemy.engine import make_url
        from dotmac_kernel import session_runtime as owner
        from app import database_runtime as product
        from app.config import settings

        engines = []
        create_engine = owner.create_engine

        def tracked_engine(*args, **kwargs):
            engine = create_engine(*args, **kwargs)
            engines.append(engine)
            return engine

        def reject_connection(*args, **kwargs):
            raise AssertionError("construction or binding opened a connection")

        owner.create_engine = tracked_engine
        event.listen(Engine, "do_connect", reject_connection)
        assert engines == []
        barrier = Barrier(12)

        def get_runtime(_):
            barrier.wait()
            return product.get_academy_database_runtime()

        with ThreadPoolExecutor(max_workers=12) as executor:
            runtimes = list(executor.map(get_runtime, range(12)))
        runtime = runtimes[0]
        assert isinstance(runtime, owner.DatabaseRuntime)
        assert all(item is runtime for item in runtimes)
        assert len(engines) == 2
        assert runtime.engine.url == make_url(settings.database_url)
        assert runtime.platform_engine.url == make_url(settings.platform_database_url)
        assert runtime.session_factory.kw["bind"] is runtime.engine
        assert runtime.platform_session_factory.kw["bind"] is runtime.platform_engine
        assert runtime.tenant_settings == (owner.CANONICAL_TENANT_SETTING,)
        assert product.bind_academy_database_runtime() is runtime
        assert product.bind_academy_database_runtime() is runtime
        assert owner.resolve_database_runtime() is runtime

        try:
            owner.bind_database_runtime(runtime, required=False)
        except owner.RuntimeBindingError:
            pass
        else:
            raise AssertionError("strict binding was downgraded")
        other = owner.DatabaseRuntime(engine=runtime.engine)
        try:
            owner.bind_database_runtime(other, required=True)
        except owner.RuntimeBindingError:
            pass
        else:
            raise AssertionError("a second product runtime replaced the sealed one")
        try:
            importlib.import_module("dotmac_kernel.db")
        except RuntimeError:
            pass
        else:
            raise AssertionError("strict binding allowed reference engine construction")
        assert "dotmac_kernel.db" not in sys.modules
        assert len(engines) == 2
        for engine in engines:
            engine.dispose()
        """,
        environment={
            "DATABASE_URL": "postgresql+psycopg://academy_runtime@localhost/academy_runtime",
            "PLATFORM_DATABASE_URL": "postgresql+psycopg://academy_platform@localhost/academy_runtime",
        },
    )


def test_platform_url_falls_back_only_to_product_database_setting():
    _process(
        """
        from app.database_runtime import get_academy_database_runtime
        runtime = get_academy_database_runtime()
        assert runtime.engine.url == runtime.platform_engine.url
        assert runtime.engine is not runtime.platform_engine
        runtime.engine.dispose()
        runtime.platform_engine.dispose()
        """,
        environment={
            "DATABASE_URL": "postgresql+psycopg://academy_runtime@localhost/academy_runtime",
            "PLATFORM_DATABASE_URL": "",
        },
    )


def test_cli_binds_before_dispatch_and_preserves_unknown_tenant_error():
    _process(
        """
        import sys
        from uuid import uuid4
        from sqlalchemy import text
        from dotmac_kernel.session_runtime import resolve_database_runtime
        from app import cli
        from app.database_runtime import get_academy_database_runtime

        def dispatch(args):
            runtime = get_academy_database_runtime()
            assert resolve_database_runtime() is runtime
            with runtime.platform_session() as db:
                assert db.scalar(text("SELECT 1")) == 1

        cli._bootstrap = dispatch
        sys.argv = ["app.cli", "bootstrap-tenant", "--slug", "unused",
                    "--name", "unused", "--admin-email", "unused@example.test",
                    "--admin-password", "unused"]
        cli.main()
        try:
            with cli._tenant_session("runtime-unknown-" + uuid4().hex):
                raise AssertionError("unknown tenant yielded an unscoped session")
        except SystemExit as exc:
            assert "No tenant with slug" in str(exc)
        else:
            raise AssertionError("CLI lost its unknown-tenant error convention")
        """,
        environment=_hosted_urls(),
    )


def _hosted_urls() -> dict[str, str]:
    url = os.environ.get("TEST_DATABASE_URL")
    platform_url = os.environ.get("PLATFORM_DATABASE_URL")
    assert url and platform_url, "hosted runtime canaries require TEST_DATABASE_URL and the hosted PLATFORM_DATABASE_URL"
    return {"DATABASE_URL": url, "PLATFORM_DATABASE_URL": platform_url}


def test_real_bound_request_transactions_and_tenant_scope_cleanup():
    _process(
        """
        from types import SimpleNamespace
        from uuid import uuid4
        from sqlalchemy import event, text
        from starlette.requests import Request
        from app.api.deps import get_db, get_platform_db
        from app.database_runtime import bind_academy_database_runtime

        runtime = bind_academy_database_runtime()
        with runtime.engine.connect() as connection:
            assert connection.scalar(text("SELECT current_user")) == "app_user"
            assert connection.scalar(text(
                "SELECT NOT rolsuper AND NOT rolbypassrls FROM pg_roles WHERE rolname = current_user"
            ))
        with runtime.platform_engine.connect() as connection:
            assert connection.scalar(text("SELECT current_user")) == "platform_api"

        events = []
        event.listen(runtime.engine, "commit", lambda connection: events.append("commit"))
        event.listen(runtime.engine, "rollback", lambda connection: events.append("rollback"))
        tid = uuid4()
        request = Request({"type": "http", "headers": [], "state": {"tenant": SimpleNamespace(id=tid)}})
        boundary = get_db(request)
        db = next(boundary)
        assert db.get_bind() is runtime.engine
        assert db.scalar(text("SELECT current_setting('app.current_tenant', true)")) == str(tid)
        assert list(boundary) == []
        assert events == ["commit"]
        assert runtime.engine.pool.checkedout() == 0

        events.clear()
        boundary = get_db(request)
        db = next(boundary)
        assert db.scalar(text("SELECT 1")) == 1
        failure = ValueError("canary")
        try:
            boundary.throw(failure)
        except ValueError as exc:
            assert exc is failure
        else:
            raise AssertionError("request failure was swallowed")
        assert events == ["rollback"]
        assert runtime.engine.pool.checkedout() == 0

        # The exact session pattern used by nav and WebSocket auth: the caller
        # owns close and commits nothing; tenant_scope owns only tenant priming.
        db = runtime.session_factory()
        try:
            with runtime.tenant_scope(db, tid):
                assert db.scalar(text("SELECT current_setting('app.current_tenant', true)")) == str(tid)
                db.commit()
                assert db.scalar(text("SELECT current_setting('app.current_tenant', true)")) == str(tid)
                db.rollback()
                assert db.scalar(text("SELECT current_setting('app.current_tenant', true)")) == str(tid)
            db.rollback()
            assert db.scalar(text("SELECT nullif(current_setting('app.current_tenant', true), '')")) is None
        finally:
            db.close()
        assert runtime.engine.pool.checkedout() == 0

        # A subsequent tenant-less request cannot inherit the previous tenant.
        boundary = get_db(Request({"type": "http", "headers": [], "state": {}}))
        db = next(boundary)
        assert db.scalar(text("SELECT nullif(current_setting('app.current_tenant', true), '')")) is None
        assert list(boundary) == []
        boundary = get_platform_db()
        db = next(boundary)
        assert db.get_bind() is runtime.platform_engine
        assert db.scalar(text("SELECT current_user")) == "platform_api"
        assert list(boundary) == []
        assert runtime.platform_engine.pool.checkedout() == 0
        """,
        environment=_hosted_urls(),
    )


def test_nav_and_websocket_auth_failure_close_real_scoped_sessions_without_commits():
    _process(
        """
        import asyncio
        from types import SimpleNamespace
        from uuid import uuid4
        from sqlalchemy import event, text
        from starlette.datastructures import Headers
        from starlette.requests import Request
        from app.database_runtime import bind_academy_database_runtime
        from app.web import context, labs

        runtime = bind_academy_database_runtime()
        tenant = SimpleNamespace(id=uuid4())
        commits = []
        event.listen(runtime.engine, "commit", lambda connection: commits.append(True))
        seen = []

        def unauthenticated(db, tid, token):
            assert db.get_bind() is runtime.engine
            assert db.scalar(text("SELECT current_setting('app.current_tenant', true)")) == str(tenant.id)
            seen.append(tid)
            return None

        context.web_auth._current_person = unauthenticated
        context.effective = lambda db: SimpleNamespace(branding_name="Hosted canary")
        request = Request({
            "type": "http", "path": "/learn", "scheme": "https", "server": ("localhost", 443),
            "headers": [(b"cookie", (context.web_auth.COOKIE + "=invalid").encode())],
            "state": {"tenant": tenant},
        })
        assert context.nav_context(request)["current_person"] is None
        assert seen == [tenant.id]
        assert runtime.engine.pool.checkedout() == 0

        def database_error(db, tid, token):
            unauthenticated(db, tid, token)
            raise ValueError("auth lookup canary")

        context.web_auth._current_person = database_error
        assert context.nav_context(request)["current_person"] is None
        assert runtime.engine.pool.checkedout() == 0

        class Socket:
            headers = Headers({"host": "localhost", "origin": "https://localhost"})
            scope = {"scheme": "https"}
            cookies = {labs.web_auth.COOKIE: "invalid"}

            def __init__(self):
                self.codes = []

            async def close(self, *, code):
                self.codes.append(code)

        labs._tenant_resolver._resolve = lambda host: tenant
        labs.web_auth._current_person = unauthenticated
        socket = Socket()
        asyncio.run(labs.console_ws(socket, uuid4(), "linux"))
        assert socket.codes == [1008]
        assert seen == [tenant.id, tenant.id, tenant.id]
        assert runtime.engine.pool.checkedout() == 0
        assert commits == []

        # Closing either auth session returns a transaction-local scope to the
        # pool without leaking it into the next borrower's transaction.
        with runtime.session_factory() as db:
            assert db.scalar(text("SELECT nullif(current_setting('app.current_tenant', true), '')")) is None
        """,
        environment=_hosted_urls(),
    )
