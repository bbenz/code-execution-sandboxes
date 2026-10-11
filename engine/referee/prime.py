"""prime: fill dependency and build caches once, so every candidate can build offline in seconds."""

from __future__ import annotations

import time

from referee import paths
from referee.targets import list_targets, load_target
from referee.toolchains import TOOLCHAINS
from referee.util import utc_now, write_json_atomic


def _dir_size_mb(path) -> float:
    if not path.exists():
        return 0.0
    return round(sum(item.stat().st_size for item in path.rglob("*") if item.is_file()) / 2**20, 1)


def prime(target_names: list[str] | None = None, *, insecure_tls: bool = False, timeout_s: int = 1200) -> dict:
    started = time.monotonic()
    targets = [load_target(name) for name in target_names] if target_names else \
        [target for target in list_targets() if target.primable]
    log_path = paths.WORK_DIR / "prime.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    results = []
    for target in targets:
        result = TOOLCHAINS[target.language].prime(target, insecure_tls=insecure_tls, timeout_s=timeout_s,
                                                   log_path=log_path)
        marker = {"target": target.name, "primed_at": utc_now(), **{k: v for k, v in result.items() if k != "tail"}}
        if result["offline_ok"]:
            write_json_atomic(paths.PRIMED_DIR / f"{target.name}.json", marker)
        results.append({"target": target.name, **result})
    return {
        "ok": bool(results) and all(result["offline_ok"] for result in results),
        "targets": results,
        "maven_repo_mb": _dir_size_mb(paths.MAVEN_REPO),
        "duration_ms": int((time.monotonic() - started) * 1000),
        "log": str(log_path),
    }
