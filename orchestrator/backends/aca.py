"""Azure Container Apps Sandboxes backend: one hardware-isolated microVM per candidate."""

from __future__ import annotations

import asyncio
import logging
import shlex
from datetime import UTC, datetime, timedelta

from azure.containerapps.sandbox import (
    AutoDeletePolicy,
    AutoSuspendPolicy,
    DiskImage,
    EgressPolicy,
    EgressRule,
    EgressRuleAction,
    EgressRuleMatch,
    LifecyclePolicy,
    SandboxClient,
    SandboxGroupClient,
    endpoint_for_region,
)
from azure.core.exceptions import HttpResponseError, ResourceNotFoundError
from models import Lane
from settings import Settings

from backends.base import EgressAudit, ExecOutcome, SandboxBackend, SandboxRef

logger = logging.getLogger("ces.aca")

PRIMED_PREFIX = "ces-primed-"
BASE_PREFIX = "ces-base-"
MAVEN_CENTRAL = "repo.maven.apache.org"
APPINSIGHTS_SECRET = "appinsights"


def locked_egress() -> EgressPolicy:
    """Candidates get no network at all. Full inspection records every attempt in the egress audit log."""
    return EgressPolicy(default_action="Deny", traffic_inspection="Full")


def registry_egress() -> EgressPolicy:
    """The primer (and the cold lane) may only read from Maven Central."""
    return EgressPolicy(
        default_action="Deny",
        traffic_inspection="Full",
        rules=[EgressRule(
            name="maven-central-read-only",
            match=EgressRuleMatch(host=MAVEN_CENTRAL, methods=["GET", "HEAD"]),
            action=EgressRuleAction(type="Allow"),
        )],
    )


def telemetry_config() -> dict:
    """Per-sandbox telemetry: stdout/stderr, VM metrics, and every egress decision, to Application Insights."""
    return {
        "metricsIntervalSeconds": 15,
        "endpoints": [{
            "kind": "ApplicationInsights",
            "data": ["ContainerStdoutStderr", "Metrics", "NetworkEgressDecisions"],
            "auth": {"secretId": APPINSIGHTS_SECRET, "secretKey": "connectionString"},
        }],
    }


