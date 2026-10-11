"""Data shared by the race loop, the backends, and the web UI."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class CandidateFile:
    path: str
    content: str


@dataclass
class Candidate:
    id: str
    wave: int
    strategy: str
    author: str
    explanation: str = ""
    files: list[CandidateFile] = field(default_factory=list)

    def public(self) -> dict:
        return {
            "id": self.id,
            "wave": self.wave,
            "strategy": self.strategy,
            "author": self.author,
            "explanation": self.explanation,
            "files": [file.path for file in self.files],
        }


@dataclass
class Lane:
    """How a candidate's sandbox is booted: from which disk image, and with which egress policy."""

    image: str = "primed"  # primed | base
    egress: str = "locked"  # locked (default-deny) | registry (package registries only)
    online: bool = False  # allow dependency downloads during verify (cold builds)
    label: str = ""


@dataclass
class Outcome:
    candidate: Candidate
    lane: Lane = field(default_factory=Lane)
    status: str = "queued"  # queued | running | passed | failed | disqualified | error
    reason: str = ""
    sandbox_id: str | None = None
    health: dict | None = None
    verdict: dict | None = None
    audit: dict | None = None
    timings: dict[str, int] = field(default_factory=dict)
    eligible: bool = False

    @property
    def diff_size(self) -> int:
        diff = (self.verdict or {}).get("diff") or {}
        return int(diff.get("lines_added", 0)) + int(diff.get("lines_removed", 0))

    def public(self) -> dict:
        verdict = self.verdict or {}
        return {
            "candidate": self.candidate.public(),
            "lane": self.lane.__dict__,
            "status": self.status,
            "reason": self.reason,
            "eligible": self.eligible,
            "sandbox_id": self.sandbox_id,
            "summary": verdict.get("summary"),
            "tests": verdict.get("tests"),
            "diff": verdict.get("diff"),
            "policy": verdict.get("policy"),
            "tamper": verdict.get("tamper"),
            "audit": self.audit,
            "timings": self.timings,
        }
