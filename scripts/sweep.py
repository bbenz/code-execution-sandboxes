#!/usr/bin/env python
"""Delete leftover referee sandboxes (and local Docker containers). Safe to run anytime.

Finds resources by the `ces-app=referee` label, so it only ever touches this demo's sandboxes.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "orchestrator"))

from backends import build_backend  # noqa: E402
from settings import Settings  # noqa: E402


async def main() -> int:
    parser = argparse.ArgumentParser(description="Delete leftover referee sandboxes.")
    parser.add_argument("--older-than", type=int, default=None,
                        help="Only delete sandboxes older than this many seconds.")
    args = parser.parse_args()

    settings = Settings.from_env()
    backend = build_backend(settings)
    await backend.startup()
    deleted = await backend.sweep(older_than_s=args.older_than)
    print(f"Swept {deleted} leftover sandbox(es) via the {backend.name} backend.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
