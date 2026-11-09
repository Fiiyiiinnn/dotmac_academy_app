"""Actual CLI parsing preserves omission and explicit report-window values."""

import sys

import pytest

from app import cli


@pytest.mark.parametrize(
    ("flags", "expected"), [([], None), (["--hours", "0"], 0), (["--hours", "-3"], -3), (["--hours", "19"], 19)]
)
def test_admin_report_parser_passes_policy_omission_and_explicit_values(monkeypatch, flags, expected):
    received = []
    monkeypatch.setattr(cli, "_admin_report", lambda args: received.append(args.hours))
    monkeypatch.setattr(sys, "argv", ["app.cli", "admin-report", *flags])
    cli.main()
    assert received == [expected]
