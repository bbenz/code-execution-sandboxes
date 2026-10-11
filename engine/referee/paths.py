"""Filesystem layout of the engine. Every path can be overridden for local tests."""

import os
from pathlib import Path


def _path(env_name: str, default: Path) -> Path:
    value = os.environ.get(env_name)
    return Path(value) if value else default


REFEREE_HOME = _path("REFEREE_HOME", Path("/opt/referee"))
TARGETS_DIR = _path("REFEREE_TARGETS", REFEREE_HOME / "targets")
PRIMED_DIR = _path("REFEREE_PRIMED", REFEREE_HOME / "primed")
MAVEN_REPO = _path("REFEREE_MAVEN_REPO", REFEREE_HOME / "m2")
WORK_DIR = _path("REFEREE_WORK", Path("/work"))
