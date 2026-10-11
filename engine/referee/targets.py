"""Target projects baked into the engine image (pristine source + metadata)."""

from __future__ import annotations

import fnmatch
import json
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from referee import paths


@dataclass(frozen=True)
class Target:
    name: str
    language: str
    title: str
    issue: str
    root: Path
    editable: list[str] = field(default_factory=list)
    protected: list[str] = field(default_factory=list)
    context: list[str] = field(default_factory=list)
    primable: bool = False

    def is_protected(self, rel_path: str) -> bool:
        return any(fnmatch.fnmatchcase(rel_path, pattern) for pattern in self.protected)

    def describe(self) -> dict:
        return {
            "name": self.name,
            "language": self.language,
            "title": self.title,
            "issue": self.issue,
            "editable": self.editable,
            "protected": self.protected,
            "context": self.context,
            "primable": self.primable,
        }


def load_target(name: str, targets_dir: Path | None = None) -> Target:
    root = (targets_dir or paths.TARGETS_DIR) / name
    meta_path = root / "target.json"
    if not meta_path.is_file():
        raise FileNotFoundError(f"Unknown target '{name}' (no {meta_path})")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    return Target(
        name=meta["name"],
        language=meta["language"],
        title=meta.get("title", name),
        issue=meta.get("issue", ""),
        root=root,
        editable=list(meta.get("editable", [])),
        protected=list(meta.get("protected", [])),
        context=list(meta.get("context", [])),
        primable=bool(meta.get("primable", False)),
    )


def list_targets(targets_dir: Path | None = None) -> list[Target]:
    base = targets_dir or paths.TARGETS_DIR
    if not base.is_dir():
        return []
    return [load_target(p.name, base) for p in sorted(base.iterdir()) if (p / "target.json").is_file()]


def safe_relative_path(raw: str) -> str:
    """Normalize a candidate-supplied path and reject anything that escapes the project."""
    if not raw or "\\" in raw or raw.startswith("/") or ":" in raw:
        raise ValueError(f"Invalid candidate path: {raw!r}")
    parts = PurePosixPath(raw).parts
    if any(part in ("..", ".", "") for part in parts):
        raise ValueError(f"Invalid candidate path: {raw!r}")
    return PurePosixPath(*parts).as_posix()
