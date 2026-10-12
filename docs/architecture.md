# Architecture

## The loop

```
                        orchestrator (an ordinary Azure Container App)
                        ┌──────────────────────────────────────────────┐
   you pick a target →  │  coding agent (live model, or recorded)       │
                        │        │ candidates (wave 1, 2, 3…)            │
                        │        ▼                                       │
                        │  race: fan out, score, feed failures back     │
                        └───┬───────────┬───────────┬──────────────┬────┘
                            ▼           ▼           ▼              ▼
                        sandbox 1   sandbox 2   sandbox 3   …   sandbox N      ← one microVM per candidate
                        (referee    (referee    (referee        (referee        booted from the PRIMED
                         engine)     engine)     engine)         engine)        disk image, egress: deny-all
                            │
                            │  health → upload → verify → read verdict → read egress audit → delete
                            ▼
                        verdict.json  +  the sandbox's egress decisions
```

The orchestrator never runs candidate code. It creates a sandbox, hands the candidate to the **referee
engine** inside it, and reads back a verdict. Each candidate gets its own hardware-isolated sandbox, so a
bad candidate can't touch the orchestrator, the host, or another candidate.

## One candidate, step by step

For every candidate the race loop (`orchestrator/race.py`) does this, with each step a span in one trace:

1. **create** — boot a sandbox from the primed disk image, with a **default-deny egress policy** and
   candidate labels (`ces-run`, `ces-candidate`, `ces-tenant`). Size L (2 vCPU / 4 GiB) by default.
2. **health** — run `referee health` as a startup probe. It must confirm the toolchains, the primed
   caches, a writable `/work`, **and that outbound HTTPS is blocked** before the sandbox gets any code.
3. **upload** — write the candidate's files into `/work/candidate/files/` through the sandbox file API.
4. **verify** — start `referee verify` in the background and poll its status file so the board shows live
   phases (policy scan → compile → test → lint).
5. **audit** — read the sandbox's **egress decisions** from the platform. A candidate that tried to reach
   the network is disqualified here, even if its tests passed. On ACA a blocked attempt shows up in the
   decision log about 10–20 s after it happens, so a candidate that passed is watched for
   `EGRESS_AUDIT_SECONDS` (30 s by default) before it can win.
6. **delete** — always, in a `finally`. Auto-suspend (60 s) and auto-delete (10 min) are a backstop if the
   orchestrator dies mid-run.

## The referee engine

`engine/referee/` is a small Python CLI baked into the image. Every command prints one JSON document.

| Command | What it does |
|---|---|
| `referee health` | Probes toolchains, primed caches, `/work`, and network isolation. The orchestrator's pre-flight gate. |
| `referee prime` | Fills Maven/Go caches once (needs registry access). Used to build the primed image. |
| `referee verify` | Copies the pristine target, overlays the candidate, compiles, tests, lints, scans, and writes a verdict. |
| `referee sentinel` | A deny-all logging HTTP proxy. Stands in for the ACA egress proxy on the local Docker backend. |
| `referee describe` | Lists the baked-in targets. |

`verify` judges a candidate on four things, in order:

1. **Scope** — it may only change the target's `editable` files. Touching a `protected` file (a test, the
   build file) is **disqualified** as tampering.
2. **Policy** — a static scan flags process-spawning and network code as *high* (disqualifying) and
   reflection, env reads, and file writes as *medium* (a warning). This is a hint, not a control.
3. **Build & tests** — it must compile and pass every test.
4. **Egress** (added by the orchestrator) — the sandbox's own record of what the candidate's code tried to
   reach. This is the control the static scan is only a hint for.

## The image pipeline — "prewarmed capacity"

A plain engine image has the toolchains but **cold** caches, so the first Maven build downloads the
internet. The fix is to warm the caches once and reuse them:

```
engine/Dockerfile ──az acr build──►  referee-engine:latest  (in ACR)
                                            │
                      create_disk_image_from_registry (v2 endpoint, managed-identity pull)
                                            ▼
                            ces-base-<built>-<digest>   ← cold: toolchains only
                                            │
          boot a primer sandbox (Maven Central egress only) → referee prime → begin_commit
                                            ▼
                           ces-primed-<built>-<digest>  ← warm: dependency + build caches baked in
                                            │
                        every candidate boots from here and builds fully OFFLINE
```

`scripts/prepare_images.py` builds both images and is idempotent: it names them with their build time and
the engine image's digest, and reuses them on re-runs. Before each race the orchestrator picks the newest
image of each kind by that name, so a rebuild takes effect without a restart. You can also pin exact IDs
with `REFEREE_PRIMED_DISK_IMAGE_ID` / `REFEREE_BASE_DISK_IMAGE_ID`.

**Why commit, not a snapshot.** Candidates must boot with a default-deny egress policy applied *at create
time*. In `azure-containerapps-sandbox` 0.1.0b4, creating a sandbox from a `snapshot_id` rejects an
`egress_policy` and a restored sandbox doesn't inherit one, so a snapshot-based pool would start
unprotected. Committing the primer to a **disk image** avoids that: every candidate is created fresh from
the image *with* its egress policy.

## Security model

- **Isolation** — each candidate runs in its own microVM (own kernel, own memory), not a shared-kernel
  container. A candidate can't see the orchestrator or another candidate.
- **Default-deny egress** — candidate sandboxes get an egress policy of `default_action=Deny` with
  `traffic_inspection=Full`, and **no allow rules**. Every outbound attempt is refused and recorded. (The
  proxy is transparent: the sandbox resolves names with normal DNS and the proxy intercepts the connection.
  A name that doesn't resolve fails inside the sandbox, so it's blocked but never shows up in the log.)
- **Keyless everywhere** — the orchestrator uses its managed identity for the data plane and the model; the
  sandbox group's identity pulls the engine image from ACR. No secret is ever placed inside a candidate
  sandbox — there's nothing there to steal.
- **The egress audit is the control** — tests and the static scan can be fooled; the platform's record of
  what the code actually tried to reach cannot. That's what catches the rogue candidate.

## Backends

The race loop talks to a `SandboxBackend` interface, so the same loop runs three ways:

| Backend | Isolation | Egress audit | Use |
|---|---|---|---|
| `aca` | ACA Sandboxes microVM | the platform's egress decisions | the real demo, on Azure |
| `docker` | a Docker container | a local deny-all **sentinel** proxy logs every attempt | laptop dev, no Azure |
| `fake` | none (in-memory) | scripted | fast, hermetic tests |

The local sentinel means even the laptop demo shows a genuine "blocked and recorded" egress trail.

## Observability

The orchestrator emits OpenTelemetry spans for the run, each wave, and each candidate's create → health →
verify → audit → delete. The referee reports its in-sandbox phase timings (compile, test, lint) as offsets,
which the orchestrator stitches into the same trace as child spans — so one Application Insights
transaction shows the whole loop, including time spent *inside* each sandbox, with no telemetry egress
required from the candidate. On Azure you can also opt each sandbox into platform `telemetryConfig`
(stdout, metrics, and every egress decision) to Application Insights.
