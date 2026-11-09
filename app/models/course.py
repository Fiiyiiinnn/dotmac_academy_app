"""Thin facade over canonical Learning declarations; no product mappings."""

from dotmac_learning.curriculum_models import Chapter
from dotmac_learning.models import Course

__all__ = ["Chapter", "Course"]
