"""Migration graph access for the composed lineages.

Genesis composition: kernel + Learning (lr) + Academy assembly (aNNNN)
lineages are composed by `alembic.ini`/`alembic/env.py`; the ordinary
config is that file config. (The legacy app needed a graph builder to
exclude its retained-schema branches; this composition has none.)
"""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config

_ROOT = Path(__file__).resolve().parent.parent


def ordinary_migration_config() -> Config:
    return Config(str(_ROOT / "alembic.ini"))
