"""Check engine: dispatch checks to primitives and aggregate a weighted score."""

from __future__ import annotations

from collections.abc import Callable

from .primitives import eval_command, eval_config_grep, eval_probe

_EVAL = {
    "command": eval_command,
    "probe": eval_probe,
    "config_grep": eval_config_grep,
}


def eval_check(check, engine, handle, seed):
    """Evaluate one check -> ``{id, label, weight, pass, actual, expected, detail, hint}``.

    ``label`` and ``hint`` are authored on the check and passed straight through:
    the engine cannot know what "client-a-prefix" means to a learner or what to
    do about it, but the person who wrote the lab can. Both are optional, so a
    check without them still renders — just with less to go on.
    """
    out = _EVAL[check["type"]](check, engine, handle, seed)
    result = {
        "id": check["id"],
        "label": check.get("label", ""),
        "weight": check.get("weight", 1),
        **out,
    }
    if not result["pass"] and check.get("hint"):
        result["hint"] = check["hint"]
    return result


def run_checks(
    checks,
    engine,
    handle,
    seed,
    *,
    before_each: Callable[[], None] | None = None,
):
    """Evaluate all checks -> ``{score, max_score, per_check}`` (weighted).

    ``score`` = sum of weights of passing checks; ``max_score`` = sum of all
    weights; ``per_check`` = list of :func:`eval_check` results in order.
    """
    per = []
    for check in checks:
        if before_each is not None:
            before_each()
        per.append(eval_check(check, engine, handle, seed))
    max_score = sum(c.get("weight", 1) for c in checks)
    score = sum(r["weight"] for r in per if r["pass"])
    return {"score": score, "max_score": max_score, "per_check": per}
