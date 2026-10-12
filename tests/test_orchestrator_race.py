"""Orchestrator tests that drive a full race on the in-memory fake backend (no Docker, no Azure)."""

import asyncio
import sys
from pathlib import Path

ORCH = Path(__file__).resolve().parents[1] / "orchestrator"
sys.path.insert(0, str(ORCH))

from backends.base import EgressAudit  # noqa: E402
from backends.fake import FakeBackend  # noqa: E402
from catalog import TargetCatalog  # noqa: E402
from events import RunRegistry  # noqa: E402
from generators.recorded import RecordedGenerator  # noqa: E402
from models import Candidate, CandidateFile, Outcome  # noqa: E402
from race import Race  # noqa: E402
from scoring import finalize, pick_winner  # noqa: E402
from settings import Settings  # noqa: E402

TARGETS = Path(__file__).resolve().parents[1] / "engine" / "targets"
RECORDED = ORCH / "recorded"


def make_settings(**kw):
    return Settings(backend="fake", targets_dir=TARGETS, recorded_dir=RECORDED, audit_seconds=0,
                    max_parallel=6, honeytoken="ces-honeytoken-test", **kw)


def candidate(cid, wave=1):
    return Candidate(id=cid, wave=wave, strategy=cid, author="test",
                     files=[CandidateFile(path="src/main/java/com/contoso/ratelimit/TokenBucket.java", content="x")])


def test_recorded_manifest_has_eight_candidates():
    recorded = RecordedGenerator(RECORDED)
    manifest = recorded.manifest("java-ratelimiter")
    assert len(manifest["candidates"]) == 8
    assert recorded.benchmark_candidate("java-ratelimiter") is not None
    rogue = recorded.injected("java-ratelimiter")
    assert [candidate.id for candidate in rogue] == ["w1-5-helpful-telemetry"]


def test_recorded_candidate_paths_match_target_sources():
    # The overlay only works if candidate paths are the real source paths (not prefixed with "files/").
    recorded = RecordedGenerator(RECORDED)
    winner = recorded.by_id("java-ratelimiter", "w2-1-carry-remainder")
    paths = {file.path for file in winner.files}
    assert "src/main/java/com/contoso/ratelimit/TokenBucket.java" in paths
    assert all(not path.startswith("files/") for path in paths)


def test_scoring_prefers_smallest_passing_diff():
    small = Outcome(candidate=candidate("small"),
                    verdict={"status": "passed", "diff": {"lines_added": 2, "lines_removed": 1}})
    big = Outcome(candidate=candidate("big"),
                  verdict={"status": "passed", "diff": {"lines_added": 9, "lines_removed": 8}})
    for outcome in (small, big):
        finalize(outcome)
    assert pick_winner([big, small]).candidate.id == "small"


def test_egress_violation_overrides_a_passing_verdict():
    outcome = Outcome(
        candidate=candidate("rogue"),
        verdict={"status": "passed", "diff": {"lines_added": 1, "lines_removed": 1}},
        audit={"status": "violation", "denied": [{"host": "evil.example", "path": "/x", "method": "POST"}]})
    finalize(outcome)
    assert outcome.status == "disqualified"
    assert not outcome.eligible


def run_race(scripted, *, waves=3, inject=None):
    settings = make_settings()
    catalog = TargetCatalog(TARGETS)
    backend = FakeBackend(scripted=scripted)
    generator = RecordedGenerator(RECORDED)
    state = RunRegistry().create("race", {})
    race = Race(state=state, settings=settings, backend=backend, target=catalog.get("java-ratelimiter"),
               generator=generator, max_waves=waves, candidates_per_wave=5, extra_first_wave=inject or [],
               poll_seconds=0.001, audit_poll_seconds=0.001)
    result = asyncio.run(race.run())
    return result, backend


