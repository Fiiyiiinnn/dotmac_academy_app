"""Reject empty test evidence and every skip not explicitly reviewed."""

from __future__ import annotations

import argparse
from pathlib import Path
from xml.etree import ElementTree


def verify(report: Path, allowlist: Path) -> None:
    try:
        raw = report.read_bytes()
        entries = [line.strip() for line in allowlist.read_text().splitlines() if line.strip()]
    except OSError as exc:
        raise ValueError("Missing test report or skip allowlist") from exc
    if len(entries) != len(set(entries)) or any("*" in entry or "::" not in entry for entry in entries):
        raise ValueError("Skip allowances must be unique exact testcase identities")
    if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
        raise ValueError("DTD/entity declarations are forbidden in test reports")
    try:
        cases = ElementTree.fromstring(raw).findall(".//testcase")  # noqa: S314 - DTD/entities rejected above
    except ElementTree.ParseError as exc:
        raise ValueError("Invalid test report") from exc
    if not cases:
        raise ValueError("Empty test report")
    if any(case.find("failure") is not None or case.find("error") is not None for case in cases):
        raise ValueError("Test report contains failures/errors")
    skipped = {
        f"{case.get('classname', '')}::{case.get('name', '')}" for case in cases if case.find("skipped") is not None
    }
    if skipped != set(entries):
        raise ValueError(
            f"Skip evidence mismatch: unexpected={sorted(skipped - set(entries))}; "
            f"unused allowances={sorted(set(entries) - skipped)}"
        )
    if all(case.find("skipped") is not None for case in cases):
        raise ValueError("No tests executed")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("allowlist", type=Path)
    args = parser.parse_args()
    verify(args.report, args.allowlist)


if __name__ == "__main__":
    main()
