"""Choose a sandbox backend from settings."""

from __future__ import annotations

from settings import Settings

from backends.base import SandboxBackend


def build_backend(settings: Settings) -> SandboxBackend:
    if settings.backend == "docker":
        from backends.docker_local import DockerBackend

        return DockerBackend(settings)
    if settings.backend == "fake":
        from backends.fake import FakeBackend

        return FakeBackend()
    from backends.aca import AcaSandboxesBackend

    return AcaSandboxesBackend(settings)
