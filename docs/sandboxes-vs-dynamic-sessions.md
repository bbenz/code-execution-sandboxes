# Sandboxes vs. Dynamic Sessions (and why this demo uses Sandboxes)

Azure Container Apps has two isolated-execution models. Both run work outside your app in a secure
boundary; they differ in how much control you get.

| | Dynamic Sessions | **Sandboxes** (used here) |
|---|---|---|
| Control model | a pool-managed execution slot behind one endpoint | individual compute instances you create and control |
| Main unit | session pool + session identifier | sandbox group + sandbox |
| State | pool-managed, ephemeral | explicit lifecycle, suspend modes, snapshots, volumes, files |
| Image model | code interpreter or custom container | OCI disk images, snapshots, committed images |
| Networking | basic isolation; egress on/off at the pool | per-sandbox egress policy, audit, VNet, ingress ports |
| Access | HTTP through the pool endpoint with an identifier | direct SDK/CLI control of each sandbox |
| Best for | managed code execution where you don't manage instances | programmable isolated compute where lifecycle, image, and network control matter |

This demo needs the control Sandboxes expose, so the abstract's "session pool / prewarm / probes /
identifiers" vocabulary is implemented on **Sandboxes**:

- **A pool of ready engines** → the orchestrator keeps its own pool of sandboxes booted from a primed disk
  image, created and deleted per candidate. It controls each one's lifecycle, which is exactly the thing
  Dynamic Sessions abstracts away.
- **Prewarmed capacity** → a primer sandbox is primed once and **committed to a disk image**; every
  candidate boots from it and builds offline. (Pin exact images, or discover the newest by label.)
- **Health probes** → `referee health` is the startup gate: toolchains, caches, `/work`, and — crucially —
  that outbound HTTPS is blocked. A sandbox that fails never receives a candidate.
- **Lifecycle policies and cleanup** → explicit delete per candidate, plus auto-suspend (60 s) and
  auto-delete (10 min) as a backstop, plus a label sweeper and the group's `maxSandboxCount`.
- **Tenant-aware identifiers** → per-candidate labels (`ces-run`, `ces-candidate`, `ces-tenant`) route and
  clean up work, the Sandboxes analog of a session identifier.
- **Network controls, identity, monitoring** → default-deny egress with an audit, keyless managed identity
  throughout, and one Application Insights trace across every sandbox.

**When Dynamic Sessions would be the better fit:** you want a managed code-execution endpoint, a session
identifier is enough to route repeat calls, and you don't need to own each instance's image, egress policy,
or state. For a verification loop that must boot each attempt from a custom engine with a locked-down
network and read back what the code tried to reach, Sandboxes are the right surface.

Sources: [Sandboxes overview](https://learn.microsoft.com/azure/container-apps/sandboxes-overview),
[Dynamic Sessions vs. Sandboxes](https://sandboxes.azure.com/docs/sandboxes/dynamic-sessions-vs-sandboxes).
