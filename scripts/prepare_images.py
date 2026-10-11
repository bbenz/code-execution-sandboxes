#!/usr/bin/env python
"""Build the referee engine's disk images on a sandbox group.

Two images come out of this script:

  * ces-base-<digest>   — the engine image converted to a sandbox disk image (cold: no caches).
  * ces-primed-<digest> — a primer sandbox booted from the base image, primed so every target builds
                          offline, then committed. Candidates boot from this one.

The disk image is pulled from ACR with the sandbox group's managed identity (keyless) through the v2
disk-image endpoint, which azure-containerapps-sandbox 0.1.0b4 doesn't expose yet. Re-runs are cheap:
images are tagged with the engine image's digest and reused when they already exist.

Run it after `azd provision` (the azure.yaml postprovision hook does this), or by hand with an active
azd environment / the right environment variables set.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

ORCHESTRATOR = Path(__file__).resolve().parents[1] / "orchestrator"
sys.path.insert(0, str(ORCHESTRATOR))

from azure.containerapps.sandbox import endpoint_for_region  # noqa: E402
from backends.aca import RefereeSandboxGroupClient, registry_egress  # noqa: E402
from settings import Settings  # noqa: E402

BASE_PREFIX = "ces-base-"
PRIMED_PREFIX = "ces-primed-"


def run(cmd: list[str]) -> str:
    return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout.strip()


def azd_set(key: str, value: str) -> None:
    try:
        subprocess.run(["azd", "env", "set", key, value], check=True, capture_output=True, text=True)
    except (OSError, subprocess.CalledProcessError):
        pass  # not fatal: the orchestrator also discovers images by label prefix


def resolve_engine_digest(settings: Settings) -> tuple[str, str]:
    """Return (pinned image reference by digest, short digest tag)."""
    acr_login = settings.engine_image.split("/", 1)[0]
    acr_name = acr_login.split(".", 1)[0]
    repo_tag = settings.engine_image.split("/", 1)[1]
    repo, _, tag = repo_tag.partition(":")
    digest = run(["az", "acr", "repository", "show", "--name", acr_name, "--image", f"{repo}:{tag or 'latest'}",
                  "--query", "digest", "-o", "tsv"])
    if not digest.startswith("sha256:"):
        raise RuntimeError(f"Could not resolve a digest for {settings.engine_image}: {digest!r}")
    return f"{acr_login}/{repo}@{digest}", digest[7:19]


def find_ready(client: RefereeSandboxGroupClient, name: str):
    for image in client.list_disk_images():
        label = (image.labels or {}).get("name") or image.name or ""
        if label == name and image.status and image.status.state in ("Ready", "Succeeded"):
            return image
    return None


def wait_ready(client: RefereeSandboxGroupClient, image_id: str, *, what: str, timeout_s: int = 420):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        image = client.get_disk_image(image_id)
        state = image.status.state if image.status else None
        if state in ("Ready", "Succeeded"):
            return image
        if state == "Failed":
            raise RuntimeError(f"{what} failed: {image.status.message if image.status else 'unknown'}")
        print(f"  {what}: {state or 'pending'}…", flush=True)
        time.sleep(5)
    raise TimeoutError(f"{what} did not become Ready within {timeout_s}s")


def ensure_base(client: RefereeSandboxGroupClient, settings: Settings, image_ref: str, tag: str) -> str:
    name = f"{BASE_PREFIX}{tag}"
    existing = find_ready(client, name)
    if existing:
        print(f"Reusing base disk image {existing.id} ({name})")
        return existing.id
    print(f"Building base disk image {name} from {image_ref} (keyless ACR pull)…")
    image = client.create_disk_image_from_registry(
        image_ref, identity_client_id=settings.group_identity_client_id, labels={"name": name, "ces-role": "base"})
    ready = wait_ready(client, image.id, what="Base disk image build")
    print(f"Base disk image ready: {ready.id}")
    return ready.id


def ensure_primed(client: RefereeSandboxGroupClient, settings: Settings, base_id: str, tag: str) -> str:
    name = f"{PRIMED_PREFIX}{tag}"
    existing = find_ready(client, name)
    if existing:
        print(f"Reusing primed disk image {existing.id} ({name})")
        return existing.id

    print("Booting a primer sandbox from the base image (Maven Central egress only)…")
    sandbox = client.begin_create_sandbox(
        disk_id=base_id,
        cpu="2000m",
        memory="4096Mi",
        auto_suspend_seconds=900,
        labels={"ces-app": "referee", "ces-role": "primer", "ces-tenant": settings.tenant},
        egress_policy=registry_egress(),
        polling_interval=2,
    ).result()
    try:
        print("Priming dependency and build caches (referee prime --insecure-tls)…")
        result = sandbox.exec("sh -c 'referee prime --insecure-tls'", working_directory="/work")
        payload = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else "{}"
        summary = json.loads(payload) if payload.startswith("{") else {"ok": False}
        if not summary.get("ok"):
            print(result.stdout[-2000:])
            print(result.stderr[-1000:], file=sys.stderr)
            raise RuntimeError("Priming failed inside the sandbox (see output above).")
        targets = ", ".join(f"{t['target']} offline={t['offline_build_ms']}ms" for t in summary.get("targets", []))
        print(f"  primed: {targets}")
        print("Committing the primed sandbox to a disk image…")
        primed = sandbox.begin_commit(name=name, polling_timeout=1200).result()
        wait_ready(client, primed.id, what="Primed disk image commit")
        print(f"Primed disk image ready: {primed.id}")
        return primed.id
    finally:
        try:
            sandbox.delete()
        finally:
            sandbox.close()


def main() -> int:
    settings = Settings.from_env()
    missing = [name for name, value in {
        "AZURE_SUBSCRIPTION_ID": settings.subscription_id,
        "AZURE_RESOURCE_GROUP": settings.resource_group,
        "SANDBOX_GROUP_NAME": settings.sandbox_group,
        "SANDBOX_GROUP_REGION": settings.region,
        "SANDBOX_GROUP_IDENTITY_CLIENT_ID": settings.group_identity_client_id,
        "REFEREE_ENGINE_IMAGE": settings.engine_image,
    }.items() if not value]
    if missing:
        print(f"Missing required settings: {', '.join(missing)}", file=sys.stderr)
        print("Run `azd provision` first, or set these environment variables.", file=sys.stderr)
        return 2

    from azure_auth import sync_credential

    image_ref, tag = resolve_engine_digest(settings)
    print(f"Engine image pinned to {image_ref}")
    client = RefereeSandboxGroupClient(
        endpoint_for_region(settings.region),
        sync_credential(),
        subscription_id=settings.subscription_id,
        resource_group=settings.resource_group,
        sandbox_group=settings.sandbox_group,
    )
    try:
        base_id = ensure_base(client, settings, image_ref, tag)
        primed_id = ensure_primed(client, settings, base_id, tag)
    finally:
        client.close()

    azd_set("REFEREE_BASE_DISK_IMAGE_ID", base_id)
    azd_set("REFEREE_PRIMED_DISK_IMAGE_ID", primed_id)
    print("\nDisk images ready:")
    print(json.dumps({"base": base_id, "primed": primed_id}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
