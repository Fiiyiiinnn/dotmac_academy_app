"""Thin facade over canonical Learning declarations; no product mappings."""

from dotmac_learning.delivery_models import (
    REASON_BELOW_PASSING,
    REASON_CERTIFICATE_BLOCKED,
    REASON_FAILED_FINAL,
    REASON_INACTIVITY,
    REASON_KINDS,
    REASON_OVERDUE_WORK,
    STATUS_ACKNOWLEDGED,
    STATUS_OPEN,
    STATUS_RESOLVED,
    SuccessQueueEntry,
)

__all__ = [
    "REASON_BELOW_PASSING",
    "REASON_CERTIFICATE_BLOCKED",
    "REASON_FAILED_FINAL",
    "REASON_INACTIVITY",
    "REASON_KINDS",
    "REASON_OVERDUE_WORK",
    "STATUS_ACKNOWLEDGED",
    "STATUS_OPEN",
    "STATUS_RESOLVED",
    "SuccessQueueEntry",
]
