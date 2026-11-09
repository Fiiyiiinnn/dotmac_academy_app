"""Sanitize private-fixture reports before terminal and JUnit publication."""

from __future__ import annotations

import re

import pytest

_PRIVATE_NODEIDS: set[str] = set()
_PRIVATE_MODULES = (
    "tests/test_webfig_adapter.py",
    "tests/test_webfig_data_parser.py",
    "legacy_probe/web/test_webfig_proxy.py",
)


def pytest_configure(config):
    config.addinivalue_line("markers", "private_fixture_data: consumes private vendor compatibility fixtures")
    # Enforce this before collection. Other capture modes can stream subprocess
    # bytes before the report hook can sanitize them.
    if config.getoption("capture") != "fd":
        raise pytest.UsageError("Private-fixture reporting requires --capture=fd before collection")
    if config.getoption("log_cli_level") is not None or config.getini("log_cli"):
        raise pytest.UsageError("Private-fixture reporting forbids live logging")
    if config.getoption("log_file") or config.getini("log_file"):
        raise pytest.UsageError("Private-fixture reporting forbids unsanitized file logging")


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if item.get_closest_marker("private_fixture_data") is None:
        return
    report.sections.clear()
    report.user_properties.clear()
    if hasattr(report, "wasxfail"):
        report.wasxfail = "Private-fixture expectation (details withheld)"
    if report.longrepr is not None:
        exception_type = "PrivateFixtureFailure"
        if call.excinfo is not None:
            candidate = call.excinfo.type.__name__
            if re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]{0,79}", candidate):
                exception_type = candidate
        if report.skipped:
            report.longrepr = (*report.location[:2], "Private-fixture skip (details withheld)")
        else:
            # Test IDs and locations are source-owned public metadata. Exception
            # messages, reprs, locals and captured sections may contain fixtures.
            report.longrepr = f"{report.nodeid[:512]}: {exception_type}: private-fixture diagnostics withheld"


def pytest_collection_modifyitems(items):
    _PRIVATE_NODEIDS.update(
        item.nodeid for item in items if item.get_closest_marker("private_fixture_data") is not None
    )


@pytest.hookimpl(tryfirst=True)
def pytest_warning_recorded(warning_message, when, nodeid, location):
    # Warning summaries bypass captured report sections and need the same policy.
    if nodeid in _PRIVATE_NODEIDS or nodeid.startswith(_PRIVATE_MODULES):
        warning_message.message = UserWarning("Private-fixture warning (details withheld)")
        warning_message.category = UserWarning
        warning_message.line = ""
        warning_message.source = None
