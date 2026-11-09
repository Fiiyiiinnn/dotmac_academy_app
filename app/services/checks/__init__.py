"""Declarative lab check engine.

Grades a lab by evaluating a list of declarative checks against a running lab
instance via a :class:`~app.services.labengine.interface.LabEngine`.

Check dict shape (one per check):
  - ``id`` (str): stable identifier, surfaced in per-check results.
  - ``type`` (str): one of ``command``, ``probe``, ``config_grep``.
  - ``node`` (str): logical node name (resolved via ``handle.nodes``).
  - ``weight`` (int, default 1): contribution to the score when passing.

``command`` checks:
  - ``command`` (str): shell/CLI command, ``{{key}}`` interpolated against seed.
  - ``transport`` (str, optional): ``"ssh"`` routes via ``engine.ssh_exec``
    (RouterOS CHR); otherwise ``engine.exec(node, ["sh","-c",cmd])``.
  - ``user`` / ``password`` (str, optional, ssh only): password is interpolated.
  - ``assert`` (dict): one of ``{"jsonpath":..,"equals":..}``,
    ``{"regex":..}``, or ``{"exit_code":..}`` (default exit_code 0).

``probe`` checks:
  - ``probe`` (dict): ``{"kind": "ping"|"dns"|"http", "target":..., ...}``.

``config_grep`` checks:
  - ``file`` (str): path to cat on the node.
  - ``contains`` (str): interpolated substring that must be present.
"""

from .engine import eval_check, run_checks
from .primitives import eval_command, eval_config_grep, eval_probe

__all__ = [
    "eval_check",
    "eval_command",
    "eval_config_grep",
    "eval_probe",
    "run_checks",
]
