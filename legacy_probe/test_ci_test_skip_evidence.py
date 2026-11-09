"""Missing reports and silently skipped database suites cannot qualify CI."""

import pytest

from scripts.check_ci_test_skips import verify


@pytest.mark.parametrize(("xml", "allowed", "accepted"), [
    ('<testsuite><testcase classname="test_db" name="test_real"/></testsuite>', "", True),
    ('<testsuite><testcase classname="test_db" name="test_real"><skipped/></testcase></testsuite>', "", False),
    ('<testsuite><testcase classname="test_db" name="test_real"><skipped/></testcase></testsuite>',
     "test_db::test_real\n", True),
    ('<testsuite><testcase classname="test_db" name="test_real"/></testsuite>', "test_db::test_real\n", False),
    ('<testsuite/>', "", False),
    ('<testsuite><testcase/></testsuite>', "test_db::*\n", False),
    ('<testsuite><testcase/></testsuite>', "test_db::test_real\ntest_db::test_real\n", False),
    ('<!DOCTYPE r [<!ENTITY e "expanded">]><testsuite><testcase name="&e;"/></testsuite>', "", False),
])
def test_skip_evidence_requires_execution_and_exact_reviewed_allowlist(tmp_path, xml, allowed, accepted):
    report, allowlist = tmp_path / "report.xml", tmp_path / "allowlist.txt"
    report.write_text(xml)
    allowlist.write_text(allowed)
    if accepted:
        verify(report, allowlist)
    else:
        with pytest.raises(ValueError):
            verify(report, allowlist)
