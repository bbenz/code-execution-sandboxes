"""Local Docker backend: the same race on your laptop, with a sentinel proxy standing in for the egress proxy.

Candidates join an internal Docker network (no route to the internet). Their only way out is the
sentinel, which refuses every request with 403 and logs it, so local runs get an egress audit too.
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
from datetime import UTC, datetime

from models import Lane
from settings import Settings

from backends.base import EgressAudit, ExecOutcome, SandboxBackend, SandboxRef


def _docker_cpus(cpu: str) -> str:
    return str(int(cpu[:-1]) / 1000) if cpu.endswith("m") else cpu


def _docker_memory(memory: str) -> str:
    match = re.fullmatch(r"(\d+)(Mi|Gi)", memory)
    if not match:
        return memory
    return f"{match.group(1)}{'m' if match.group(2) == 'Mi' else 'g'}"


class DockerBackend(SandboxBackend):
    name = "docker"

    def __init__(self, settings: Settings):
        self.settings = settings
        self._ips: dict[str, str] = {}
        self._created_at: dict[str, str] = {}
        self._available: dict[str, bool] = {}

    @staticmethod
    def _docker(*args: str, data: bytes | None = None, timeout: int = 300) -> subprocess.CompletedProcess:
        return subprocess.run(["docker", *args], input=data, capture_output=True, timeout=timeout)

    async def _run(self, *args: str, data: bytes | None = None, timeout: int = 300,
                   check: bool = True) -> subprocess.CompletedProcess:
        proc = await asyncio.to_thread(self._docker, *args, data=data, timeout=timeout)
        if check and proc.returncode != 0:
            message = proc.stderr.decode("utf-8", errors="replace").strip()[-500:]
            raise RuntimeError(f"docker {args[0]} failed: {message}")
        return proc

    @staticmethod
    def network(run_id: str) -> str:
        return f"ces-net-{run_id}"

    @staticmethod
    def sentinel(run_id: str) -> str:
        return f"ces-sentinel-{run_id}"

    def _image(self, image: str) -> str:
        return self.settings.docker_primed_image if image == "primed" else self.settings.docker_image

    async def startup(self) -> None:
        for role in ("primed", "base"):
            proc = await self._run("image", "inspect", self._image(role), check=False)
            self._available[role] = proc.returncode == 0

    def has_image(self, image: str) -> bool:
        return self._available.get(image, False)

    async def refresh_images(self) -> None:
        await self.startup()

    async def prepare_run(self, run_id: str) -> None:
        labels = ["--label", "ces-app=referee", "--label", f"ces-run={run_id}"]
        await self._run("network", "create", "--internal", *labels, self.network(run_id))
        await self._run("run", "-d", "--name", self.sentinel(run_id), "--network", self.network(run_id),
                        *labels, "--label", "ces-role=sentinel", self.settings.docker_image,
                        "referee", "sentinel", "--port", "3128")

    async def finish_run(self, run_id: str) -> None:
        await self._run("rm", "-f", self.sentinel(run_id), check=False)
        await self._run("network", "rm", self.network(run_id), check=False)

    async def create(self, *, run_id: str, name: str, lane: Lane, labels: dict[str, str],
                     environment: dict[str, str]) -> SandboxRef:
        container = re.sub(r"[^a-zA-Z0-9_.-]", "-", f"ces-{run_id}-{name}")[:63]
        args = ["run", "-d", "--name", container,
                "--cpus", _docker_cpus(self.settings.candidate_cpu),
                "--memory", _docker_memory(self.settings.candidate_memory)]
        for key, value in labels.items():
            args += ["--label", f"{key}={value}"]
        for key, value in environment.items():
            args += ["-e", f"{key}={value}"]
        if lane.egress == "locked":
            proxy = f"http://{self.sentinel(run_id)}:3128"
            args += ["--network", self.network(run_id)]
            for variable in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
                args += ["-e", f"{variable}={proxy}"]
            args += ["-e", "NO_PROXY=localhost,127.0.0.1", "-e", "no_proxy=localhost,127.0.0.1"]
        args += [self._image(lane.image), "sleep", "infinity"]
        await self._run(*args)
        inspect = await self._run("inspect", "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
                                  container)
        self._ips[container] = inspect.stdout.decode().strip()
        self._created_at[container] = datetime.now(UTC).isoformat(timespec="milliseconds")
        return SandboxRef(id=container, labels=labels)

    async def exec(self, ref: SandboxRef, command: str, *, timeout_s: int = 120) -> ExecOutcome:
        proc = await self._run("exec", "-w", "/work", ref.id, "sh", "-c", command, timeout=timeout_s, check=False)
        return ExecOutcome(proc.returncode, proc.stdout.decode("utf-8", errors="replace"),
                           proc.stderr.decode("utf-8", errors="replace"))

    async def write_file(self, ref: SandboxRef, path: str, data: bytes) -> None:
        await self._run("exec", "-i", ref.id, "sh", "-c", 'mkdir -p "$(dirname "$1")" && cat > "$1"', "sh", path,
                        data=data)

    async def read_file(self, ref: SandboxRef, path: str) -> bytes:
        return (await self._run("exec", ref.id, "cat", path)).stdout

    async def egress_audit(self, ref: SandboxRef) -> EgressAudit:
        run_id = ref.labels.get("ces-run", "")
        proc = await self._run("logs", self.sentinel(run_id), check=False)
        if proc.returncode != 0:
            return EgressAudit(supported=False)
        ip = self._ips.get(ref.id)
        # The sentinel's log is shared across the whole run and Docker reuses container IPs, so filter
        # to attempts made at or after this container was created. (On ACA, egress decisions are already
        # scoped per sandbox, so this bookkeeping is only needed for the local backend.)
        since = self._created_at.get(ref.id, "")
        denied = []
        for line in proc.stdout.decode("utf-8", errors="replace").splitlines():
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if entry.get("action") != "Deny":
                continue
            if ip and entry.get("client") != ip:
                continue
            if since and (entry.get("timestamp") or "") < since:
                continue
            denied.append({key: entry.get(key) for key in ("timestamp", "host", "method", "path", "scheme")})
        # The sentinel logs each attempt as it handles it, so the log is complete up to now.
        now = datetime.now(UTC).isoformat(timespec="milliseconds")
        return EgressAudit(supported=True, denied=denied, last_updated=now, complete_through=now)

    async def delete(self, ref: SandboxRef) -> None:
        await self._run("rm", "-f", ref.id, check=False)
        self._ips.pop(ref.id, None)
        self._created_at.pop(ref.id, None)

    async def sweep(self, *, older_than_s: int | None = None) -> int:
        containers = (await self._run("ps", "-aq", "--filter", "label=ces-app=referee", check=False)).stdout.split()
        if containers:
            await self._run("rm", "-f", *[c.decode() for c in containers], check=False)
        networks = (await self._run("network", "ls", "-q", "--filter", "label=ces-app=referee",
                                    check=False)).stdout.split()
        for network in networks:
            await self._run("network", "rm", network.decode(), check=False)
        return len(containers)

    def describe(self) -> dict:
        return {
            "name": self.name,
            "title": "Local Docker (sentinel egress proxy)",
            "images": {
                "primed": {"id": self.settings.docker_primed_image, "name": self.settings.docker_primed_image}
                if self._available.get("primed") else None,
                "base": {"id": self.settings.docker_image, "name": self.settings.docker_image}
                if self._available.get("base") else None,
            },
            "candidate_size": f"{self.settings.candidate_cpu} CPU / {self.settings.candidate_memory}",
            "telemetry": False,
        }
