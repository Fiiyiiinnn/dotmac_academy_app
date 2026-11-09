"""Run the declared legacy qualification selection on Git-hosted CI.

The copied legacy suite is qualification collateral, not the maintained
application lint scope. Retain the exact selection alongside its JUnit report.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def selection() -> list[str]:
    args: list[str] = []
    excluded = (ROOT / "legacy_probe/EXCLUDED.txt").read_text().splitlines()
    for entry in excluded:
        path = ROOT / "legacy_probe" / entry
        if not entry or not path.resolve().is_relative_to(ROOT / "legacy_probe"):
            raise ValueError(f"Unsafe legacy exclusion: {entry!r}")
        if not path.is_file() or not entry.endswith(".py"):
            raise ValueError(f"Missing legacy exclusion: {entry!r}")
        args.append(f"--ignore=legacy_probe/{entry}")
    for entry in (ROOT / "legacy_probe/DESELECT.txt").read_text().splitlines():
        filename, separator, _symbol = entry.partition("::")
        path = (ROOT / filename).resolve()
        if not separator or not path.is_relative_to(ROOT / "legacy_probe") or not path.is_file():
            raise ValueError(f"Invalid legacy deselection: {entry!r}")
        if filename.removeprefix("legacy_probe/") in excluded:
            raise ValueError(f"Deselection overlaps excluded file: {entry!r}")
        args.append(f"--deselect={entry}")
    if len(args) != len(set(args)):
        raise ValueError("Duplicate legacy exclusions/deselections")
    return args


def main() -> int:
    if os.environ.get("GITHUB_ACTIONS") != "true":
        raise SystemExit("Run broad qualification on Git-hosted CI")
    args = selection()
    collect_args = [arg for arg in args if not arg.startswith("--deselect=")]
    collected = subprocess.run(  # noqa: S603 - fixed interpreter and repo-owned selection
        [
            sys.executable,
            "-m",
            "pytest",
            "legacy_probe/",
            "--collect-only",
            "-q",
            "-p",
            "no:cacheprovider",
            "-o",
            "addopts=",
            *collect_args,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if collected.returncode:
        print(collected.stdout)
        print(collected.stderr, file=sys.stderr)
        return collected.returncode
    nodes = {
        line.strip() for line in collected.stdout.splitlines() if line.startswith("legacy_probe/") and "::" in line
    }
    if not nodes:
        raise ValueError("Legacy collection produced no test identities")
    for arg in args:
        if arg.startswith("--deselect=") and arg.removeprefix("--deselect=") not in nodes:
            raise ValueError(f"Stale legacy deselection: {arg}")
    artifacts = ROOT / "artifacts"
    artifacts.mkdir(exist_ok=True)
    (artifacts / "legacy-selection.json").write_text(json.dumps({"arguments": args}, indent=2) + "\n")
    return subprocess.run(  # noqa: S603 - fixed interpreter/runner; checked repo-owned selection
        [
            sys.executable,
            "-m",
            "pytest",
            "legacy_probe/",
            "-p",
            "no:cacheprovider",
            "-o",
            "addopts=",
            *args,
            "-q",
            "--tb=short",
            "--junitxml=artifacts/legacy.xml",
        ],
        cwd=ROOT,
        check=False,
    ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
