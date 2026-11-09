"""The linter must keep working with nothing but PyYAML installed.

This is the property the whole gate depends on. `lint_bank` lives in this
repository; 252 of the technical banks live in `dotmac-academy`, which has no
application code and no database. If `bank_lint` ever acquires an import from
`app.*` or SQLAlchemy, the content repository's CI stops being able to run it,
the gate silently disappears, and the banks drift back to where they were —
246 of 333 failing, because nothing was looking.

So this test asserts the dependency boundary rather than the rules themselves.
"""

from __future__ import annotations

import ast
import pathlib
import subprocess
import sys
import textwrap

MODULE = pathlib.Path(__file__).resolve().parents[2] / "app" / "services" / "bank_lint.py"


def test_bank_lint_imports_nothing_from_the_application():
    """No `app.*` and no SQLAlchemy — the two things the content repo lacks."""
    tree = ast.parse(MODULE.read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module.split(".")[0])

    banned = {"app", "sqlalchemy", "alembic", "fastapi", "pydantic"}
    assert not (imported & banned), (
        f"bank_lint imports {sorted(imported & banned)} — the content repository "
        "cannot install those, so its CI would stop running the rules"
    )


def test_bank_lint_runs_in_a_bare_interpreter():
    """Import it in a subprocess with the repo *not* on the path.

    The static check above can be satisfied by a module that still fails at
    runtime, so this actually executes it the way the other repo would.
    """
    with_only_yaml = textwrap.dedent(
        f"""
        import importlib.util, sys
        spec = importlib.util.spec_from_file_location("bank_lint", r"{MODULE}")
        mod = importlib.util.module_from_spec(spec)
        sys.modules["bank_lint"] = mod          # dataclass needs this registered
        spec.loader.exec_module(mod)
        assert hasattr(mod, "lint_bank") and hasattr(mod, "parse_bank")
        assert not any(k.startswith("sqlalchemy") for k in sys.modules), "pulled sqlalchemy"
        assert not any(k.startswith("app.") for k in sys.modules), "pulled app.*"
        print("ok")
        """
    )
    proc = subprocess.run(  # noqa: S603
        [sys.executable, "-c", with_only_yaml],
        capture_output=True,
        text=True,
        cwd="/",  # not the repo, so `app` is not importable even by accident
    )
    assert proc.returncode == 0, proc.stderr
    assert "ok" in proc.stdout


def test_the_cli_exits_nonzero_on_a_failing_bank(tmp_path):
    """CI gates on the exit code, so a failing bank must not exit 0."""
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        textwrap.dedent(
            """
            bank:
              course: x
              kind: chapter
              chapter: 1
              version: 1
              questions:
                - id: q1
                  stem: "Which one?"
                  type: single
                  options: ["The considerably longer correct answer", "no", "or", "eh"]
                  correct: ["The considerably longer correct answer"]
                  rubric_category: recall
                  explanation: "e"
                  weight: 1
            """
        )
    )
    proc = subprocess.run(  # noqa: S603
        [sys.executable, str(MODULE), str(bad)], capture_output=True, text=True
    )
    assert proc.returncode == 1, f"expected exit 1, got {proc.returncode}: {proc.stdout}"
    assert "FAIL" in proc.stdout
