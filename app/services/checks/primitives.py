"""Check primitives: command, probe, config_grep.

Each primitive takes ``(check, engine, handle, seed)`` and returns
``{"pass": bool, "actual": ..., "expected": ..., "detail": str}``.

``detail`` is the learner-facing sentence. A raw ``actual`` is often useless on
its own — a failed jsonpath read renders as "actual: None", which says nothing
about what went wrong — so every primitive explains what it observed in words.
Check authors add ``label`` and ``hint`` (see :mod:`app.services.checks.engine`)
for a human name and the corrective action.
"""

from __future__ import annotations

import json
import re

from jsonpath_ng import parse as jp_parse

from app.config import settings, validate_lab_check_output_limit
from app.services.lab_seed import interpolate

# Command output echoed back to the learner is truncated: it is evidence, not a
# terminal, and an unbounded blob would be stored on every Score row.
# Limits are deployment policy in Settings; no database session or runtime
# adapter is introduced into these existing pure caller interfaces.


def _evidence(text: str) -> str:
    """A one-line, bounded excerpt of command output for the learner."""
    flat = " ".join((text or "").split())
    if not flat:
        return "no output"
    limit = validate_lab_check_output_limit(settings.lab_check_evidence_chars)
    return flat[:limit] + ("…" if len(flat) > limit else "")


def eval_command(check, engine, handle, seed):
    """Run a command on a node and assert on stdout/exit code.

    ``transport: ssh`` routes to ``engine.ssh_exec`` (RouterOS CHR via mgmt IP);
    otherwise the command runs through ``engine.exec`` as ``sh -c``.
    """
    cmd = interpolate(check["command"], seed)
    node = check["node"]
    if check.get("transport") == "ssh":  # RouterOS CHR node: SSH to mgmt IP
        res = engine.ssh_exec(
            handle,
            node,
            cmd,
            user=check.get("user", "admin"),
            password=interpolate(check.get("password", ""), seed),
        )
    else:  # container node: docker exec
        res = engine.exec(handle, node, ["sh", "-c", cmd])
    a = check.get("assert", {})
    expected = a.get("equals")
    if "jsonpath" in a:
        try:
            actual = str(next(m.value for m in jp_parse(a["jsonpath"]).find(json.loads(res.stdout))))
        except Exception:
            actual = None
        ok = actual == str(expected)
        if ok:
            detail = f"{node}: read {actual}, as required."
        elif actual is None:
            # The distinction that matters to a learner: the value could not be
            # read at all (usually because the thing is not configured yet),
            # rather than read and found wrong.
            detail = (
                f"{node}: could not read a value from `{cmd}` — "
                f"nothing matched {a['jsonpath']}. Output was: {_evidence(res.stdout)}"
            )
        else:
            detail = f"{node}: read {actual}, expected {expected}."
    elif "regex" in a:
        actual = res.stdout.strip()
        ok = re.search(a["regex"], res.stdout) is not None
        expected = a["regex"]
        detail = (
            f"{node}: output matched the expected pattern."
            if ok
            else f"{node}: `{cmd}` output did not match /{a['regex']}/. Output was: {_evidence(res.stdout)}"
        )
    else:
        actual = res.exit_code
        wanted = a.get("exit_code", 0)
        ok = res.exit_code == wanted
        expected = wanted
        detail = (
            f"{node}: `{cmd}` succeeded."
            if ok
            else f"{node}: `{cmd}` exited {res.exit_code} (wanted {wanted}). Output was: {_evidence(res.stdout)}"
        )
    return {"pass": ok, "actual": actual, "expected": expected, "detail": detail}


def eval_probe(check, engine, handle, seed):
    """Reachability probe: ping / dns / http."""
    p = check["probe"]
    node = check["node"]
    target = interpolate(p.get("target", ""), seed)
    if p["kind"] == "ping":
        res = engine.exec(handle, node, ["ping", "-c", str(p["count"]), target])
        m = re.search(r"(\d+) received", res.stdout)
        got = int(m.group(1)) if m else 0
        need = p.get("min_success", 1)
        ok = got >= need
        return {
            "pass": ok,
            "actual": f"{got} received",
            "expected": f">={need}",
            "detail": (
                f"{node} reached {target} ({got} of {p['count']} replies)."
                if ok
                else f"{node} could not reach {target} — {got} of {p['count']} replies, needed {need}."
            ),
        }
    if p["kind"] == "dns":
        res = engine.exec(handle, node, ["nslookup", target])
        ok = "Address" in res.stdout
        return {
            "pass": ok,
            "actual": res.stdout.strip()[: validate_lab_check_output_limit(settings.lab_check_dns_actual_chars)],
            "expected": f"resolves {target}",
            "detail": (
                f"{node} resolved {target}."
                if ok
                else f"{node} could not resolve {target}. Output was: {_evidence(res.stdout)}"
            ),
        }
    if p["kind"] == "http":
        res = engine.exec(
            handle,
            node,
            ["sh", "-c", f"curl -s -o /dev/null -w '%{{http_code}}' {target}"],
        )
        want = str(p.get("status", 200))
        got = res.stdout.strip()
        ok = got == want
        return {
            "pass": ok,
            "actual": got,
            "expected": want,
            "detail": (
                f"{node} got HTTP {want} from {target}."
                if ok
                else f"{node} got {got or 'no response'} from {target}, expected HTTP {want}."
            ),
        }
    raise ValueError(f"unknown probe {p['kind']}")


def eval_config_grep(check, engine, handle, seed):
    """Assert that ``cat <file>`` on the node contains an interpolated substring."""
    node = check["node"]
    res = engine.exec(handle, node, ["sh", "-c", f"cat {check['file']}"])
    pat = interpolate(check["contains"], seed)
    ok = pat in res.stdout
    return {
        "pass": ok,
        "actual": ("present" if ok else "absent"),
        "expected": pat,
        "detail": (
            f"{node}: {check['file']} contains the expected configuration."
            if ok
            else f"{node}: {check['file']} does not contain `{pat}`."
        ),
    }
