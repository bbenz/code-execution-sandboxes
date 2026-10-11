"""Command line for the referee engine. Every command prints one JSON document to stdout."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from referee import VERSION


def _print(data: dict) -> None:
    sys.stdout.write(json.dumps(data, indent=2) + "\n")
    sys.stdout.flush()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="referee", description="Custom code-execution engine for agent loops.")
    parser.add_argument("--version", action="version", version=f"referee {VERSION}")
    commands = parser.add_subparsers(dest="command", required=True)

    health_cmd = commands.add_parser("health", help="Check toolchains, caches, and network isolation.")
    health_cmd.add_argument("--expect-primed", action="store_true", help="Fail if dependency caches are cold.")
    health_cmd.add_argument("--require-locked", action="store_true", help="Fail if outbound HTTPS is reachable.")
    health_cmd.add_argument("--probe-url", default=None, help="URL used for the network isolation probe.")

    prime_cmd = commands.add_parser("prime", help="Fill dependency and build caches (needs registry access).")
    prime_cmd.add_argument("--target", action="append", help="Target to prime (default: all primable targets).")
    prime_cmd.add_argument("--insecure-tls", action="store_true",
                           help="Accept the egress proxy's TLS interception certificate during downloads.")
    prime_cmd.add_argument("--timeout", type=int, default=1200)

    verify_cmd = commands.add_parser("verify", help="Build, test, and judge one candidate.")
    verify_cmd.add_argument("--target", required=True)
    verify_cmd.add_argument("--candidate", required=True, type=Path, help="Directory with candidate.json and files/.")
    verify_cmd.add_argument("--out", type=Path, help="Where to write the verdict JSON.")
    verify_cmd.add_argument("--status", type=Path, help="Progress file the orchestrator polls.")
    verify_cmd.add_argument("--online", action="store_true", help="Allow dependency downloads (cold builds).")
    verify_cmd.add_argument("--insecure-tls", action="store_true",
                            help="With --online: accept the egress proxy's TLS interception certificate.")
    verify_cmd.add_argument("--timeout", type=int, default=600)

    describe_cmd = commands.add_parser("describe", help="List baked-in targets.")
    describe_cmd.add_argument("--target")

    sentinel_cmd = commands.add_parser("sentinel", help="Run the local deny-all egress proxy.")
    sentinel_cmd.add_argument("--port", type=int, default=3128)

    args = parser.parse_args(argv)

    if args.command == "health":
        from referee.health import DEFAULT_PROBE_URL, health

        result = health(expect_primed=args.expect_primed, require_locked=args.require_locked,
                        probe_url=args.probe_url or DEFAULT_PROBE_URL)
        _print(result)
        return 0 if result["ok"] else 1

    if args.command == "prime":
        from referee.prime import prime

        result = prime(args.target, insecure_tls=args.insecure_tls, timeout_s=args.timeout)
        _print(result)
        return 0 if result["ok"] else 1

    if args.command == "verify":
        from referee.util import StatusReporter, write_json_atomic
        from referee.verify import error_verdict, verify

        try:
            verdict = verify(args.target, args.candidate, out_path=args.out, status_path=args.status,
                             offline=not args.online, insecure_tls=args.insecure_tls, timeout_s=args.timeout)
        except Exception as error:  # report engine failures as a verdict instead of a stack trace
            verdict = error_verdict(args.target, f"{type(error).__name__}: {error}")
            if args.out:
                write_json_atomic(args.out, verdict)
            StatusReporter(args.status).update("done", verdict["reason"], state="error")
            _print(verdict)
            return 2
        _print(verdict)
        return 0

    if args.command == "describe":
        from referee.targets import list_targets, load_target

        targets = [load_target(args.target)] if args.target else list_targets()
        _print({"engine_version": VERSION, "targets": [target.describe() for target in targets]})
        return 0

    if args.command == "sentinel":
        from referee.sentinel import run

        return run(args.port)

    parser.error(f"Unknown command {args.command}")
    return 2
