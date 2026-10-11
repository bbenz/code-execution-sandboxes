"""Where candidate fixes come from."""

from __future__ import annotations

from abc import ABC, abstractmethod

from catalog import TargetInfo
from models import Candidate


class CandidateGenerator(ABC):
    name = "generator"

    @abstractmethod
    async def generate(self, target: TargetInfo, *, wave: int, count: int, feedback: str | None) -> list[Candidate]:
        """Return the candidates for one wave. `feedback` explains why the previous wave lost."""
