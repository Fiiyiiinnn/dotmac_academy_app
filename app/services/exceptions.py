"""Compatibility imports for kernel-owned domain exceptions.

Keeping this module avoids a noisy all-at-once import rewrite while ensuring
Academy services raise the exact classes handled by ``dotmac_kernel.create_app``.
"""

from dotmac_kernel.exceptions import (
    BadRequestError,
    ConflictError,
    DomainError,
    NotFoundError,
)

__all__ = ["BadRequestError", "ConflictError", "DomainError", "NotFoundError"]
