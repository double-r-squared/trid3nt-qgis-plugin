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
now peer top-level packages. The GateSpec engine currently lives in
`server/_core` (its extraction into this folder is deferred, ADR 0277).

## Invariants / extension points

- Gates on tools are DECLARED (GateSpec metadata + pure providers), NEVER
  hand-wired in server code.
- INPUT_REQUIRED has two modes (AUTO labeled-defaults vs USER-GATED); the
  model never invents physics for un-fetchable inputs.
