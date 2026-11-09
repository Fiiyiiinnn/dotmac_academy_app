"""Deterministic per-attempt seed generation + template interpolation for labs.

A lab's ``seed_spec`` declares named variables; ``generate_seed`` resolves them
into concrete values using a PRNG seeded by ``attempt_id`` so the same attempt
always yields the same lab parameters. ``interpolate`` substitutes those values
into topology / instruction / check templates via ``{{key}}`` placeholders.
"""

from __future__ import annotations

import random
from typing import Any


def generate_seed(spec: dict, attempt_id: int) -> dict:
    """Resolve a seed spec into concrete values, deterministic per attempt_id.

    Each entry in ``spec`` maps a key to a generator spec:
      - ``{"type": "int", "min": a, "max": b}`` → integer in [a, b] inclusive.
      - ``{"type": "choice", "options": [...]}`` → one element of ``options``.

    Determinism: identical (spec, attempt_id) always produce an identical dict
    because the PRNG is seeded solely from ``attempt_id`` (never wall-clock).
    Keys are processed in insertion order so the draw sequence is stable.
    """
    rng = random.Random(attempt_id)
    seed: dict[str, Any] = {}
    for key, entry in spec.items():
        etype = entry["type"]
        if etype == "int":
            seed[key] = rng.randint(entry["min"], entry["max"])
        elif etype == "choice":
            seed[key] = rng.choice(entry["options"])
        else:
            raise ValueError(f"Unsupported seed spec type: {etype!r}")
    return seed


def interpolate(text: str, seed: dict) -> str:
    """Replace every ``{{key}}`` placeholder in ``text`` with ``str(seed[key])``."""
    for key, value in seed.items():
        text = text.replace("{{" + key + "}}", str(value))
    return text
