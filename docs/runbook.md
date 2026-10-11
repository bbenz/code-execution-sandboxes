# Presenter runbook

A 20–30 minute live flow. The recorded coding agent makes the race deterministic, so you can rehearse the
exact beats. Keep a terminal and the race board side by side.

## Before you start

- [ ] Deployed to Azure (`azd up`) **or** local Docker ready (`./scripts/run_local.ps1`).
- [ ] `python scripts/smoke.py` prints `SMOKE TEST: PASS` against your backend.
- [ ] Board open; **Token bucket never refills under steady polling** selected.
- [ ] (Azure) Application Insights open on the end-to-end transaction search.
- [ ] (Optional) A second sandbox pre-created in the portal to show the microVM up close.

## The arc

**1. The problem (2 min).** A coding agent that writes code needs somewhere safe to *run* it — to compile,
test, and retry. Running untrusted, agent-written code on your box, or in a shared-kernel container, is the
risk. Pamela's swarm fanned out *reading*; this fans out *running code*.

**2. The custom engine (3 min).** Show `engine/Dockerfile`: JDK 21 + Maven, Go, Python + pytest + ruff, the
referee CLI, and the broken project — packaged as one image. This is the "custom code execution engine".
The built-in interpreter couldn't do a real Maven build; this can.

**3. Prewarming (2 min).** Show the image pipeline (`docs/architecture.md`): prime the caches once, commit
to a disk image, and every candidate boots from it and builds **offline in seconds**. Optionally run
**Benchmark: cold vs. primed** on the board to show the build-time drop.

**4. The race (8 min).** Start the race. Narrate as the lanes light up:
   - Several candidates **fail** — wrong fix, doesn't compile.
   - One **edits a test to make it pass** → disqualified for tampering.
   - One **passes every test** but lights up red on the egress row: *it tried to POST a secret to an
     outside host, and the sandbox blocked and recorded it.* This is the money moment — pause here.
   - No winner yet, so the loop **feeds the failures back** and runs **wave 2**.
   - Wave 2 produces two correct fixes; the **smallest diff wins**. 👑

**5. Why it's safe (4 min).** Click the rogue lane. Show the egress audit entry. Make the point:
   *its tests were green and the static scan only shrugged — isolation plus the egress audit is what caught
   it.* Then show the security model: microVM per candidate, default-deny egress, keyless auth, nothing to
   steal inside the box.

**6. Health, lifecycle, identifiers (3 min).** Tie each board detail back to the abstract's vocabulary:
   the **health probe** gates each sandbox (including "network locked"); **lifecycle** = explicit delete +
   auto-suspend/auto-delete + the sweeper; **identifiers** = the per-candidate labels. Mention Sandboxes vs.
   Dynamic Sessions (`docs/sandboxes-vs-dynamic-sessions.md`): you chose Sandboxes for exactly this control.

**7. One trace (2 min, Azure).** Open the Application Insights transaction for the run: the loop, each
sandbox's create → health → verify → delete, and the compile/test phases *inside* each sandbox — one trace,
with no telemetry egress needed from the candidate.

**8. Close (1 min).** The pattern: give each agent attempt its own disposable, isolated, bounded sandbox;
judge it on tests **and** on what its code tried to do; loop until one wins. Point to the repo.

## Live-model variant

With a model deployed, switch the coding agent to **Live model** and tick **Inject a rogue candidate**. Now
the real model writes the fixes and the race is non-deterministic, while a known rogue is mixed into wave 1
so the egress-catch beat still lands. Keep the recorded generator as your rehearsed fallback — if the model
is slow or flaky, flip back and the demo is identical.

## If something goes wrong

- **A lane is stuck on "Booting sandbox"** — sandbox cores quota or a cold custom-image prepare on the
  first run. Say so; it resolves. `scripts/sweep.py` clears leftovers from a previous run.
- **The live model errors** — flip the coding agent to **Recorded** and re-run. Same story, deterministic.
- **The egress catch doesn't show** — confirm the audit window (`EGRESS_AUDIT_SECONDS`, default 30 s) and
  that the rogue is in the wave; on Azure the egress decision log can take a few seconds to refresh.
- **Everything is red** — run `python scripts/smoke.py`; it isolates create → health → verify → audit.
