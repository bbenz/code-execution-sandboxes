"""Target projects, read from the same folder that is baked into the engine image."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass
class TargetInfo:
    name: str
    language: str
    title: str
    issue: str
    editable: list[str]
    protected: list[str]
    context: list[str]
    root: Path

    def read(self, rel_path: str) -> str:
        return (self.root / rel_path).read_text(encoding="utf-8")

    def public(self) -> dict:
        return {
            "name": self.name,
            "language": self.language,
            "title": self.title,
            "issue": self.issue,
            "editable": self.editable,
            "context": self.context,
        }


class TargetCatalog:
    def __init__(self, targets_dir: Path):
        self.targets_dir = targets_dir

    def all(self) -> list[TargetInfo]:
        if not self.targets_dir.is_dir():
            return []
        return [self.get(path.name) for path in sorted(self.targets_dir.iterdir()) if (path / "target.json").is_file()]

    def get(self, name: str) -> TargetInfo:
        root = self.targets_dir / name
        meta_path = root / "target.json"
        if not meta_path.is_file():
            raise KeyError(f"Unknown target: {name}")
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        return TargetInfo(
            name=meta["name"],
            language=meta["language"],
            title=meta.get("title", name),
            issue=meta.get("issue", ""),
            editable=list(meta.get("editable", [])),
            protected=list(meta.get("protected", [])),
            context=list(meta.get("context", [])),
            root=root,
        )
