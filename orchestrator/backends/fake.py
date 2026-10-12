"""In-memory backend for tests and UI development. It never runs code; verdicts are scripted."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

from models import Lane

from backends.base import EgressAudit, ExecOutcome, SandboxBackend, SandboxRef

DEFAULT_VERDICT = {
    "status": "passed",
    "reason": "All 12 tests passed",
    "summary": "12/12 tests passed",
    "compile": {"ok": True, "errors": []},
    "tests": {"total": 12, "passed": 12, "failed": 0, "errors": 0, "skipped": 0, "failures": []},
    "policy": {"findings": [], "high": 0, "medium": 0},
    "tamper": {"modified_protected": [], "out_of_scope": []},
    "diff": {"files_changed": 1, "lines_added": 3, "lines_removed": 2, "text": ""},
    "phases": [{"name": "compile", "start_ms": 0, "duration_ms": 5}, {"name": "test", "start_ms": 5, "duration_ms": 5}],
    "duration_ms": 10,
}


class FakeBackend(SandboxBackend):
    name = "fake"

    def __init__(self, scripted: dict[str, dict] | None = None, *, healthy: bool = True, delay_s: float = 0.0):
        self.scripted = scripted or {}
        self.healthy = healthy
        self.delay_s = delay_s
        self.files: dict[str, dict[str, bytes]] = {}
        self.created: list[SandboxRef] = []
        self.deleted: list[str] = []

    async def create(self, *, run_id: str, name: str, lane: Lane, labels: dict[str, str],
                     environment: dict[str, str]) -> SandboxRef:
        await asyncio.sleep(self.delay_s)
        ref = SandboxRef(id=f"fake-{len(self.created) + 1}-{name}", labels=labels)
        self.created.append(ref)
        self.files[ref.id] = {}
        return ref

    async def exec(self, ref: SandboxRef, command: str, *, timeout_s: int = 120) -> ExecOutcome:
        files = self.files[ref.id]
        if " health" in command:
            return ExecOutcome(0 if self.healthy else 1, json.dumps({
                "ok": self.healthy,
                "checks": [{"name": "egress-locked", "ok": True, "detail": "fake", "required": True}],
                "network": {"locked": True, "http_code": "000"},
                "duration_ms": 1,
            }), "")
        if " verify " in command:
            meta = json.loads(files["/work/candidate/candidate.json"])
            script = self.scripted.get(meta["id"], {})
            verdict = {**DEFAULT_VERDICT, "candidate_id": meta["id"], **script.get("verdict", {}),
                       "finished_at": datetime.now(UTC).isoformat()}
            files["/work/candidate/verdict.json"] = json.dumps(verdict).encode()
            files["/work/candidate/status.json"] = json.dumps({"state": "done", "phase": "done"}).encode()
            return ExecOutcome(0, "", "")
        if command.startswith("cat /work/candidate/status.json"):
            return ExecOutcome(0, files.get("/work/candidate/status.json", b"").decode(), "")
        return ExecOutcome(0, "", "")

    async def write_file(self, ref: SandboxRef, path: str, data: bytes) -> None:
        self.files[ref.id][path] = data

    async def read_file(self, ref: SandboxRef, path: str) -> bytes:
        return self.files[ref.id][path]

    async def egress_audit(self, ref: SandboxRef) -> EgressAudit:
        meta = json.loads(self.files[ref.id]["/work/candidate/candidate.json"])
        denied = self.scripted.get(meta["id"], {}).get("denied", [])
        now = datetime.now(UTC).isoformat()
        return EgressAudit(supported=True, denied=denied, last_updated=now, complete_through=now)

    async def delete(self, ref: SandboxRef) -> None:
        self.deleted.append(ref.id)

    def describe(self) -> dict:
        return {"name": self.name, "title": "Fake backend (tests)",
                "images": {"primed": {"id": "fake", "name": "fake"}}}
