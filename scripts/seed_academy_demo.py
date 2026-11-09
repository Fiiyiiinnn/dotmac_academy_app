"""Retired demo-seed compatibility entrypoint; no application runtime is loaded."""

import sys


def main() -> None:
    """Refuse every invocation before configuration, credentials, or DB access."""
    print(
        "seed_academy_demo.py is retired and cannot seed or update data. "
        "For tenant creation, inspect python -m app.cli bootstrap-tenant --help. "
        "Use the current Admin user, instructor course/cohort, and bank-draft workflows. "
        "See docs/runbooks/RETIRED_DEMO_SEED.md.",
        file=sys.stderr,
    )
    raise SystemExit(2)


if __name__ == "__main__":
    main()
