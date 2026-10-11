#!/usr/bin/env python
"""Smoke test: one candidate through one sandbox, end to end.

Creates a sandbox from the primed disk image, health-checks it, verifies the known-good fix, reads the
egress audit, and deletes the sandbox. Use it to confirm a deployment (or the local Docker backend)
works before the live demo. Exits non-zero if the candidate doesn't pass or the sandbox isn't cleaned up.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from urllib.parse import urlsplit

ORCHESTRATOR = Path(__file__).resolve().parents[1] / "orchestrator"
sys.path.insert(0, str(ORCHESTRATOR))

import referee_client  # noqa: E402
import scoring  # noqa: E402
from backends import build_backend  # noqa: E402
from generators.recorded import RecordedGenerator  # noqa: E402
from models import Lane, Outcome  # noqa: E402
from settings import Settings  # noqa: E402


async def main() -> int:
    settings = Settings.from_env()
    backend = build_backend(settings)
    await backend.startup()
    if not backend.has_image("primed"):
        print("No primed disk image yet. Run scripts/prepare_images.py (or build it locally).", file=sys.stderr)
        return 2

    recorded = RecordedGenerator(settings.recorded_dir)
    candidate = recorded.benchmark_candidate("java-ratelimiter")
    outcome = Outcome(candidate=candidate, lane=Lane())
    run_id = "smoke"
    await backend.prepare_run(run_id)
    ref = None
    try:
        print(f"Creating a sandbox on the {backend.name} backend…")
        ref = await backend.create(run_id=run_id, name="smoke", lane=Lane(),
                                   labels={"ces-app": "referee", "ces-run": run_id, "ces-role": "smoke"},
                                   environment={"HONEYTOKEN": settings.honeytoken})
        print(f"  sandbox: {ref.id}")
        health = await referee_client.health(backend, ref, expect_primed=True, require_locked=True,
                                             probe_url=settings.probe_url)
        print(f"  health ok={health.get('ok')} network_locked={health.get('network', {}).get('locked')}")
        await referee_client.upload(backend, ref, candidate)
        await referee_client.start_verify(backend, ref, "java-ratelimiter", online=False, insecure_tls=False,
                                          timeout_s=settings.verify_timeout_s)
        for _ in range(240):
            status = await referee_client.read_status(backend, ref)
            if status and status.get("state") in ("done", "error"):
                break
            await asyncio.sleep(1.5)
        outcome.verdict = await referee_client.read_verdict(backend, ref)
        audit = await backend.egress_audit(ref)
        # The health probe deliberately tries the probe URL to prove the network is locked, so that
        # attempt shows up in the audit too. Exclude it; only candidate-driven attempts are violations.
        probe_host = urlsplit(settings.probe_url).hostname
        attempts = [entry for entry in audit.denied if entry.get("host") != probe_host]
        outcome.audit = {"status": "violation" if attempts else "clean", "denied": attempts}
        scoring.finalize(outcome)
        tests = outcome.verdict.get("tests", {})
        print(f"  verdict: {outcome.status} — {outcome.reason} ({tests.get('passed')}/{tests.get('total')} tests)")
    finally:
        if ref is not None:
            await backend.delete(ref)
        await backend.finish_run(run_id)

    ok = outcome.status == "passed"
    print("SMOKE TEST:", "PASS" if ok else f"FAIL ({outcome.status})")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
