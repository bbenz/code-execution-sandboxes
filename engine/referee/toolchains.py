"""Language toolchains: how the engine builds, tests, lints, and primes a target in each language."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from referee import paths
from referee.reports import (
    TestSummary,
    parse_go_test_json,
    parse_junit_dir,
    parse_junit_file,
    surefire_console_totals,
)
from referee.targets import Target
from referee.util import StatusReporter, run_quick, run_streaming


class PhaseClock:
    """Records named phases (compile, test, lint, ...) as offsets from the start of a verify run."""

    def __init__(self, status: StatusReporter):
        self.status = status
        self.phases: list[dict] = []
        self._current: dict | None = None

    @property
    def current(self) -> str | None:
        return self._current["name"] if self._current else None

    def start(self, name: str, detail: str = "") -> None:
        self.stop()
        self._current = {"name": name, "detail": detail, "start_ms": self.status.elapsed_ms()}
        self.status.update(name, detail)

    def stop(self) -> None:
        if self._current:
            self._current["duration_ms"] = self.status.elapsed_ms() - self._current["start_ms"]
            self.phases.append(self._current)
            self._current = None


@dataclass
class BuildResult:
    compiled: bool
    tests: TestSummary | None
    compile_errors: list[str] = field(default_factory=list)
    lint: list[dict] = field(default_factory=list)
    timed_out: bool = False
    exit_code: int = 0
    tail: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _exe(name: str) -> str:
    return shutil.which(name) or name


def _relative(path_text: str, run_dir: Path) -> str:
    try:
        return Path(path_text).resolve().relative_to(run_dir.resolve()).as_posix()
    except (ValueError, OSError):
        return path_text.replace("\\", "/")


def _fresh_copy(target: Target, name: str) -> Path:
    destination = paths.WORK_DIR / name
    shutil.rmtree(destination, ignore_errors=True)
    shutil.copytree(target.root, destination, ignore=shutil.ignore_patterns("target.json"))
    return destination


# ── Java (Maven) ─────────────────────────────────────────────────────────────

PLUGIN_LINE = re.compile(r"--- (?:maven-)?([a-z][a-z-]*?)(?:-plugin)?:[\w.\-]+:([\w-]+)")
JAVA_ERROR = re.compile(r"^\[ERROR\] (.+?\.java):\[(\d+),(\d+)\] (.*)$")
JAVA_WARNING = re.compile(r"^\[WARNING\] (.+?\.java):\[(\d+),(\d+)\] (.*)$")


class JavaToolchain:
    language = "java"

    def maven(self, *, offline: bool, insecure_tls: bool = False) -> list[str]:
        cmd = [
            _exe("mvn"), "-B", "-ntp", "-Dstyle.color=never",
            f"-Dmaven.repo.local={paths.MAVEN_REPO}",
            "-Dmaven.test.failure.ignore=true",
        ]
        if offline:
            cmd.append("--offline")
        if insecure_tls:
            # The sandbox egress proxy terminates TLS with a certificate custom images don't trust.
            # Only the trusted, allowlisted primer uses this; candidates always build offline.
            cmd.append("-Daether.connector.https.securityMode=insecure")
        return cmd

    def env(self) -> dict[str, str]:
        env = dict(os.environ)
        env.setdefault("MAVEN_OPTS", "-XX:TieredStopAtLevel=1 -XX:+UseSerialGC")
        return env

    def build_and_test(self, run_dir: Path, *, offline: bool, clock: PhaseClock, log_path: Path,
                       timeout_s: int, changed: list[str], insecure_tls: bool = False) -> BuildResult:
        clock.start("compile", "javac via Maven")

        def on_line(line: str) -> None:
            match = PLUGIN_LINE.search(line)
            if match and match.group(1) == "surefire" and clock.current != "test":
                clock.start("test", "JUnit via Maven Surefire")

        result = run_streaming(self.maven(offline=offline, insecure_tls=insecure_tls) + ["test"], cwd=run_dir,
                               env=self.env(), on_line=on_line, log_path=log_path, timeout_s=timeout_s)
        clock.stop()
        log_lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        errors, lint = [], []
        for line in log_lines:
            if match := JAVA_ERROR.match(line):
                errors.append(f"{_relative(match.group(1), run_dir)}:{match.group(2)}: {match.group(4)}")
            elif match := JAVA_WARNING.match(line):
                rel = _relative(match.group(1), run_dir)
                if rel in changed:
                    lint.append({"tool": "javac -Xlint", "file": rel, "line": int(match.group(2)),
                                 "message": match.group(4)})
        compiled = result.exit_code == 0 and not result.timed_out
        if not compiled and not errors:
            errors = [line for line in log_lines if line.startswith("[ERROR]")][:10]
        tests = parse_junit_dir(run_dir / "target" / "surefire-reports") if compiled else None
        notes = []
        totals = surefire_console_totals(log_lines)
        if tests is not None and totals and \
                (totals[0], totals[1] + totals[2]) != (tests.total, tests.failed + tests.errors):
            notes.append("report-mismatch")
        return BuildResult(compiled, tests, errors[:10], lint, result.timed_out, result.exit_code,
                           result.tail[-40:], notes)

    def lint(self, run_dir: Path, changed: list[str]) -> list[dict]:
        return []  # javac -Xlint warnings are collected during the build

    def prime(self, target: Target, *, insecure_tls: bool, timeout_s: int, log_path: Path) -> dict:
        project = _fresh_copy(target, f"prime-{target.name}")
        online = run_streaming(self.maven(offline=False, insecure_tls=insecure_tls) + ["test"], cwd=project,
                               env=self.env(), log_path=log_path, timeout_s=timeout_s)
        offline = run_streaming(self.maven(offline=True) + ["test"], cwd=project, env=self.env(),
                                log_path=log_path, timeout_s=timeout_s)
        shutil.rmtree(project, ignore_errors=True)
        return {
            "download_ok": online.exit_code == 0,
            "download_ms": online.duration_ms,
            "offline_ok": offline.exit_code == 0,
            "offline_build_ms": offline.duration_ms,
            "tail": (offline.tail if online.exit_code == 0 else online.tail)[-15:],
        }

    def versions(self) -> dict[str, str]:
        java_code, java_out = run_quick([_exe("java"), "-version"])
        mvn_code, mvn_out = run_quick([_exe("mvn"), "-v"])
        return {
            "java": java_out.splitlines()[0] if java_code == 0 and java_out else "",
            "maven": mvn_out.splitlines()[0] if mvn_code == 0 and mvn_out else "",
        }


# ── Python (pytest + ruff) ───────────────────────────────────────────────────

class PythonToolchain:
    language = "python"

    def env(self) -> dict[str, str]:
        env = dict(os.environ)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        return env

    def build_and_test(self, run_dir: Path, *, offline: bool, clock: PhaseClock, log_path: Path,
                       timeout_s: int, changed: list[str], insecure_tls: bool = False) -> BuildResult:
        clock.start("compile", "py_compile")
        sources = [path for path in changed if path.endswith(".py")]
        if sources:
            code, output = run_quick([sys.executable, "-m", "py_compile", *sources], cwd=run_dir, env=self.env(),
                                     timeout_s=60)
            if code != 0:
                clock.stop()
                errors = [line for line in output.splitlines() if line.strip()][-10:]
                return BuildResult(False, None, errors, exit_code=code)
        clock.start("test", "pytest")
        junit = run_dir / ".referee" / "pytest.xml"
        junit.parent.mkdir(parents=True, exist_ok=True)
        result = run_streaming([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", f"--junitxml={junit}"],
                               cwd=run_dir, env=self.env(), log_path=log_path, timeout_s=timeout_s)
        clock.stop()
        tests = parse_junit_file(junit) if junit.is_file() else None
        return BuildResult(True, tests, [], [], result.timed_out, result.exit_code, result.tail[-40:])

    def lint(self, run_dir: Path, changed: list[str]) -> list[dict]:
        sources = [path for path in changed if path.endswith(".py")]
        if not sources:
            return []
        try:
            proc = subprocess.run([sys.executable, "-m", "ruff", "check", "--isolated", "--no-cache",
                                   "--output-format", "json", *sources],
                                  cwd=run_dir, capture_output=True, text=True, timeout=60)
            items = json.loads(proc.stdout or "[]")
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return []
        return [{"tool": "ruff", "file": _relative(item.get("filename", ""), run_dir),
                 "line": (item.get("location") or {}).get("row"),
                 "message": f"{item.get('code')}: {item.get('message')}"} for item in items[:20]]

    def prime(self, target: Target, *, insecure_tls: bool, timeout_s: int, log_path: Path) -> dict:
        return {"download_ok": True, "download_ms": 0, "offline_ok": True, "offline_build_ms": 0, "tail": []}

    def versions(self) -> dict[str, str]:
        _, pytest_out = run_quick([sys.executable, "-m", "pytest", "--version"])
        _, ruff_out = run_quick([sys.executable, "-m", "ruff", "--version"])
        return {
            "python": sys.version.split()[0],
            "pytest": pytest_out.strip().splitlines()[0] if pytest_out.strip() else "",
            "ruff": ruff_out.strip().splitlines()[0] if ruff_out.strip() else "",
        }


# ── Go (go test + go vet) ────────────────────────────────────────────────────

class GoToolchain:
    language = "go"

    def env(self, *, offline: bool) -> dict[str, str]:
        env = dict(os.environ)
        env.setdefault("GOTOOLCHAIN", "local")
        env.setdefault("GOFLAGS", "-mod=mod")
        env["CGO_ENABLED"] = "0"
        if offline:
            env["GOPROXY"] = "off"
        return env

    def build_and_test(self, run_dir: Path, *, offline: bool, clock: PhaseClock, log_path: Path,
                       timeout_s: int, changed: list[str], insecure_tls: bool = False) -> BuildResult:
        clock.start("compile", "go build")
        events: list[str] = []

        def on_line(line: str) -> None:
            if line.startswith("{"):
                events.append(line)
                if '"Action":"run"' in line and clock.current != "test":
                    clock.start("test", "go test")

        result = run_streaming([_exe("go"), "test", "-json", "-count=1", "./..."], cwd=run_dir,
                               env=self.env(offline=offline), on_line=on_line, log_path=log_path,
                               timeout_s=timeout_s)
        clock.stop()
        tests, build_errors = parse_go_test_json(events)
        if tests.total == 0 and result.exit_code != 0:
            errors = build_errors or [line for line in result.tail if line and not line.startswith("{")][-10:]
            return BuildResult(False, None, errors, [], result.timed_out, result.exit_code, result.tail[-40:])
        return BuildResult(True, tests, [], [], result.timed_out, result.exit_code, result.tail[-40:])

    def lint(self, run_dir: Path, changed: list[str]) -> list[dict]:
        code, output = run_quick([_exe("go"), "vet", "./..."], cwd=run_dir, env=self.env(offline=True), timeout_s=120)
        findings = []
        for line in output.splitlines():
            match = re.match(r"^(?:\./)?([^:\s]+\.go):(\d+):(?:\d+:)?\s*(.*)$", line.strip())
            if match:
                findings.append({"tool": "go vet", "file": match.group(1), "line": int(match.group(2)),
                                 "message": match.group(3)})
        return findings[:20]

    def prime(self, target: Target, *, insecure_tls: bool, timeout_s: int, log_path: Path) -> dict:
        project = _fresh_copy(target, f"prime-{target.name}")
        run = run_streaming([_exe("go"), "test", "-count=1", "./..."], cwd=project, env=self.env(offline=True),
                            log_path=log_path, timeout_s=timeout_s)
        shutil.rmtree(project, ignore_errors=True)
        ok = "[build failed]" not in "\n".join(run.tail)
        return {"download_ok": ok, "download_ms": run.duration_ms, "offline_ok": ok,
                "offline_build_ms": run.duration_ms, "tail": run.tail[-15:]}

    def versions(self) -> dict[str, str]:
        code, output = run_quick([_exe("go"), "version"])
        return {"go": output.strip() if code == 0 else ""}


TOOLCHAINS = {"java": JavaToolchain(), "python": PythonToolchain(), "go": GoToolchain()}
