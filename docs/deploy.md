# Deploying to Azure

## Prerequisites

- [Azure Developer CLI](https://learn.microsoft.com/azure/developer/azure-developer-cli/) (`azd`) and the
  [Azure CLI](https://learn.microsoft.com/cli/azure/) (`az`), both signed in (`azd auth login`, `az login`).
- Python 3.12+ on the machine you run `azd up` from (the postprovision hook runs `scripts/prepare_images.py`).
- An Azure subscription with:
  - permission to create resources and role assignments (Owner, or Contributor + User Access Administrator);
  - quota for the coding model (`gpt-5.6-luna` GlobalStandard by default) in the model region;
  - a region where **ACA Sandboxes** are available — see the
    [Sandboxes overview](https://learn.microsoft.com/azure/container-apps/sandboxes-overview).
- Docker is **not** required: the engine and orchestrator images build in ACR.

## What `azd up` does

1. **Provisions** `infra/main.bicep`: ACR, Azure OpenAI + a model deployment, Log Analytics, Application
   Insights, an ACA environment, the orchestrator Container App (placeholder image at first), a
   `Microsoft.App/sandboxGroups` resource with a user-assigned identity, and all the role assignments.
2. **postprovision hook** (`azure.yaml`):
   - `scripts/build_engine.(sh|ps1)` → `az acr build` the engine image to `referee-engine:latest`.
   - `scripts/prepare_images.py` → build the **base** disk image (keyless ACR pull via the v2 endpoint),
     then boot a primer sandbox, `referee prime`, and `commit` it to the **primed** disk image.
3. **Deploys** the orchestrator image to the Container App.

Get the URL:

```bash
azd env get-value orchestratorUrl
```

## Configuration

Set these with `azd env set <KEY> <VALUE>` before `azd up` (all optional):

| Key | Default | Notes |
|---|---|---|
| `AZURE_LOCATION` | prompted | Region for everything except the model. Must support ACA Sandboxes. |
| `AZURE_OPENAI_LOCATION` | `AZURE_LOCATION` | Model region, if different. |
| `AZURE_OPENAI_MODEL` | `gpt-5.6-luna` | Coding-agent model. |
| `AZURE_OPENAI_CAPACITY` | `250` | Thousands of tokens/min. Lower it if quota is tight. |
| `CES_PREFIX` | `ces` | Resource-name prefix. |

The orchestrator's runtime settings (parallelism, audit window, candidate size, telemetry) are environment
variables listed in `.env.example`; set them on the Container App or via `azd env set`.

## Keyless auth

No keys or connection strings are passed into a sandbox. The managed identities and their roles:

| Identity | Role | Scope | Why |
|---|---|---|---|
| orchestrator UAMI | AcrPull | ACR | pull its own image |
| orchestrator UAMI | Container Apps SandboxGroup Data Owner | sandbox group | drive the data plane |
| orchestrator UAMI | Cognitive Services OpenAI User | Azure OpenAI | call the coding model |
| sandbox group UAMI | AcrPull | ACR | keyless pull of the engine image for disk-image builds |

The local developer principal (`AZURE_PRINCIPAL_ID`, set by `azd`) gets the same data-plane and model roles
so you can run the orchestrator or `scripts/prepare_images.py` from your machine.

## Rebuild and redeploy

```bash
azd deploy                       # orchestrator code only
azd provision                    # infrastructure only
python scripts/prepare_images.py # rebuild the disk images (e.g. after changing the engine)
bash scripts/build_engine.sh     # rebuild just the engine image in ACR
```

`prepare_images.py` reuses disk images tagged with the current engine digest, so re-running after an
orchestrator-only change is a no-op.

## Tear down

```bash
azd down
```

Deleting the sandbox group removes every sandbox, snapshot, and disk image in it.

## Cost notes

- Sandboxes bill for vCPU/memory **only while running**; stopped sandboxes don't. The loop deletes each
  sandbox as soon as it's judged, and auto-suspend/auto-delete are a backstop.
- Disk images and snapshots bill as storage for as long as they're kept (storage billing was "coming soon"
  at the time of writing — see the [cost docs](https://sandboxes.azure.com/docs/sandboxes/cost)). Re-running
  `prepare_images.py` reuses images rather than piling up new ones; `scripts/sweep.py` clears stray sandboxes.
- The Azure OpenAI deployment bills per token when the live coding agent runs. The recorded agent makes no
  model calls.

## Troubleshooting

- **`prepare_images.py` fails with `RegistryAuthFailed`** — the sandbox group's identity needs AcrPull on
  the registry (the Bicep grants this). Confirm the role assignment propagated (it can take a minute).
- **A race never leaves "Booting sandbox"** — check the **Sandbox Cores** quota for your subscription and
  region (Azure portal → Quotas → Azure Container Apps). Each L sandbox is 2 cores.
- **The model calls 401/403** — the orchestrator identity needs Cognitive Services OpenAI User on the
  account, and the deployment name must match `AZURE_OPENAI_DEPLOYMENT`.
- **Leftover sandboxes** — `python scripts/sweep.py` (optionally `--older-than 3600`). It only deletes
  sandboxes labelled `ces-app=referee`.
