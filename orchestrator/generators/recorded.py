"""Pre-recorded candidates: a deterministic race for live demos (and the fallback when no model is available)."""

from __future__ import annotations

import json
from pathlib import Path

from catalog import TargetInfo
from models import Candidate, CandidateFile

from generators.base import CandidateGenerator


class RecordedGenerator(CandidateGenerator):
    name = "recorded"

    def __init__(self, recorded_dir: Path):
        self.recorded_dir = recorded_dir

    def manifest(self, target_name: str) -> dict:
        path = self.recorded_dir / target_name / "manifest.json"
        if not path.is_file():
            return {"target": target_name, "candidates": []}
        return json.loads(path.read_text(encoding="utf-8"))

    def load(self, target_name: str, entry: dict) -> Candidate:
        files_dir = self.recorded_dir / target_name / entry["id"] / "files"
        files = [
            CandidateFile(path=path.relative_to(files_dir).as_posix(), content=path.read_text(encoding="utf-8"))
            for path in sorted(files_dir.rglob("*")) if path.is_file()
        ]
        return Candidate(
            id=entry["id"],
            wave=int(entry["wave"]),
            strategy=entry["strategy"],
            author=entry.get("author", "recorded"),
            explanation=entry.get("explanation", ""),
            files=files,
        )

    async def generate(self, target: TargetInfo, *, wave: int, count: int, feedback: str | None) -> list[Candidate]:
        manifest = self.manifest(target.name)
        return [self.load(target.name, entry) for entry in manifest["candidates"] if int(entry["wave"]) == wave]

    def by_id(self, target_name: str, candidate_id: str) -> Candidate | None:
        for entry in self.manifest(target_name)["candidates"]:
            if entry["id"] == candidate_id:
                return self.load(target_name, entry)
        return None

    def injected(self, target_name: str) -> list[Candidate]:
        """Candidates that join a live LLM wave, such as one from a prompt-injected agent."""
        manifest = self.manifest(target_name)
        return [self.load(target_name, {**entry, "wave": 1}) for entry in manifest["candidates"]
                if entry.get("inject_with_llm")]

    def benchmark_candidate(self, target_name: str) -> Candidate | None:
        candidate_id = self.manifest(target_name).get("benchmark")
        return self.by_id(target_name, candidate_id) if candidate_id else None

    def has_recordings(self, target_name: str) -> bool:
        return bool(self.manifest(target_name)["candidates"])
