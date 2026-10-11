"""Configuration from environment variables. Locally, values from the active azd environment are loaded too."""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path

ORCHESTRATOR_DIR = Path(__file__).resolve().parent


def load_azd_env() -> None:
    """Load `azd env get-values` into os.environ (without overriding existing variables), if azd is set up."""
    try:
        from dotenv_azd import load_azd_env as _load

        _load(quiet=True)
    except Exception:  # azd isn't installed or no environment is selected: rely on the process environment
        pass


def _int(name: str, default: int) -> int:
    value = os.environ.get(name, "")
    return int(value) if value.strip() else default


def _default_targets_dir() -> Path:
    in_repo = ORCHESTRATOR_DIR.parent / "engine" / "targets"
    return in_repo if in_repo.is_dir() else ORCHESTRATOR_DIR / "targets"


@dataclass
class Settings:
    backend: str = "aca"  # aca | docker | fake
    tenant: str = "contoso"

    # Azure Container Apps Sandboxes
    subscription_id: str = ""
    resource_group: str = ""
    sandbox_group: str = ""
    region: str = ""
    group_identity_resource_id: str = ""
    group_identity_client_id: str = ""
    engine_image: str = ""
    primed_disk_image_id: str = ""
    base_disk_image_id: str = ""
    sandbox_telemetry: str = "auto"  # auto | off
    candidate_cpu: str = "2000m"
    candidate_memory: str = "4096Mi"

    # Local Docker
    docker_image: str = "referee-engine:local"
    docker_primed_image: str = "referee-engine:primed"

    # Coding agent (Azure OpenAI / Microsoft Foundry)
    openai_endpoint: str = ""
    openai_deployment: str = ""

    # Observability
    appinsights_connection_string: str = ""

    # Race behavior
    max_parallel: int = 8
    audit_seconds: int = 30
    verify_timeout_s: int = 600
    probe_url: str = "https://example.com/"
    honeytoken: str = field(default_factory=lambda: f"ces-honeytoken-{uuid.uuid4().hex[:12]}")
    targets_dir: Path = field(default_factory=_default_targets_dir)
    recorded_dir: Path = ORCHESTRATOR_DIR / "recorded"

    @property
    def llm_configured(self) -> bool:
        return bool(self.openai_endpoint and self.openai_deployment)

    @classmethod
    def from_env(cls, *, load_azd: bool = True) -> Settings:
        if load_azd:
            load_azd_env()
        env = os.environ.get
        return cls(
            backend=env("SANDBOX_BACKEND", "aca").lower(),
            tenant=env("CES_TENANT", "contoso"),
            subscription_id=env("AZURE_SUBSCRIPTION_ID", ""),
            resource_group=env("AZURE_RESOURCE_GROUP", ""),
            sandbox_group=env("SANDBOX_GROUP_NAME", ""),
            region=env("SANDBOX_GROUP_REGION", "") or env("AZURE_LOCATION", ""),
            group_identity_resource_id=env("SANDBOX_GROUP_IDENTITY_RESOURCE_ID", ""),
            group_identity_client_id=env("SANDBOX_GROUP_IDENTITY_CLIENT_ID", ""),
            engine_image=env("REFEREE_ENGINE_IMAGE", ""),
            primed_disk_image_id=env("REFEREE_PRIMED_DISK_IMAGE_ID", ""),
            base_disk_image_id=env("REFEREE_BASE_DISK_IMAGE_ID", ""),
            sandbox_telemetry=env("SANDBOX_TELEMETRY", "auto").lower(),
            candidate_cpu=env("CANDIDATE_CPU", "2000m"),
            candidate_memory=env("CANDIDATE_MEMORY", "4096Mi"),
            docker_image=env("LOCAL_ENGINE_IMAGE", "referee-engine:local"),
            docker_primed_image=env("LOCAL_PRIMED_IMAGE", "referee-engine:primed"),
            openai_endpoint=env("AZURE_OPENAI_ENDPOINT", ""),
            openai_deployment=env("AZURE_OPENAI_DEPLOYMENT", ""),
            appinsights_connection_string=env("APPLICATIONINSIGHTS_CONNECTION_STRING", ""),
            max_parallel=_int("MAX_PARALLEL_SANDBOXES", 8),
            audit_seconds=_int("EGRESS_AUDIT_SECONDS", 30),
            verify_timeout_s=_int("VERIFY_TIMEOUT_SECONDS", 600),
            probe_url=env("EGRESS_PROBE_URL", "https://example.com/"),
            targets_dir=Path(env("REFEREE_TARGETS_DIR", "") or _default_targets_dir()),
        )
