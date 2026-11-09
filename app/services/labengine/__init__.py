"""Lab engine SDK interfaces; runtime adapter composition is a separate slice."""

from .interface import ExecResult, LabEngine, LabHandle, WrongLabHostError

__all__ = ["ExecResult", "LabEngine", "LabHandle", "WrongLabHostError"]
