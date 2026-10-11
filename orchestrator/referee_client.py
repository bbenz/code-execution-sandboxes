"""Talks to the referee engine inside any sandbox through the backend's exec and file APIs."""

from __future__ import annotations

import json
import shlex
from pathlib import PurePosixPath

from backends.base import SandboxBackend, SandboxRef
from models import Candidate

REFEREE = "/usr/local/bin/referee"
CANDIDATE_DIR = "/work/candidate"


def safe_relative_path(raw: str) -> str:
    """Reject candidate paths that could escape the project (absolute, .., backslashes, drive letters)."""
    if not raw or "\\" in raw or raw.startswith("/") or ":" in raw:
        raise ValueError(f"Invalid candidate path: {raw!r}")
    parts = PurePosixPath(raw).parts
    if any(part in ("..", ".", "") for part in parts):
        raise ValueError(f"Invalid candidate path: {raw!r}")
    return PurePosixPath(*parts).as_posix()


def _json_or_error(text: str, fallback: str) -> dict:
    try:
        return json.loads(text)
    except ValueError:
        return {"ok": False, "error": (fallback or text or "no output")[-800:]}


async def health(backend: SandboxBackend, ref: SandboxRef, *, expect_primed: bool, require_locked: bool,
                 probe_url: str) -> dict:
    flags = " --expect-primed" if expect_primed else ""
    flags += " --require-locked" if require_locked else ""
    result = await backend.exec(ref, f"{REFEREE} health{flags} --probe-url {shlex.quote(probe_url)}")
    return _json_or_error(result.stdout, result.stderr)


async def upload(backend: SandboxBackend, ref: SandboxRef, candidate: Candidate) -> None:
    meta = {"id": candidate.id, "wave": candidate.wave, "strategy": candidate.strategy, "author": candidate.author}
    await backend.write_file(ref, f"{CANDIDATE_DIR}/candidate.json", json.dumps(meta).encode("utf-8"))
    for file in candidate.files:
        rel = safe_relative_path(file.path)
        await backend.write_file(ref, f"{CANDIDATE_DIR}/files/{rel}", file.content.encode("utf-8"))


async def start_verify(backend: SandboxBackend, ref: SandboxRef, target: str, *, online: bool,
                       insecure_tls: bool, timeout_s: int) -> None:
    """Start `referee verify` in the background and return immediately; progress goes to status.json."""
    args = [
        REFEREE, "verify", "--target", target, "--candidate", CANDIDATE_DIR,
        "--out", f"{CANDIDATE_DIR}/verdict.json", "--status", f"{CANDIDATE_DIR}/status.json",
        "--timeout", str(timeout_s),
    ]
    if online:
        args.append("--online")
    if insecure_tls:
        args.append("--insecure-tls")
    command = " ".join(shlex.quote(arg) for arg in args)
    await backend.exec(
        ref,
        f"rm -f {CANDIDATE_DIR}/status.json {CANDIDATE_DIR}/verdict.json && "
        f"setsid nohup {command} > {CANDIDATE_DIR}/verify.log 2>&1 < /dev/null &",
    )


async def read_status(backend: SandboxBackend, ref: SandboxRef) -> dict | None:
    result = await backend.exec(ref, f"cat {CANDIDATE_DIR}/status.json 2>/dev/null || true")
    text = result.stdout.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


async def read_verdict(backend: SandboxBackend, ref: SandboxRef) -> dict:
    return json.loads((await backend.read_file(ref, f"{CANDIDATE_DIR}/verdict.json")).decode("utf-8"))


async def read_log_tail(backend: SandboxBackend, ref: SandboxRef, lines: int = 20) -> str:
    result = await backend.exec(ref, f"tail -n {lines} {CANDIDATE_DIR}/verify.log 2>/dev/null || true")
    return result.stdout
