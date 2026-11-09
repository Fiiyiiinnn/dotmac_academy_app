"""ASGI entrypoint for the Academy kernel assembly."""

from __future__ import annotations

from app.kernel_runtime import create_academy_app

app = create_academy_app()

__all__ = ["app"]
