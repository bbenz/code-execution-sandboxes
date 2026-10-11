"""How the loop judges candidates: the referee's verdict plus the sandbox's egress audit."""

from __future__ import annotations

from models import Outcome


def finalize(outcome: Outcome) -> None:
    """Combine the in-sandbox verdict with the platform's network evidence."""
    verdict = outcome.verdict or {}
    audit = outcome.audit or {}
    if not verdict:
        outcome.status = "error"
        outcome.reason = outcome.reason or "No verdict"
    elif audit.get("status") == "violation":
        attempt = audit["denied"][0]
        target = f"{attempt.get('method') or ''} {attempt.get('host') or ''}{attempt.get('path') or ''}".strip()
        outcome.status = "disqualified"
        outcome.reason = f"Tried to reach {target}; the egress policy blocked it"
    else:
        outcome.status = verdict.get("status", "error")
        outcome.reason = verdict.get("reason", "")
    outcome.eligible = outcome.status == "passed"


def pick_winner(outcomes: list[Outcome]) -> Outcome | None:
    """Among candidates that passed every check, prefer the smallest diff, then the fastest build."""
    eligible = [outcome for outcome in outcomes if outcome.eligible]
    if not eligible:
        return None
    return min(eligible, key=lambda o: (o.diff_size, (o.verdict or {}).get("duration_ms", 0)))


def summarize(outcomes: list[Outcome]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for outcome in outcomes:
        counts[outcome.status] = counts.get(outcome.status, 0) + 1
    return counts


def feedback(outcomes: list[Outcome], limit: int = 6000) -> str:
    """What the coding agent learns from a wave: why each candidate lost."""
    lines: list[str] = []
    for outcome in outcomes:
        verdict = outcome.verdict or {}
        lines.append(f"- Candidate '{outcome.candidate.strategy}': {outcome.status.upper()} - {outcome.reason}")
        for error in (verdict.get("compile") or {}).get("errors", [])[:3]:
            lines.append(f"    compile error: {error}")
        for failure in ((verdict.get("tests") or {}).get("failures") or [])[:4]:
            message = " ".join(failure.get("message", "").split())[:300]
            lines.append(f"    failing test {failure.get('name')}: {message}")
        if (verdict.get("tamper") or {}).get("modified_protected"):
            lines.append("    note: changing tests or build files is not allowed")
    text = "\n".join(lines)
    return text[:limit]
