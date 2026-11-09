"""Canonical migration CLI: configuration always precedes graph construction.

Use ``python -m app.migration_cli heads|history|show REV|upgrade REV``.
Historical replay and identity-import custody remain separate caller operations.
"""

from __future__ import annotations

import argparse

from alembic import command
from app.migration_graph import ordinary_migration_config


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("heads", "history", "show", "upgrade"))
    parser.add_argument("revision", nargs="?")
    args = parser.parse_args(argv)
    if args.command in {"show", "upgrade"} and not args.revision:
        parser.error("show and upgrade require an explicit revision")
    if args.command in {"heads", "history"} and args.revision:
        parser.error("heads and history do not accept a revision")
    config = ordinary_migration_config()
    if args.command == "heads":
        command.heads(config)
    elif args.command == "history":
        command.history(config)
    elif args.command == "show":
        command.show(config, args.revision)
    else:
        command.upgrade(config, args.revision)


if __name__ == "__main__":
    main()
