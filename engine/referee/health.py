"""health: the probe the orchestrator runs before it trusts a sandbox with a candidate."""

from __future__ import annotations

import json
import os
import shutil
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from referee import VERSION, paths
from referee.targets import list_targets
from referee.toolchains import TOOLCHAINS
from referee.util import run_quick

DEFAULT_PROBE_URL = "https://example.com/"


def network_probe(url: str = DEFAULT_PROBE_URL, timeout_s: int = 5) -> dict:
    """Try one outbound HTTPS request. A sandbox for untrusted code should NOT reach it."""
    code, output = run_quick(
        ["curl", "-sk", "-o", os.devnull, "-w", "%{http_code}", "-m", str(timeout_s), url],
        timeout_s=timeout_s + 5,
    )
    http_code = output.strip()[-3:] if output.strip() else "000"
    reachable = http_code.isdigit() and 200 <= int(http_code) < 400
    return {"probe_url": url, "http_code": http_code, "curl_exit": code, "locked": not reachable}


def _workdir_check() -> tuple[bool, str]:
    try:
        paths.WORK_DIR.mkdir(parents=True, exist_ok=True)
        probe = paths.WORK_DIR / f".health-{uuid.uuid4().hex[:8]}"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        free_gib = shutil.disk_usage(paths.WORK_DIR).free / 2**30
        return free_gib >= 1, f"{paths.WORK_DIR} writable, {free_gib:.1f} GiB free"
    except OSError as error:
        return False, f"{paths.WORK_DIR} not writable: {error}"


def primed_markers() -> dict[str, dict | None]:
    markers: dict[str, dict | None] = {}
    for target in list_targets():
        if not target.primable:
            continue
        marker = paths.PRIMED_DIR / f"{target.name}.json"
        markers[target.name] = json.loads(marker.read_text(encoding="utf-8")) if marker.is_file() else None
    return markers


def health(*, expect_primed: bool = False, require_locked: bool = False,
           probe_url: str = DEFAULT_PROBE_URL) -> dict:
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=5) as pool:
        version_futures = {name: pool.submit(toolchain.versions) for name, toolchain in TOOLCHAINS.items()}
        network_future = pool.submit(network_probe, probe_url)
        workdir_future = pool.submit(_workdir_check)
        versions: dict[str, str] = {}
        for future in version_futures.values():
            versions.update(future.result())
        network = network_future.result()
        workdir_ok, workdir_detail = workdir_future.result()

    targets = [target.name for target in list_targets()]
    markers = primed_markers()
    checks = [
        {"name": "java", "ok": bool(versions.get("java")), "detail": versions.get("java", ""), "required": True},
        {"name": "maven", "ok": bool(versions.get("maven")), "detail": versions.get("maven", ""), "required": True},
        {"name": "go", "ok": bool(versions.get("go")), "detail": versions.get("go", ""), "required": True},
        {"name": "python", "ok": bool(versions.get("pytest")),
         "detail": f"Python {versions.get('python', '?')}, {versions.get('pytest', 'pytest missing')}",
         "required": True},
        {"name": "targets", "ok": bool(targets), "detail": ", ".join(targets) or "none", "required": True},
        {"name": "workdir", "ok": workdir_ok, "detail": workdir_detail, "required": True},
        {"name": "primed-cache", "ok": bool(markers) and all(markers.values()),
         "detail": ", ".join(f"{name}: {'primed' if marker else 'cold'}" for name, marker in markers.items()),
         "required": expect_primed},
        {"name": "egress-locked", "ok": network["locked"],
         "detail": f"{probe_url} -> HTTP {network['http_code']}", "required": require_locked},
    ]
    return {
        "ok": all(check["ok"] for check in checks if check["required"]),
        "engine_version": VERSION,
        "checks": checks,
        "versions": versions,
        "primed": markers,
        "network": network,
        "duration_ms": int((time.monotonic() - started) * 1000),
    }
