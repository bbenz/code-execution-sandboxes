"""Parse test results: JUnit XML (Maven Surefire, pytest) and `go test -json`."""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from pathlib import Path

MAX_FAILURES = 12


@dataclass
class TestSummary:
    __test__ = False  # not a pytest test class
    total: int = 0
    passed: int = 0
    failed: int = 0
    errors: int = 0
    skipped: int = 0
    failures: list[dict] = field(default_factory=list)

    def add_failure(self, name: str, message: str) -> None:
        if len(self.failures) < MAX_FAILURES:
            self.failures.append({"name": name, "message": (message or "").strip()[:400]})

    @property
    def all_passed(self) -> bool:
        return self.total > 0 and self.failed == 0 and self.errors == 0

    def to_dict(self) -> dict:
        return asdict(self)


def parse_junit_file(path: Path, summary: TestSummary | None = None) -> TestSummary:
    summary = summary or TestSummary()
    root = ET.parse(path).getroot()
    for case in root.iter("testcase"):
        name = ".".join(filter(None, [case.get("classname", ""), case.get("name", "")]))
        failure = case.find("failure")
        error = case.find("error")
        summary.total += 1
        if failure is not None:
            summary.failed += 1
            summary.add_failure(name, failure.get("message") or (failure.text or ""))
        elif error is not None:
            summary.errors += 1
            summary.add_failure(name, error.get("message") or (error.text or ""))
        elif case.find("skipped") is not None:
            summary.skipped += 1
        else:
            summary.passed += 1
    return summary


def parse_junit_dir(directory: Path, pattern: str = "TEST-*.xml") -> TestSummary | None:
    files = sorted(directory.glob(pattern)) if directory.is_dir() else []
    if not files:
        return None
    summary = TestSummary()
    for path in files:
        parse_junit_file(path, summary)
    return summary


SUREFIRE_TOTALS = re.compile(r"Tests run: (\d+), Failures: (\d+), Errors: (\d+), Skipped: (\d+)\s*$")


def surefire_console_totals(lines: list[str]) -> tuple[int, int, int, int] | None:
    """The last 'Tests run:' summary Maven prints; used to cross-check the XML reports."""
    for line in reversed(lines):
        match = SUREFIRE_TOTALS.search(line.strip())
        if match:
            return tuple(int(group) for group in match.groups())  # type: ignore[return-value]
    return None


def parse_go_test_json(lines: list[str]) -> tuple[TestSummary, list[str]]:
    """Summarize `go test -json` events. Returns (summary, build errors)."""
    final: dict[str, str] = {}
    output: dict[str, list[str]] = {}
    build_errors: list[str] = []
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        action = event.get("Action")
        test = event.get("Test")
        if action == "build-output":
            build_errors.append(event.get("Output", "").rstrip())
        elif action == "build-fail":
            build_errors.append(f"build failed: {event.get('ImportPath', '')}".strip())
        if not test:
            continue
        if action == "output":
            output.setdefault(test, []).append(event.get("Output", ""))
        elif action == "run":
            final.setdefault(test, "run")
        elif action in ("pass", "fail", "skip"):
            final[test] = action
    # Report leaf tests only: a parent test with subtests is a container, not a test case.
    leaves = [name for name in final if not any(other.startswith(name + "/") for other in final)]
    summary = TestSummary()
    for name in sorted(leaves):
        result = final[name]
        summary.total += 1
        if result == "pass":
            summary.passed += 1
        elif result == "skip":
            summary.skipped += 1
        else:
            # "run" with no final action means the test binary crashed (for example, a panic).
            if result == "fail":
                summary.failed += 1
            else:
                summary.errors += 1
            message = "".join(line for line in output.get(name, []) if not line.startswith(("=== ", "--- ")))
            summary.add_failure(name, message or ("panicked or crashed" if result == "run" else "failed"))
    return summary, [error for error in build_errors if error][:20]
