"""Unit tests for the engine's report parsers, policy scan, and path safety."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))

from referee import policy  # noqa: E402
from referee.reports import TestSummary, parse_go_test_json, surefire_console_totals  # noqa: E402
from referee.targets import safe_relative_path  # noqa: E402


def test_surefire_console_totals_reads_last_summary():
    lines = ["[ERROR] Tests run: 12, Failures: 5, Errors: 0, Skipped: 0", "[INFO] later noise"]
    assert surefire_console_totals(lines) == (12, 5, 0, 0)


def test_go_json_counts_leaf_tests_and_captures_failures():
    events = [
        '{"Action":"run","Test":"TestCompare"}',
        '{"Action":"run","Test":"TestCompare/equal"}',
        '{"Action":"pass","Test":"TestCompare/equal"}',
        '{"Action":"run","Test":"TestCompare/minor"}',
        '{"Action":"output","Test":"TestCompare/minor","Output":"compare failed"}',
        '{"Action":"fail","Test":"TestCompare/minor"}',
        '{"Action":"fail","Test":"TestCompare"}',
    ]
    summary, build_errors = parse_go_test_json(events)
    assert summary.total == 2 and summary.passed == 1 and summary.failed == 1
    assert build_errors == []
    assert "compare failed" in summary.failures[0]["message"]


def test_go_json_reports_build_failure():
    events = ['{"Action":"build-output","Output":"semver.go:7: undefined: strings"}',
              '{"Action":"build-fail","ImportPath":"example.com/semver"}']
    summary, build_errors = parse_go_test_json(events)
    assert summary.total == 0
    assert any("undefined" in error for error in build_errors)


def test_all_passed_requires_tests():
    assert not TestSummary().all_passed
    assert TestSummary(total=3, passed=3).all_passed
    assert not TestSummary(total=3, passed=2, failed=1).all_passed


def test_policy_flags_process_and_network_as_high():
    java = 'Runtime.getRuntime().exec("id"); var c = java.net.http.HttpClient.newHttpClient();'
    findings = policy.scan("java", {"A.java": java})
    rules = {finding["rule"]: finding["severity"] for finding in findings}
    assert rules.get("process-exec") == "high"
    assert rules.get("network-client") == "high"


def test_policy_flags_reflection_as_medium_only():
    source = 'Class<?> k = Class.forName(name); k.getMethod("start").invoke(pb);'
    findings = policy.scan("java", {"Sneaky.java": source})
    assert findings and all(finding["severity"] == "medium" for finding in findings)
    assert any(finding["rule"] == "reflection" for finding in findings)


def test_safe_relative_path_rejects_escapes():
    assert safe_relative_path("src/main/App.java") == "src/main/App.java"
    for bad in ["../secrets", "/etc/passwd", "a\\b", "C:/x", "./../x", "a/../../b"]:
        try:
            safe_relative_path(bad)
            raise AssertionError(f"should have rejected {bad}")
        except ValueError:
            pass
