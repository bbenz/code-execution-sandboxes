"""The sandbox interface the race loop uses. Implementations: ACA Sandboxes, local Docker, and a fake."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from models import Lane


@dataclass
class SandboxRef:
    id: str
    labels: dict[str, str] = field(default_factory=dict)


@dataclass
class ExecOutcome:
    exit_code: int
    stdout: str
    stderr: str


@dataclass
class EgressAudit:
    supported: bool
    allowed: list[dict] = field(default_factory=list)
    denied: list[dict] = field(default_factory=list)
    last_updated: str | None = None
    # Set only when the log is known to hold every attempt made up to this time. ACA's decision log lags
    # (an attempt can show up 10-20 s after `last_updated` moves past it), so it never sets this.
    complete_through: str | None = None


class SandboxBackend(ABC):
    name = "base"

    async def startup(self) -> None:
        """Discover images, create shared configuration, and so on."""

    async def prepare_run(self, run_id: str) -> None:
        """Called once before a race starts."""

    async def finish_run(self, run_id: str) -> None:
        """Called once after a race ends, even if it failed."""

    @abstractmethod
    async def create(self, *, run_id: str, name: str, lane: Lane, labels: dict[str, str],
                     environment: dict[str, str]) -> SandboxRef: ...

    @abstractmethod
    async def exec(self, ref: SandboxRef, command: str, *, timeout_s: int = 120) -> ExecOutcome: ...

    @abstractmethod
    async def write_file(self, ref: SandboxRef, path: str, data: bytes) -> None: ...

    @abstractmethod
    async def read_file(self, ref: SandboxRef, path: str) -> bytes: ...

    @abstractmethod
    async def egress_audit(self, ref: SandboxRef) -> EgressAudit: ...

    @abstractmethod
    async def delete(self, ref: SandboxRef) -> None: ...

    async def sweep(self, *, older_than_s: int | None = None) -> int:
        """Delete leftover sandboxes from earlier runs. Returns how many were removed."""
        return 0

    def has_image(self, image: str) -> bool:
        return True

    async def refresh_images(self) -> None:
        """Re-discover the engine images, e.g. after prepare_images.py built new ones."""

    def describe(self) -> dict:
        return {"name": self.name}
