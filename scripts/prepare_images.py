#!/usr/bin/env python
"""Build the referee engine's disk images on a sandbox group.

Two images come out of this script:

  * ces-base-<built>-<digest>   — the engine image converted to a sandbox disk image (cold: no caches).
  * ces-primed-<built>-<digest> — a primer sandbox booted from the base image, primed so every target
                                  builds offline, then committed. Candidates boot from this one.

The disk image is pulled from ACR with the sandbox group's managed identity (keyless) through the v2
disk-image endpoint, which azure-containerapps-sandbox 0.1.0b4 doesn't expose yet. Re-runs are cheap:
images carry the engine image's digest and are reused when one built from the same digest exists.
<built> is a UTC timestamp, so the orchestrator can tell which image is newest from the name alone.

Run it after `azd provision` (the azure.yaml postprovision hook does this), or by hand with an active
azd environment / the right environment variables set.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

ORCHESTRATOR = Path(__file__).resolve().parents[1] / "orchestrator"
sys.path.insert(0, str(ORCHESTRATOR))

try:
    import httpx
    from azure.containerapps.sandbox import endpoint_for_region
    from backends.aca import RefereeSandboxGroupClient, registry_egress
    from settings import Settings
except ImportError as missing:
    sys.exit(f"{missing}. Install the orchestrator's packages into the Python that runs this script "
             "(and that `azd up` finds on PATH): pip install -r orchestrator/requirements.txt")

BASE_PREFIX = "ces-base-"
PRIMED_PREFIX = "ces-primed-"
MANIFEST_TYPES = ", ".join([
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.docker.distribution.manifest.v2+json",
])


def azd_set(key: str, value: str) -> None:
    # On Windows, azd is an .exe that subprocess can't always find by bare name.
    try:
        subprocess.run([shutil.which("azd") or "azd", "env", "set", key, value],
                       check=True, capture_output=True, text=True)
    except (OSError, subprocess.CalledProcessError):
        pass  # not fatal: the orchestrator also discovers images by label prefix


def split_image(image: str) -> tuple[str, str, str]:
    """Split 'registry/repo[:tag]' into (registry, repo, tag)."""
    registry, _, path = image.partition("/")
    repo, tag = path, "latest"
    if ":" in path.rsplit("/", 1)[-1]:
        repo, _, tag = path.rpartition(":")
    return registry, repo, tag


def resolve_engine_digest(settings: Settings, credential) -> tuple[str, str]:
    """Return (image reference pinned by digest, short digest tag).

    Reads the tag's manifest digest straight from the registry with an Entra token exchange (keyless), so
    the script needs neither the Azure CLI nor an ARM call (`az acr repository show` first looks the
    registry up across the whole subscription).
    """
    registry, repo, tag = split_image(settings.engine_image)
    aad_token = credential.get_token("https://management.azure.com/.default").token
    with httpx.Client(timeout=60) as http:
        exchange = http.post(f"https://{registry}/oauth2/exchange", data={
            "grant_type": "access_token", "service": registry, "access_token": aad_token})
        exchange.raise_for_status()
        token = http.post(f"https://{registry}/oauth2/token", data={
            "grant_type": "refresh_token", "service": registry, "scope": f"repository:{repo}:pull",
            "refresh_token": exchange.json()["refresh_token"]})
        token.raise_for_status()
        manifest = http.head(f"https://{registry}/v2/{repo}/manifests/{tag}", headers={
            "Authorization": f"Bearer {token.json()['access_token']}", "Accept": MANIFEST_TYPES})
        manifest.raise_for_status()
    digest = manifest.headers.get("Docker-Content-Digest", "")
    if not digest.startswith("sha256:"):
        raise RuntimeError(f"Could not resolve a digest for {settings.engine_image}: {digest!r}")
    return f"{registry}/{repo}@{digest}", digest[7:19]


def json_document(text: str) -> dict:
    """Parse the one JSON document a referee command prints. It's indented, so it spans many lines."""
    lines = text.strip().splitlines()
    for start, line in enumerate(lines):
        if line.startswith("{"):
            try:
                return json.loads("\n".join(lines[start:]))
            except ValueError:
                continue
    return {}


def image_name(image) -> str:
    return (image.labels or {}).get("name") or image.name or ""


def find_ready(client: RefereeSandboxGroupClient, prefix: str, digest: str):
    """The newest Ready image of this kind built from this engine digest, if any."""
    matches = [image for image in client.list_disk_images()
               if image.status and image.status.state in ("Ready", "Succeeded")
               and image_name(image).startswith(prefix) and image_name(image).endswith(f"-{digest}")]
    return max(matches, key=image_name, default=None)


def new_image_name(prefix: str, digest: str) -> str:
    # The SDK's DiskImage model has no creation time, so the name carries it: the orchestrator picks the
    # newest image by name, and the digest suffix lets re-runs reuse images built from the same engine.
    return f"{prefix}{datetime.now(UTC):%Y%m%d%H%M%S}-{digest}"


def wait_ready(client: RefereeSandboxGroupClient, image_id: str, *, what: str, timeout_s: int = 420):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        image = client.get_disk_image(image_id)
        state = image.status.state if image.status else None
        if state in ("Ready", "Succeeded"):
            return image
        if state == "Failed":
            raise RuntimeError(f"{what} failed: {image.status.message if image.status else 'unknown'}")
        print(f"  {what}: {state or 'pending'}...", flush=True)
        time.sleep(5)
    raise TimeoutError(f"{what} did not become Ready within {timeout_s}s")


def ensure_base(client: RefereeSandboxGroupClient, settings: Settings, image_ref: str, digest: str) -> str:
    existing = find_ready(client, BASE_PREFIX, digest)
    if existing:
        print(f"Reusing base disk image {existing.id} ({image_name(existing)})")
        return existing.id
    name = new_image_name(BASE_PREFIX, digest)
    print(f"Building base disk image {name} from {image_ref} (keyless ACR pull)...")
    image = client.create_disk_image_from_registry(
        image_ref, identity_client_id=settings.group_identity_client_id, labels={"name": name, "ces-role": "base"})
    ready = wait_ready(client, image.id, what="Base disk image build")
    print(f"Base disk image ready: {ready.id}")
    return ready.id


def ensure_primed(client: RefereeSandboxGroupClient, settings: Settings, base_id: str, digest: str) -> str:
    existing = find_ready(client, PRIMED_PREFIX, digest)
    if existing:
        print(f"Reusing primed disk image {existing.id} ({image_name(existing)})")
        return existing.id
    name = new_image_name(PRIMED_PREFIX, digest)

    print("Booting a primer sandbox from the base image (Maven Central egress only)...")
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
        print("Priming dependency and build caches (referee prime --insecure-tls)...")
        result = sandbox.exec("sh -c 'referee prime --insecure-tls'", working_directory="/work")
        summary = json_document(result.stdout)
        if not summary.get("ok"):
            print(result.stdout[-2000:])
            print(result.stderr[-1000:], file=sys.stderr)
            raise RuntimeError("Priming failed inside the sandbox (see output above).")
        targets = ", ".join(f"{t['target']} offline={t['offline_build_ms']}ms" for t in summary.get("targets", []))
        print(f"  primed: {targets}")
        print("Committing the primed sandbox to a disk image...")
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
    # Sandbox output can contain characters a Windows console or pipe encoding can't represent.
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(errors="replace")
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

    credential = sync_credential()
    image_ref, tag = resolve_engine_digest(settings, credential)
    print(f"Engine image pinned to {image_ref}")
    client = RefereeSandboxGroupClient(
        endpoint_for_region(settings.region),
        credential,
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
