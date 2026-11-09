"""Academy YAML decoding and command-line rendering for learning bank rules."""

# Ported at new-composition genesis (learning-facing wave).

from __future__ import annotations

from pathlib import Path

import yaml
from dotmac_learning_bank_rules import (
    BankDoc,
    balance_targets,
    lint_bank,
    propose_balance,
)


def encode_bank_document(document, *, provenance: dict[str, object] | None = None) -> str:
    """Encode a Learning ``BankDocument`` to the YAML transport shape.

    Mirror of :func:`parse_bank`: Academy owns YAML in both directions and
    Learning never sees a file. Provenance rides as header comments so a
    re-imported export is recognizable (a hash-equal import is a no-op).
    The document carries full answer keys; callers gate disclosure.
    """
    questions = []
    for q in document.questions:
        row = {
            "id": q.id,
            "stem": q.stem,
            "type": q.type,
            "options": q.options,
            "correct": q.correct,
            "rubric_category": q.rubric_category,
            "weight": q.weight,
        }
        category = q.category if q.category is not None else q.competency
        if category is not None:
            row["category"] = category
        if q.explanation:
            row["explanation"] = q.explanation
        questions.append(row)
    payload = {
        "bank": {
            "course": document.course,
            "chapter": document.chapter,
            "kind": document.kind,
            "version": document.version,
            "questions": questions,
            "policy": dict(document.policy),
        }
    }
    header = ""
    if provenance:
        header = "".join(f"# {key}: {provenance[key]}\n" for key in sorted(provenance))
    return header + yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)


def parse_bank_text(text: str) -> BankDoc:
    """Parse bank YAML text (e.g. an upload) into a BankDoc dataclass."""
    data = yaml.safe_load(text)["bank"]
    return BankDoc(
        course=data["course"],
        chapter=data.get("chapter"),
        kind=data["kind"],
        version=int(data.get("version", 1)),
        questions=list(data["questions"]),
        policy=dict(data.get("policy") or {}),
    )


def parse_bank(path) -> BankDoc:
    """Load and parse a bank YAML file into a BankDoc dataclass."""
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))["bank"]
    return BankDoc(
        course=data["course"],
        chapter=data.get("chapter"),
        kind=data["kind"],
        version=int(data.get("version", 1)),
        questions=list(data["questions"]),
        policy=dict(data.get("policy") or {}),
    )


def _main(argv: list[str]) -> int:
    """Lint bank files given as paths. Exit 1 if any fails, so CI can gate on it."""
    import sys

    paths: list[Path] = []
    for a in argv:
        if a.startswith("--"):
            continue
        p = Path(a)
        paths.extend(sorted(p.glob("**/*.yaml")) if p.is_dir() else [p])
    if not paths:
        print("no bank files given", file=sys.stderr)
        return 2

    if "--propose" in argv:
        for path in paths:
            rows = propose_balance(parse_bank(path))
            if not rows:
                continue
            print(f"\n{path.name}")
            for r in rows:
                print(f"  {r['id']}  needs +{r['needed']}")
                print(f"    -  {r['option']}")
                if r["suggestion"]:
                    print(f"    +  {r['suggestion']}")
                else:
                    print(f"    !  {r['note']}")
        return 0

    if "--targets" in argv:
        for path in paths:
            rows = balance_targets(parse_bank(path))
            if not rows:
                continue
            print(f"\n{path.name}")
            for r in rows:
                print(f"  {r['id']}  answer={r['answer_len']}")
                for d in r["distractors"]:
                    delta = d["target"] - d["now"]
                    sign = "+" if delta > 0 else ""
                    print(f"     {d['now']:>4} -> {d['target']:<4} ({sign}{delta})  {d['text'][:76]}")
        return 0

    failed = 0
    for path in paths:
        try:
            violations = lint_bank(parse_bank(path))
        except Exception as exc:  # unparseable is a failure, not a crash
            print(f"FAIL {path}: could not parse — {exc}")
            failed += 1
            continue
        if violations:
            failed += 1
            print(f"FAIL {path}")
            for v in violations:
                print(f"     {v}")
    print(f"\n{len(paths) - failed}/{len(paths)} bank(s) pass")
    return 1 if failed else 0


if __name__ == "__main__":
    import sys

    raise SystemExit(_main(sys.argv[1:]))
