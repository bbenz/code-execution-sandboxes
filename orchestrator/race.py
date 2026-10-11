"""The loop: generate candidates, referee each one in its own sandbox, and keep going until one wins."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

import referee_client
import scoring
import telemetry
from backends.base import SandboxBackend, SandboxRef
from catalog import TargetInfo
from events import RunState
from generators.base import CandidateGenerator
from models import Candidate, Lane, Outcome
from settings import Settings

logger = logging.getLogger("ces.race")


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _slim(verdict: dict) -> dict:
    slim = {key: value for key, value in verdict.items() if key not in ("build",)}
    if isinstance(slim.get("diff"), dict):
        slim["diff"] = {**slim["diff"], "text": (slim["diff"].get("text") or "")[:8000]}
    return slim


class Race:
    def __init__(self, *, state: RunState, settings: Settings, backend: SandboxBackend, target: TargetInfo,
                 generator: CandidateGenerator | None = None, max_waves: int = 3, candidates_per_wave: int = 4,
                 extra_first_wave: list[Candidate] | None = None, poll_seconds: float = 1.5,
                 audit_poll_seconds: float = 3.0):
        self.state = state
        self.settings = settings
        self.backend = backend
        self.target = target
        self.generator = generator
        self.max_waves = max_waves
        self.candidates_per_wave = candidates_per_wave
        self.extra_first_wave = extra_first_wave or []
        self.poll_seconds = poll_seconds
        self.audit_poll_seconds = audit_poll_seconds
        self.semaphore = asyncio.Semaphore(settings.max_parallel)
        self.active_sandboxes = 0

    @property
    def run_id(self) -> str:
        return self.state.run_id

    # ── The loop ──────────────────────────────────────────────────────────────

    async def run(self) -> dict:
        started = time.monotonic()
        self.state.publish("run.started", kind="race", target=self.target.public(), generator=self.generator.name,
                           backend=self.backend.describe(), max_waves=self.max_waves)
        winner: Outcome | None = None
        outcomes: list[Outcome] = []
        error = None
        wave = 0
        with telemetry.span("race", **{"ces.run_id": self.run_id, "ces.target": self.target.name,
                                       "ces.generator": self.generator.name}) as race_span:
            await self.backend.prepare_run(self.run_id)
            try:
                feedback = None
                for wave in range(1, self.max_waves + 1):
                    with telemetry.span("wave", **{"ces.wave": wave}):
                        self.state.publish("wave.generating", wave=wave, generator=self.generator.name)
                        candidates = await self.generator.generate(self.target, wave=wave,
                                                                   count=self.candidates_per_wave, feedback=feedback)
                        if wave == 1:
                            candidates += self.extra_first_wave
                        if not candidates:
                            self.state.publish("log", level="warning", message=f"No candidates for wave {wave}")
                            break
                        self.state.publish("wave.started", wave=wave, candidates=[c.public() for c in candidates])
                        results = await asyncio.gather(*(self.referee(candidate, Lane()) for candidate in candidates))
                        outcomes += results
                        winner = scoring.pick_winner(results)
                        self.state.publish("wave.finished", wave=wave, counts=scoring.summarize(results),
                                           winner=winner.candidate.id if winner else None)
                        if winner:
                            break
                        feedback = scoring.feedback(results)
            except Exception as exc:  # e.g. the coding agent is unavailable
                logger.exception("Race %s failed", self.run_id)
                error = f"{type(exc).__name__}: {exc}"
                self.state.publish("log", level="error", message=error)
            finally:
                await self.backend.finish_run(self.run_id)
            telemetry.set_attributes(race_span, **{"ces.waves": wave,
                                                   "ces.winner": winner.candidate.id if winner else "none"})
        result = {
            "kind": "race",
            "winner": winner.public() if winner else None,
            "waves": wave,
            "sandboxes": len(outcomes),
            "elapsed_ms": _ms(started),
            "error": error,
        }
        self.state.publish("run.finished", **result)
        self.state.finish(result)
        return result

    async def benchmark(self, candidate: Candidate) -> dict:
        """Run the same known-good fix twice: cold (base image, downloads) and primed (offline)."""
        started = time.monotonic()
        lanes = [
            Lane(image="base", egress="registry", online=True, label="cold"),
            Lane(image="primed", egress="locked", online=False, label="primed"),
        ]
        runs = [(replace(candidate, id=f"{lane.label}-{candidate.id}", strategy=f"{lane.label}: {candidate.strategy}"),
                 lane) for lane in lanes]
        self.state.publish("run.started", kind="benchmark", target=self.target.public(), generator="recorded",
                           backend=self.backend.describe(), max_waves=1)
        with telemetry.span("benchmark", **{"ces.run_id": self.run_id, "ces.target": self.target.name}):
            await self.backend.prepare_run(self.run_id)
            try:
                self.state.publish("wave.started", wave=1, candidates=[c.public() for c, _ in runs],
                                   lanes={c.id: lane.label for c, lane in runs})
                results = await asyncio.gather(*(self.referee(c, lane) for c, lane in runs))
            finally:
                await self.backend.finish_run(self.run_id)
        result = {
            "kind": "benchmark",
            "lanes": [{"label": o.lane.label, "status": o.status, "reason": o.reason, "timings": o.timings,
                       "build_ms": sum(p.get("duration_ms", 0) for p in (o.verdict or {}).get("phases", [])
                                       if p.get("name") in ("compile", "test"))} for o in results],
            "elapsed_ms": _ms(started),
        }
        self.state.publish("run.finished", **result)
        self.state.finish(result)
        return result

    # ── One candidate, one sandbox ────────────────────────────────────────────

    async def referee(self, candidate: Candidate, lane: Lane) -> Outcome:
        outcome = Outcome(candidate=candidate, lane=lane)
        cid = candidate.id
        labels = {
            "ces-app": "referee",
            "ces-role": "candidate",
            "ces-run": self.run_id,
            "ces-wave": str(candidate.wave),
            "ces-candidate": cid[:60],
            "ces-tenant": self.settings.tenant,
        }
        environment = {"HONEYTOKEN": self.settings.honeytoken, "CES_RUN": self.run_id}
        with telemetry.span("candidate", **{"ces.candidate": cid, "ces.strategy": candidate.strategy,
                                            "ces.lane": lane.label or lane.image}) as candidate_span:
            async with self.semaphore:
                ref: SandboxRef | None = None
                try:
                    self._phase(cid, "creating", f"booting from the {lane.image} disk image")
                    started = time.monotonic()
                    with telemetry.span("sandbox.create", **{"ces.image": lane.image, "ces.egress": lane.egress}) as s:
                        ref = await self.backend.create(run_id=self.run_id, name=cid, lane=lane, labels=labels,
                                                        environment=environment)
                        telemetry.set_attributes(s, **{"sandbox.id": ref.id})
                    outcome.sandbox_id = ref.id
                    outcome.timings["create_ms"] = _ms(started)
                    self._capacity(+1)
                    self.state.publish("candidate.sandbox", id=cid, sandbox_id=ref.id,
                                       create_ms=outcome.timings["create_ms"])

                    await self._health_gate(outcome, ref)

                    self._phase(cid, "uploading", f"{len(candidate.files)} file(s)")
                    await referee_client.upload(self.backend, ref, candidate)

                    verify_started = datetime.now(UTC)
                    anchor_ns, started = time.time_ns(), time.monotonic()
                    with telemetry.span("referee.verify") as verify_span:
                        await referee_client.start_verify(
                            self.backend, ref, self.target.name, online=lane.online,
                            insecure_tls=lane.online and self.backend.name == "aca",
                            timeout_s=self.settings.verify_timeout_s)
                        await self._watch(cid, ref)
                        verdict = await referee_client.read_verdict(self.backend, ref)
                        telemetry.record_phases(verdict.get("phases", []), anchor_ns)
                        tests = verdict.get("tests") or {}
                        telemetry.set_attributes(verify_span, **{"ces.verdict": verdict.get("status"),
                                                                 "ces.tests.passed": tests.get("passed"),
                                                                 "ces.tests.total": tests.get("total")})
                    outcome.verdict = verdict
                    outcome.timings["verify_ms"] = _ms(started)
                    self.state.publish("candidate.verdict", id=cid, verdict=_slim(verdict))

                    self._phase(cid, "auditing", "reading the sandbox's egress decisions")
                    started = time.monotonic()
                    with telemetry.span("egress.audit"):
                        outcome.audit = await self._audit(ref, verdict, verify_started)
                    outcome.timings["audit_ms"] = _ms(started)
                    self.state.publish("candidate.audit", id=cid, audit=outcome.audit)
                    scoring.finalize(outcome)
                except Exception as exc:
                    logger.exception("Candidate %s failed", cid)
                    outcome.status = "error"
                    outcome.reason = f"{type(exc).__name__}: {exc}"[:300]
                finally:
                    if ref is not None:
                        self._phase(cid, "cleanup", "deleting the sandbox")
                        started = time.monotonic()
                        try:
                            with telemetry.span("sandbox.delete"):
                                await self.backend.delete(ref)
                        except Exception as exc:
                            logger.warning("Couldn't delete sandbox %s: %s", ref.id, exc)
                        outcome.timings["delete_ms"] = _ms(started)
                        self._capacity(-1)
            telemetry.set_attributes(candidate_span, **{"ces.status": outcome.status, "ces.reason": outcome.reason})
        self.state.publish("candidate.finished", id=cid, outcome=outcome.public())
        return outcome

    async def _health_gate(self, outcome: Outcome, ref: SandboxRef) -> None:
        """Like a startup probe: don't hand untrusted code to a sandbox until the engine proves it's ready."""
        self._phase(outcome.candidate.id, "health", "toolchains, caches, and network isolation")
        started = time.monotonic()
        with telemetry.span("referee.health"):
            health = await referee_client.health(
                self.backend, ref,
                expect_primed=outcome.lane.image == "primed",
                require_locked=outcome.lane.egress == "locked",
                probe_url=self.settings.probe_url,
            )
        outcome.health = health
        outcome.timings["health_ms"] = _ms(started)
        self.state.publish("candidate.health", id=outcome.candidate.id, ok=bool(health.get("ok")),
                           checks=health.get("checks", []), network=health.get("network"))
        if not health.get("ok"):
            failing = [check["name"] for check in health.get("checks", [])
                       if check.get("required") and not check.get("ok")]
            raise RuntimeError(f"Sandbox failed its health check: {', '.join(failing) or health.get('error', '?')}")

    async def _watch(self, cid: str, ref: SandboxRef) -> None:
        """Poll the referee's status file and forward phase changes (compile, test, lint) to the board."""
        deadline = time.monotonic() + self.settings.verify_timeout_s + 60
        last_phase = None
        while time.monotonic() < deadline:
            await asyncio.sleep(self.poll_seconds)
            status = await referee_client.read_status(self.backend, ref)
            if not status:
                continue
            phase = status.get("phase")
            if phase != last_phase and phase != "done":
                self._phase(cid, phase, status.get("detail", ""))
                last_phase = phase
            if status.get("state") in ("done", "error"):
                return
        raise TimeoutError("The referee didn't finish in time")

    async def _audit(self, ref: SandboxRef, verdict: dict, verify_started: datetime) -> dict:
        """Ask the platform what the candidate's code tried to reach. Tests passing isn't enough."""
        wait = verdict.get("status") == "passed"
        deadline = time.monotonic() + (self.settings.audit_seconds if wait else 0)
        finished = _parse_time(verdict.get("finished_at"))
        probe_host = urlsplit(self.settings.probe_url).hostname
        window_start = verify_started - timedelta(seconds=5)
        while True:
            audit = await self.backend.egress_audit(ref)
            if not audit.supported:
                return {"status": "unsupported", "denied": [], "note": "No egress audit for this lane"}
            attempts = [entry for entry in audit.denied if entry.get("host") != probe_host and (
                _parse_time(entry.get("timestamp")) is None or _parse_time(entry.get("timestamp")) >= window_start)]
            if attempts:
                return {"status": "violation", "denied": attempts[:10], "allowed": len(audit.allowed),
                        "last_updated": audit.last_updated}
            updated = _parse_time(audit.last_updated)
            covered = updated is not None and finished is not None and updated >= finished
            if covered or time.monotonic() >= deadline:
                return {"status": "clean", "denied": [], "allowed": len(audit.allowed),
                        "last_updated": audit.last_updated,
                        "note": "No outbound attempts" + ("" if covered else " recorded yet")}
            await asyncio.sleep(self.audit_poll_seconds)

    # ── Board updates ─────────────────────────────────────────────────────────

    def _phase(self, cid: str, phase: str, detail: str = "") -> None:
        self.state.publish("candidate.phase", id=cid, phase=phase, detail=detail)

    def _capacity(self, delta: int) -> None:
        self.active_sandboxes += delta
        self.state.publish("capacity", active=self.active_sandboxes, cpu=self.settings.candidate_cpu,
                           memory=self.settings.candidate_memory)
