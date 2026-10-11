"""Small helpers for JSON files and processes."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def write_json_atomic(path: Path, data: dict) -> None:
    """Write JSON so a concurrent reader never sees a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


class StatusReporter:
    """Writes a status file the orchestrator polls to show live progress."""

    def __init__(self, path: Path | None):
        self.path = path
        self.started = time.monotonic()

    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self.started) * 1000)

    def update(self, phase: str, detail: str = "", state: str = "running") -> None:
        if not self.path:
            return
        write_json_atomic(self.path, {
            "state": state,
            "phase": phase,
            "detail": detail,
            "elapsed_ms": self.elapsed_ms(),
            "updated_at": utc_now(),
        })


class ProcessResult:
    def __init__(self, exit_code: int, tail: list[str], timed_out: bool, duration_ms: int):
        self.exit_code = exit_code
        self.tail = tail
        self.timed_out = timed_out
        self.duration_ms = duration_ms


def run_streaming(
    cmd: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    on_line: Callable[[str], None] | None = None,
    log_path: Path | None = None,
    timeout_s: int = 600,
    tail_lines: int = 200,
) -> ProcessResult:
    """Run a command, stream its output line by line, and kill the whole process tree on timeout."""
    started = time.monotonic()
    tail: deque[str] = deque(maxlen=tail_lines)
    popen_kwargs: dict = {}
    if os.name == "posix":
        popen_kwargs["start_new_session"] = True
    proc = subprocess.Popen(
        cmd,
        cwd=str(cwd),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        **popen_kwargs,
    )
    timed_out = threading.Event()

    def _kill() -> None:
        timed_out.set()
        try:
            if os.name == "posix":
                os.killpg(proc.pid, signal.SIGKILL)
            else:
                proc.kill()
        except (ProcessLookupError, PermissionError, OSError):
            pass

    timer = threading.Timer(timeout_s, _kill)
    timer.start()
    log_file = log_path.open("a", encoding="utf-8") if log_path else None
    try:
        assert proc.stdout is not None
        for raw in proc.stdout:
            line = raw.rstrip("\r\n")
            tail.append(line)
            if log_file:
                log_file.write(line + "\n")
            if on_line:
                on_line(line)
        exit_code = proc.wait()
    finally:
        timer.cancel()
        if log_file:
            log_file.close()
    return ProcessResult(exit_code, list(tail), timed_out.is_set(), int((time.monotonic() - started) * 1000))


def run_quick(cmd: list[str], *, cwd: Path | None = None, timeout_s: int = 20, env: dict[str, str] | None = None):
    """Run a short command and return (exit_code, combined output). Never raises."""
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
            stdin=subprocess.DEVNULL,
        )
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except FileNotFoundError:
        return 127, f"{cmd[0]}: not found"
    except subprocess.TimeoutExpired:
        return 124, f"{cmd[0]}: timed out after {timeout_s}s"
