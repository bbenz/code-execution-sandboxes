"""Static policy scan of the files a candidate changed.

This is a hint, not a control: a determined attacker can hide from regular expressions. The
sandbox boundary and the egress audit are what actually contain untrusted code.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Rule:
    id: str
    severity: str  # "high" disqualifies a candidate, "medium" is a warning
    pattern: re.Pattern
    description: str


def _rule(rule_id: str, severity: str, pattern: str, description: str) -> Rule:
    return Rule(rule_id, severity, re.compile(pattern), description)


RULES: dict[str, list[Rule]] = {
    "java": [
        _rule("process-exec", "high", r"\bProcessBuilder\b|Runtime\.getRuntime\(\)\s*\.exec", "Starts an OS process"),
        _rule("network-client", "high",
              r"\bjava\.net\.http\b|\bHttpClient\b|\bHttpURLConnection\b|new\s+Socket\s*\(|new\s+URL\s*\(|\.openConnection\(",
              "Opens a network connection"),
        _rule("env-access", "medium", r"System\.getenv", "Reads environment variables"),
        _rule("reflection", "medium",
              r"Class\.forName|\.getDeclaredMethod\(|\.getMethod\(|\.setAccessible\(|MethodHandles",
              "Uses reflection"),
        _rule("filesystem-write", "medium", r"Files\.(write|writeString|newOutputStream)\b|FileOutputStream|FileWriter",
              "Writes files"),
        _rule("process-exit", "medium", r"System\.exit\(|Runtime\.getRuntime\(\)\s*\.halt", "Exits the JVM"),
    ],
    "python": [
        _rule("process-exec", "high", r"\bsubprocess\b|\bos\.(system|popen|exec\w*|spawn\w*)\b|\bpty\.spawn",
              "Starts an OS process"),
        _rule("network-client", "high", r"\bsocket\b|\burllib\b|\brequests\b|\bhttp\.client\b|\bhttpx\b|\baiohttp\b",
              "Opens a network connection"),
        _rule("env-access", "medium", r"\bos\.environ\b|\bos\.getenv\b", "Reads environment variables"),
        _rule("dynamic-code", "medium", r"\beval\(|\bexec\(|__import__\(|\bimportlib\b", "Runs dynamically built code"),
    ],
    "go": [
        _rule("process-exec", "high", r'"os/exec"|syscall\.Exec', "Starts an OS process"),
        _rule("network-client", "high", r'"net/http"|"net"|\bnet\.Dial', "Opens a network connection"),
        _rule("env-access", "medium", r"\bos\.(Getenv|Environ|LookupEnv)\b", "Reads environment variables"),
        _rule("unsafe-code", "medium", r'"unsafe"|"reflect"', "Uses unsafe or reflect"),
    ],
}


def scan(language: str, files: dict[str, str]) -> list[dict]:
    """Return one finding per (file, rule): the first line that matches."""
    findings: list[dict] = []
    for rel_path, content in sorted(files.items()):
        lines = content.splitlines()
        for rule in RULES.get(language, []):
            for number, line in enumerate(lines, start=1):
                if rule.pattern.search(line):
                    findings.append({
                        "rule": rule.id,
                        "severity": rule.severity,
                        "file": rel_path,
                        "line": number,
                        "snippet": line.strip()[:160],
                        "description": rule.description,
                    })
                    break
    return findings
