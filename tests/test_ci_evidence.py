"""Missing reports and silently skipped database suites cannot qualify CI."""

import pytest

from scripts.check_ci_test_skips import verify


@pytest.mark.parametrize(
    ("xml", "allowed", "accepted"),
    [
        ('<testsuite><testcase classname="test_db" name="test_real"/></testsuite>', "", True),
        ('<testsuite><testcase classname="test_db" name="test_real"><skipped/></testcase></testsuite>', "", False),
        (
            '<testsuite><testcase classname="test_db" name="test_real"><skipped/></testcase></testsuite>',
            "test_db::test_real\n",
            False,
        ),
        ('<testsuite><testcase classname="test_db" name="test_real"/></testsuite>', "test_db::test_real\n", False),
        ("<testsuite/>", "", False),
        ("<testsuite><testcase/></testsuite>", "test_db::*\n", False),
        ("<testsuite><testcase/></testsuite>", "test_db::test_real\ntest_db::test_real\n", False),
        ('<!DOCTYPE r [<!ENTITY e "expanded">]><testsuite><testcase name="&e;"/></testsuite>', "", False),
    ],
)
def test_skip_evidence_requires_execution_and_exact_reviewed_allowlist(tmp_path, xml, allowed, accepted):
    report, allowlist = tmp_path / "report.xml", tmp_path / "allowlist.txt"
    report.write_text(xml)
    allowlist.write_text(allowed)
    if accepted:
        verify(report, allowlist)
    else:
        with pytest.raises(ValueError):
            verify(report, allowlist)


def test_reviewed_skip_is_accepted_alongside_executed_test(tmp_path):
    report, allowlist = tmp_path / "report.xml", tmp_path / "allowlist.txt"
    report.write_text(
        '<testsuite><testcase classname="suite" name="executed"/>'
        '<testcase classname="suite" name="absent"><skipped/></testcase></testsuite>'
    )
    allowlist.write_text("suite::absent\n")
    verify(report, allowlist)


def test_missing_report_is_rejected(tmp_path):
    allowlist = tmp_path / "allowlist.txt"
    allowlist.write_text("")
    with pytest.raises(ValueError, match="Missing"):
        verify(tmp_path / "missing.xml", allowlist)