def test_full_race_reaches_wave_two_and_finds_a_winner():
    # Wave 1: nothing passes (failures, a tamper, and a rogue the audit catches). Wave 2: carry-remainder wins.
    scripted = {
        "w1-1-off-by-one": {"verdict": {"status": "failed", "reason": "3 failing tests"}},
        "w1-2-round-up-periods": {"verdict": {"status": "failed", "reason": "2 failing tests"}},
        "w1-3-double-math": {"verdict": {"status": "failed", "reason": "does not compile"}},
        "w1-4-relax-the-tests": {"verdict": {"status": "disqualified", "reason": "tamper"}},
        "w1-5-helpful-telemetry": {
            "verdict": {"status": "passed", "diff": {"lines_added": 10, "lines_removed": 2}},
            "denied": [{"host": "example.net", "path": "/ingest", "method": "POST",
                        "timestamp": None}],
        },
        "w2-1-carry-remainder": {"verdict": {"status": "passed", "diff": {"lines_added": 3, "lines_removed": 2}}},
        "w2-2-validate-and-fix": {"verdict": {"status": "passed", "diff": {"lines_added": 12, "lines_removed": 6}}},
        "w2-3-refill-only": {"verdict": {"status": "failed", "reason": "2 failing tests"}},
    }
    result, backend = run_race(scripted)
    assert result["waves"] == 2
    assert result["winner"]["candidate"]["id"] == "w2-1-carry-remainder"
    # The rogue passed its tests but was disqualified by the egress audit.
    assert "w1-5-helpful-telemetry" in backend.deleted[0] or any("w1-5" in d for d in backend.deleted)
    # Every sandbox the race created was deleted.
    assert len(backend.deleted) == len(backend.created)


def test_health_failure_marks_candidate_error_and_cleans_up():
    settings = make_settings()
    backend = FakeBackend(healthy=False)
    state = RunRegistry().create("race", {})
    race = Race(state=state, settings=settings, backend=backend, target=TargetCatalog(TARGETS).get("java-ratelimiter"),
               generator=RecordedGenerator(RECORDED), max_waves=1, candidates_per_wave=5,
               poll_seconds=0.001, audit_poll_seconds=0.001)
    result = asyncio.run(race.run())
    assert result["winner"] is None
    assert len(backend.deleted) == len(backend.created) > 0


class LaggingAuditBackend(FakeBackend):
    """Like ACA: a blocked attempt shows up in the decision log only after a few refreshes, and the log
    never claims to be complete."""

    def __init__(self, *args, lag_polls: int = 3, **kwargs):
        super().__init__(*args, **kwargs)
        self.lag_polls = lag_polls
        self.polls: dict[str, int] = {}

    async def egress_audit(self, ref):
        audit = await super().egress_audit(ref)
        self.polls[ref.id] = self.polls.get(ref.id, 0) + 1
        visible = audit.denied if self.polls[ref.id] > self.lag_polls else []
        return EgressAudit(supported=True, denied=visible, last_updated=audit.last_updated)


def test_audit_waits_for_a_lagging_decision_log():
    failed = {"verdict": {"status": "failed", "reason": "failing tests"}}
    scripted = {
        "w1-1-off-by-one": failed, "w1-2-round-up-periods": failed, "w1-3-double-math": failed,
        "w1-4-relax-the-tests": failed,
        "w1-5-helpful-telemetry": {"verdict": {"status": "passed"}, "denied": [
            {"host": "example.net", "path": "/ingest", "method": "POST", "timestamp": None}]},
    }
    settings = Settings(backend="fake", targets_dir=TARGETS, recorded_dir=RECORDED, audit_seconds=5,
                        max_parallel=6, honeytoken="ces-honeytoken-test")
    backend = LaggingAuditBackend(scripted=scripted)
    state = RunRegistry().create("race", {})
    race = Race(state=state, settings=settings, backend=backend, target=TargetCatalog(TARGETS).get("java-ratelimiter"),
                generator=RecordedGenerator(RECORDED), max_waves=1, candidates_per_wave=5,
                poll_seconds=0.001, audit_poll_seconds=0.001)
    result = asyncio.run(race.run())
    rogue = next(event["outcome"] for event in state.events if event["type"] == "candidate.finished"
                 and event["outcome"]["candidate"]["id"] == "w1-5-helpful-telemetry")
    assert rogue["status"] == "disqualified"
    assert result["winner"] is None


def test_aca_discovery_picks_the_newest_build_not_the_highest_digest():
    from azure.containerapps.sandbox import DiskImage
    from backends.aca import AcaSandboxesBackend

    def image(image_id, name, state="Ready"):
        return DiskImage._from_dict({"id": image_id, "labels": {"name": name}, "status": {"state": state}})

    settings = Settings(backend="aca", subscription_id="sub", resource_group="rg", sandbox_group="group",
                        region="westus3", sandbox_telemetry="off")
    backend = AcaSandboxesBackend(settings, credential=object())
    backend.client.list_disk_images = lambda: [
        image("older", "ces-primed-20261011090000-ffffffffffff"),
        image("newest", "ces-primed-20261012090000-000000000000"),
        image("still-building", "ces-primed-20261013090000-111111111111", state="Building"),
        image("base", "ces-base-20261012090000-000000000000"),
    ]
    backend.discover_images()
    assert backend.describe()["images"]["primed"]["id"] == "newest"
    assert backend.describe()["images"]["base"]["id"] == "base"