class RefereeSandboxGroupClient(SandboxGroupClient):
    """SandboxGroupClient plus two capabilities that azure-containerapps-sandbox 0.1.0b4 doesn't expose yet:

    * disk images pulled from ACR with the sandbox group's managed identity (the v2 disk image API), and
    * a per-sandbox ``telemetryConfig`` (falls back to no telemetry if the service rejects it).
    """

    def __init__(self, *args, telemetry: dict | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.telemetry = telemetry

    def create_disk_image_from_registry(self, image_url: str, *, identity_client_id: str,
                                        labels: dict[str, str]) -> DiskImage:
        body = {
            "labels": labels,
            "source": {"kind": "registry", "imageUrl": image_url, "managedIdentityClientId": identity_client_id},
        }
        return DiskImage._from_dict(self._dp_put(f"{self._group_path}/diskimages/v2", body))

    def _dp_put(self, path, body=None, **kwargs):
        if self.telemetry and isinstance(body, dict) and path == f"{self._group_path}/sandboxes":
            try:
                return super()._dp_put(path, {**body, "telemetryConfig": self.telemetry}, **kwargs)
            except HttpResponseError as error:
                if error.status_code not in (400, 422):
                    raise
                logger.warning("Sandboxes rejected telemetryConfig (%s). Creating sandboxes without it.",
                               error.message)
                self.telemetry = None
        return super()._dp_put(path, body, **kwargs)


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _image_name(image: DiskImage) -> str:
    return (image.labels or {}).get("name") or image.name or ""


class AcaSandboxesBackend(SandboxBackend):
    name = "aca"

    def __init__(self, settings: Settings, credential=None):
        from azure_auth import sync_credential

        missing = [name for name, value in {
            "AZURE_SUBSCRIPTION_ID": settings.subscription_id,
            "AZURE_RESOURCE_GROUP": settings.resource_group,
            "SANDBOX_GROUP_NAME": settings.sandbox_group,
            "SANDBOX_GROUP_REGION": settings.region,
        }.items() if not value]
        if missing:
            raise RuntimeError(f"Missing settings for the ACA Sandboxes backend: {', '.join(missing)}")
        self.settings = settings
        self.credential = credential or sync_credential()
        wants_telemetry = settings.sandbox_telemetry != "off" and bool(settings.appinsights_connection_string)
        self.client = RefereeSandboxGroupClient(
            endpoint_for_region(settings.region),
            self.credential,
            subscription_id=settings.subscription_id,
            resource_group=settings.resource_group,
            sandbox_group=settings.sandbox_group,
            telemetry=telemetry_config() if wants_telemetry else None,
        )
        self._sandboxes: dict[str, SandboxClient] = {}
        self._images: dict[str, DiskImage | None] = {"primed": None, "base": None}

    # ── Startup ──────────────────────────────────────────────────────────────

    async def startup(self) -> None:
        await asyncio.to_thread(self.discover_images)
        if self.client.telemetry:
            try:
                await asyncio.to_thread(self.client.upsert_secret, APPINSIGHTS_SECRET,
                                        {"connectionString": self.settings.appinsights_connection_string})
            except HttpResponseError as error:
                logger.warning("Couldn't store the Application Insights secret on the sandbox group: %s", error)
                self.client.telemetry = None

    def discover_images(self) -> None:
        """Pick the newest Ready disk images created by prepare_images.py (or the IDs pinned in settings)."""
        pinned = {"primed": self.settings.primed_disk_image_id, "base": self.settings.base_disk_image_id}
        ready = [image for image in self.client.list_disk_images()
                 if image.status and image.status.state in ("Ready", "Succeeded")]
        for role, prefix in (("primed", PRIMED_PREFIX), ("base", BASE_PREFIX)):
            if pinned[role]:
                self._images[role] = next((i for i in ready if i.id == pinned[role]), None) or \
                    DiskImage._from_dict({"id": pinned[role], "labels": {"name": pinned[role]}})
                continue
            named = sorted((i for i in ready if _image_name(i).startswith(prefix)), key=_image_name)
            self._images[role] = named[-1] if named else None

    def has_image(self, image: str) -> bool:
        return self._images.get(image) is not None

    # ── Sandbox lifecycle ────────────────────────────────────────────────────

    async def create(self, *, run_id: str, name: str, lane: Lane, labels: dict[str, str],
                     environment: dict[str, str]) -> SandboxRef:
        image = self._images.get(lane.image)
        if image is None:
            await asyncio.to_thread(self.discover_images)
            image = self._images.get(lane.image)
        if image is None:
            raise RuntimeError(f"No '{lane.image}' disk image yet. Run scripts/prepare_images.py first.")
        policy = locked_egress() if lane.egress == "locked" else registry_egress()

        def _create() -> SandboxClient:
            sandbox = self.client.begin_create_sandbox(
                disk_id=image.id,
                cpu=self.settings.candidate_cpu,
                memory=self.settings.candidate_memory,
                auto_suspend_seconds=60,
                auto_suspend_mode="Disk",
                labels=labels,
                environment=environment,
                egress_policy=policy,
                polling_interval=1,
            ).result()
            try:
                # Backstop for automatic cleanup: if the orchestrator dies, an idle sandbox stops after
                # 60 seconds and the platform deletes it 10 minutes later.
                sandbox.set_lifecycle_policy(LifecyclePolicy(
                    auto_suspend=AutoSuspendPolicy(enabled=True, interval=60, mode="Disk"),
                    auto_delete=AutoDeletePolicy(enabled=True, delete_interval_seconds=600),
                ))
            except HttpResponseError as error:
                logger.warning("Couldn't set the lifecycle policy on %s: %s", sandbox.sandbox_id, error)
            return sandbox

        sandbox = await asyncio.to_thread(_create)
        self._sandboxes[sandbox.sandbox_id] = sandbox
        return SandboxRef(id=sandbox.sandbox_id, labels=labels)

    async def exec(self, ref: SandboxRef, command: str, *, timeout_s: int = 120) -> ExecOutcome:
        sandbox = self._sandboxes[ref.id]
        result = await asyncio.wait_for(
            asyncio.to_thread(sandbox.exec, f"sh -c {shlex.quote(command)}", working_directory="/work"),
            timeout=timeout_s,
        )
        return ExecOutcome(result.exit_code, result.stdout or "", result.stderr or "")

    async def write_file(self, ref: SandboxRef, path: str, data: bytes) -> None:
        await asyncio.to_thread(self._sandboxes[ref.id].write_file, path, data, create_dirs=True)

    async def read_file(self, ref: SandboxRef, path: str) -> bytes:
        return await asyncio.to_thread(self._sandboxes[ref.id].read_file, path)

    async def egress_audit(self, ref: SandboxRef) -> EgressAudit:
        decisions = await asyncio.to_thread(self._sandboxes[ref.id].get_egress_decisions)
        network = decisions.network_egress if decisions else None

        def entries(items) -> list[dict]:
            return [{"timestamp": e.timestamp, "host": e.host, "method": e.method, "path": e.path,
                     "scheme": e.scheme} for e in (items or [])]

        return EgressAudit(
            supported=True,
            allowed=entries(network.allowed if network else None),
            denied=entries(network.denied if network else None),
            last_updated=decisions.last_updated if decisions else None,
        )

    async def delete(self, ref: SandboxRef) -> None:
        sandbox = self._sandboxes.pop(ref.id, None)
        if sandbox is None:
            return
        try:
            await asyncio.to_thread(sandbox.delete)
        except ResourceNotFoundError:
            pass
        finally:
            await asyncio.to_thread(sandbox.close)

    async def sweep(self, *, older_than_s: int | None = None) -> int:
        """Delete sandboxes this demo created (found by label), optionally only older ones."""

        def _sweep() -> int:
            cutoff = datetime.now(UTC) - timedelta(seconds=older_than_s) if older_than_s else None
            deleted = 0
            for sandbox in self.client.list_sandboxes(labels={"ces-app": "referee"}):
                created = _parse_time(sandbox.created_at)
                if cutoff and created and created > cutoff:
                    continue
                try:
                    self.client.delete_sandbox(sandbox.id)
                    deleted += 1
                except ResourceNotFoundError:
                    pass
            return deleted

        return await asyncio.to_thread(_sweep)

    def describe(self) -> dict:
        def image_info(role: str) -> dict | None:
            image = self._images.get(role)
            return {"id": image.id, "name": _image_name(image)} if image else None

        return {
            "name": self.name,
            "title": "Azure Container Apps Sandboxes",
            "sandbox_group": self.settings.sandbox_group,
            "region": self.settings.region,
            "images": {"primed": image_info("primed"), "base": image_info("base")},
            "candidate_size": f"{self.settings.candidate_cpu} CPU / {self.settings.candidate_memory}",
            "telemetry": bool(self.client.telemetry),
        }
