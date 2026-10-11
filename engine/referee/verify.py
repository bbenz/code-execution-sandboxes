"""verify: apply a candidate to a pristine copy of the target, then build, test, lint, and judge it."""

from __future__ import annotations

import difflib
import json
import shutil
from pathlib import Path

from referee import VERSION, paths, policy
from referee.targets import Target, load_target, safe_relative_path
from referee.toolchains import TOOLCHAINS, BuildResult, PhaseClock
from referee.util import StatusReporter, utc_now, write_json_atomic

MAX_FILES = 20
MAX_FILE_BYTES = 256 * 1024


class CandidateError(Exception):
    """The candidate itself is malformed (bad paths, too large, unreadable)."""


def read_candidate(candidate_dir: Path) -> tuple[dict, dict[str, str]]:
    meta_path = candidate_dir / "candidate.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.is_file() else {}
    files_dir = candidate_dir / "files"
    files: dict[str, str] = {}
    if files_dir.is_dir():
        for path in sorted(files_dir.rglob("*")):
            if path.is_symlink():
                raise CandidateError(f"Symlinks are not allowed: {path.name}")
            if not path.is_file():
                continue
            rel = safe_relative_path(path.relative_to(files_dir).as_posix())
            if path.stat().st_size > MAX_FILE_BYTES:
                raise CandidateError(f"{rel} is larger than {MAX_FILE_BYTES // 1024} KiB")
            files[rel] = path.read_text(encoding="utf-8", errors="replace")
    if len(files) > MAX_FILES:
        raise CandidateError(f"Candidate changes {len(files)} files (limit {MAX_FILES})")
    return meta, files


def diff_against(root: Path, files: dict[str, str]) -> tuple[str, dict]:
    chunks: list[str] = []
    added = removed = changed = 0
    for rel, new in sorted(files.items()):
        old_path = root / rel
        old = old_path.read_text(encoding="utf-8") if old_path.is_file() else ""
        if old == new:
            continue
        changed += 1
        lines = list(difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True),
                                          fromfile=f"a/{rel}", tofile=f"b/{rel}", n=2))
        added += sum(1 for line in lines if line.startswith("+") and not line.startswith("+++"))
        removed += sum(1 for line in lines if line.startswith("-") and not line.startswith("---"))
        chunks.append("".join(line if line.endswith("\n") else line + "\n" for line in lines))
    return "".join(chunks), {"files_changed": changed, "lines_added": added, "lines_removed": removed}


def judge(target: Target, files: dict[str, str], tampered: list[str], findings: list[dict],
          build: BuildResult, stats: dict) -> tuple[str, str, str]:
    """Return (status, reason, summary). Tests passing is necessary, not sufficient."""
    tests = build.tests
    summary = f"{tests.passed}/{tests.total} tests passed" if tests else "no test results"
    high = [finding for finding in findings if finding["severity"] == "high"]
    if not build.compiled:
        summary = "timed out" if build.timed_out else "does not compile"
    if stats["files_changed"] == 0:
        return "failed", "The candidate didn't change anything", "no changes"
    if tampered:
        return "disqualified", f"Modified protected files: {', '.join(tampered)}", summary
    if high:
        first = high[0]
        return "disqualified", f"Policy: {first['description']} ({first['file']}:{first['line']})", summary
    if build.timed_out:
        return "failed", "The build timed out", summary
    if not build.compiled:
        return "failed", build.compile_errors[0] if build.compile_errors else "The build failed", summary
    if tests is None or tests.total == 0:
        return "failed", "No tests ran", summary
    if "report-mismatch" in build.notes:
        return "failed", "Test reports don't match the build output", summary
    if not tests.all_passed:
        failing = tests.failed + tests.errors
        first = tests.failures[0]["name"].rsplit(".", 1)[-1] if tests.failures else ""
        plural = "s" if failing != 1 else ""
        return "failed", f"{failing} failing test{plural}" + (f", e.g. {first}" if first else ""), summary
    return "passed", f"All {tests.total} tests passed", summary


def verify(target_name: str, candidate_dir: Path, *, out_path: Path | None, status_path: Path | None,
           offline: bool = True, insecure_tls: bool = False, timeout_s: int = 600) -> dict:
    status = StatusReporter(status_path)
    status.update("preparing", "copying the pristine project")
    target = load_target(target_name)
    meta, files = read_candidate(candidate_dir)

    run_dir = paths.WORK_DIR / "run"
    shutil.rmtree(run_dir, ignore_errors=True)
    shutil.copytree(target.root, run_dir, ignore=shutil.ignore_patterns("target.json"))
    log_path = run_dir / ".referee" / "build.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    # Judge the change itself before running anything: tampering, scope, and the diff.
    tampered = [rel for rel in files if target.is_protected(rel)]
    out_of_scope = [rel for rel in files if rel not in target.editable and rel not in tampered]
    diff_text, stats = diff_against(target.root, files)
    for rel, content in files.items():
        destination = run_dir / rel
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8")

    clock = PhaseClock(status)
    clock.start("policy", "static scan of changed files")
    findings = policy.scan(target.language, files)
    clock.stop()

    toolchain = TOOLCHAINS[target.language]
    build = toolchain.build_and_test(run_dir, offline=offline, insecure_tls=insecure_tls, clock=clock,
                                     log_path=log_path, timeout_s=timeout_s, changed=list(files))
    lint = list(build.lint)
    if build.compiled:
        clock.start("lint", f"{target.language} linters")
        lint += toolchain.lint(run_dir, list(files))
        clock.stop()

    verdict_status, reason, summary = judge(target, files, tampered, findings, build, stats)
    verdict = {
        "engine_version": VERSION,
        "target": target.name,
        "language": target.language,
        "candidate_id": meta.get("id"),
        "strategy": meta.get("strategy"),
        "status": verdict_status,
        "reason": reason,
        "summary": summary,
        "offline": offline,
        "compile": {"ok": build.compiled, "errors": build.compile_errors},
        "tests": build.tests.to_dict() if build.tests else None,
        "lint": lint[:20],
        "policy": {
            "findings": findings,
            "high": sum(1 for finding in findings if finding["severity"] == "high"),
            "medium": sum(1 for finding in findings if finding["severity"] == "medium"),
        },
        "tamper": {"modified_protected": tampered, "out_of_scope": out_of_scope},
        "diff": {**stats, "text": diff_text[:16000]},
        "phases": clock.phases,
        "build": {"exit_code": build.exit_code, "timed_out": build.timed_out, "notes": build.notes,
                  "tail": build.tail[-25:]},
        "duration_ms": status.elapsed_ms(),
        "finished_at": utc_now(),
    }
    if out_path:
        write_json_atomic(out_path, verdict)
    status.update("done", f"{verdict_status}: {reason}", state="done")
    return verdict


def error_verdict(target_name: str, reason: str) -> dict:
    return {
        "engine_version": VERSION,
        "target": target_name,
        "status": "error",
        "reason": reason,
        "summary": "engine error",
        "finished_at": utc_now(),
    }
