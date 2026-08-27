# gates/ -- agent-loop safety and routing gates

`trid3nt_server/gates/` (was `agent/gates/`, ADR 0277) holds the agent-loop
gates: user-decision cards, tool-gating/retrieval, runaway/circuit guards,
context-budget, and actionability classification.

## What lives here

- `cards/` -- the user-decision gate cards: `estimate`, `payload_warning`,
  `region_choice`, `solver_confirm`, `spatial_input`, `credential`. Each card
  is a DECLARED gate whose pure estimate/pin providers are owned by the engine.
- `tool_gating.py`, `pending.py` -- visible-tool gating + pending-decision
  registry.
- `runaway_guard.py`, `circuit_breaker.py` -- loop-runaway + repeated-failure
  guards.
- `context_budget.py` -- token budget + compaction labels (also drives the
  model-discovery `reset_num_ctx_cache` seam).
- `input_review.py`, `actionability.py`, `spatial_input.py` -- input-review
  gate + exception-actionability classifier (`{agent, user, operator}`).

## Composition

`cards/*` import (deferred, function-local) from `data/`, `workflows/`, and
`mesh/` to compute estimates -- absolute cross-package imports since these are
now peer top-level packages. The GateSpec confirm engine + the shared gate-wait
seam + the five user-decision emit-wait gate families (payload, code-exec,
solver-confirm, credential, region, spatial) now live in `confirm.py` (ADR 0278,
evicted from `server/_core`). The server callers import those functions
function-locally to keep the `server <-> gates` package edge acyclic.

## The mesh gate loop

`workflows/mesh/gate.py` rides this same spine for the one gate whose card is
not the whole interaction. Under USER-GATED a built mesh is presented as an
editable MDAL layer (through `emission.publish_input_layer`) plus its numeric
probes, and the gate MOUNTS one agent tool per edit action the building mesher
registered -- `mesh_edit_<action>`, plus `mesh_accept` and `mesh_restart` --
into `TOOL_REGISTRY` for exactly as long as the session is open. A mounted tool
is unrankable (the retrieval index predates it), so `MOUNTED_TOOLS` is a
visibility floor in `tool_retrieval` and in the openai tool gate. AUTO builds
inline: no card, no layer, no mounted tools.

A DEMANDED build (a plan step pulling `MESH`) additionally parks on the
pending-confirmation future like any other card: `proceed` accepts,
`narrow_scope` carries `{"restart": true}` or `{"edit": "<action>", ...inputs}`
and re-presents, `cancel` refuses the run.

## Invariants / extension points

- Gates on tools are DECLARED (GateSpec metadata + pure providers), NEVER
  hand-wired in server code.
- INPUT_REQUIRED has two modes (AUTO labeled-defaults vs USER-GATED); the
  model never invents physics for un-fetchable inputs.
- A mounted tool never shadows a registered one, and never outlives the thing
  it acts on: the mount seam refuses a duplicate name and the gate unmounts in
  a `finally`.
