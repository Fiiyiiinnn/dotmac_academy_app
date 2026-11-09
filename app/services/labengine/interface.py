from abc import ABC, abstractmethod
from dataclasses import dataclass

from app.config import LabExecutionPolicy


@dataclass
class ExecResult:
    stdout: str
    stderr: str
    exit_code: int


class WrongLabHostError(RuntimeError):
    """Raised when a lab engine operation is attempted on a non-lab host."""


@dataclass
class LabHandle:
    instance_name: str
    nodes: dict  # logical node name -> container name
    mgmt: dict  # logical node name -> mgmt IPv4
    kinds: dict  # logical node name -> containerlab kind


class LabEngine(ABC):
    @property
    @abstractmethod
    def execution_policy(self) -> LabExecutionPolicy:
        """The exact immutable policy shared with this pass's claimed operations."""
        ...

    @abstractmethod
    def inventory(self) -> dict[str, str]:
        """Return running lab names mapped to their authoritative topology paths."""
        ...

    @abstractmethod
    def deploy(self, topology_text: str, instance_name: str) -> LabHandle: ...

    @abstractmethod
    def destroy(self, instance_name: str) -> None: ...

    @abstractmethod
    def deploy_if_absent(self, topology_text: str, instance_name: str) -> LabHandle | None:
        """Deploy ``instance_name`` only if it is not already running.

        Observation and mutation happen inside exactly ONE host-lock
        acquisition, atomically — never by composing the public
        :meth:`inventory`/:meth:`deploy` (each of which independently
        acquires and releases its own lock, reopening the exact race this
        primitive exists to close). Returns ``None`` (never calling
        :meth:`deploy`'s underlying mutation) when the runtime is found
        already present; returns the fresh :class:`LabHandle` when the
        precondition held and the deploy actually ran.
        """
        ...

    @abstractmethod
    def destroy_if_present(self, instance_name: str) -> bool:
        """Destroy ``instance_name`` only if it is currently running.

        Same single-lock-acquisition guarantee as :meth:`deploy_if_absent`.
        Returns ``False`` (never calling :meth:`destroy`'s underlying
        mutation) when the runtime is already absent; returns ``True`` when
        the precondition held and the destroy actually ran.
        """
        ...

    @abstractmethod
    def reset(self, topology_text: str, instance_name: str) -> LabHandle: ...

    @abstractmethod
    def exec(self, handle: LabHandle, node: str, command: list) -> ExecResult: ...

    @abstractmethod
    def ssh_exec(
        self,
        handle: LabHandle,
        node: str,
        command: str,
        user: str = "admin",
        password: str = "",
    ) -> ExecResult: ...

    @abstractmethod
    def status(self, instance_name: str) -> str: ...

    @abstractmethod
    def inspect_running(self, instance_name: str) -> LabHandle | None:
        """Reconstruct a live :class:`LabHandle` for an already-running
        ``instance_name`` from a fresh, locked inspection — never a
        redeploy. Returns ``None`` if the instance is not currently running.

        Used to resync ``consoles``/``status`` for a conditional deploy
        whose runtime is observed present — UNCONDITIONALLY, regardless of
        whether ``consoles`` was already populated on the DB row (any
        pre-existing value is untrustworthy for this operation kind
        regardless; see ``app/services/lab_lifecycle.py``'s
        ``_rebuild_consoles_from_live_inspection`` for the full rationale,
        including the crash-then-retry gap this also covers: a worker crash
        between a successful ``deploy_if_absent()`` and that same attempt
        ever recording consoles).
        """
        ...

    @abstractmethod
    def console_target(self, handle: LabHandle, node: str) -> str: ...
