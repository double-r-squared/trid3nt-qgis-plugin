# gates/ -- agent-loop safety and routing gates

`trid3nt_server/gates/` holds the agent-loop
gates: user-decision cards, tool-gating/retrieval, runaway/circuit guards,
context-budget, and actionability classification.

## What lives here

- `cards/` -- the user-decision gate cards: `estimate`, `payload_warning`,
  `solver_confirm`, `spatial_input`. Each card is a DECLARED gate whose pure
  estimate/pin providers are owned by the engine.
- `tool_gating.py`, `pending.py` -- visible-tool gating + pending-decision
  registry.
- `runaway_guard.py`, `circuit_breaker.py` -- loop-runaway + repeated-failure
  guards.
- `context_budget.py` -- token budget + compaction labels (also drives the
  model-discovery `reset_num_ctx_cache` seam).
- `input_review.py`, `actionability.py`, `spatial_input.py` -- input-review
  gate + exception-actionability classifier (`{agent, user, operator}`).
- `draw_input.py`, `spatial_roles.py` -- the DRAW gate that asks for one
  declared param's geometry on the canvas, and the shared role vocabulary and
  parser the mesh authoring layer reads a drawn `FeatureCollection` through.
- `fallback.py` -- the ONE fallback gate: the loudness floor over the
  pending-confirm spine, where a `synthetic` rung always pauses and its
  labeled default is REFUSE.

## Composition

`cards/*` import (deferred, function-local) from `data/`, `workflows/`, and
`mesh/` to compute estimates -- absolute cross-package imports since these are
now peer top-level packages. The GateSpec confirm engine + the shared gate-wait
seam + the four user-decision emit-wait gate families (payload, code-exec,
solver-confirm, spatial) live in `confirm.py`. The server callers import those functions
function-locally to keep the `server <-> gates` package edge acyclic.

## The mesh on the gate

There is no second gate machine. `workflows/mesh/gate.py` builds the round's
CARD for the mesh under construction and hands it to `gate_input_review` like
any other user-gated thing: under USER-GATED the built mesh is presented as an
editable MDAL layer (through `render.publish_input_layer`) plus its numeric
probes quoted as the card's lines, and its rows are the one size word, the
numbered recipe ops (read-only - `mesh_op` is where an op is written), the
revert and the path of a hand-edited layer to adopt. `proceed` accepts the mesh,
`narrow_scope` applies the reply back onto the session and re-presents, `cancel`
refuses the run. AUTO, and a headless call with no session to present on, builds
inline: no card, no layer.

Two general capabilities on the one gate make that possible, and neither names a
mesh: `present` (a per-round `GateCard` - lines, sheet and envelope `tool_args` -
from a caller that owns the thing under review) and `apply_revision` (a reply
taken as a change to that thing, then another round). `ReviewOutcome.cancel_code`
says which cancel it was, so a caller raises its own typed refusal.

## Invariants / extension points

- Gates on tools are DECLARED (GateSpec metadata + pure providers), NEVER
  hand-wired in server code.
- A DECLINE IS NOT AN ERROR. A card answered with `cancel` raises a
  `UserDeclinedError` carrying the gate's own wire code
  (`PAYLOAD_WARNING_CANCELLED`, `CODE_EXEC_CANCELLED`,
  `SOLVER_CONFIRMATION_CANCELLED`). Three seams read its `declined` marker: the
  pipeline emitter marks the step CANCELLED rather than failed, the result
  summarizer hands the model `status="declined"` naming the card and what it
  asked, and the circuit breaker leaves the tool's retry budget alone.
- A TIMEOUT IS NOT A DECLINE. A card nobody answers before its deadline raises
  `GateConfirmationTimeoutError` at all three gates: `CONFIRMATION_TIMEOUT` on
  the wire and on the model's result, the step marked FAILED rather than
  cancelled, and a narration that says the card expired unanswered and never
  that the user declined it.
- INPUT_REQUIRED has two modes (AUTO labeled-defaults vs USER-GATED); the
  model never invents physics for un-fetchable inputs.
- ONE gate machine presents, asks and accepts every user-gated thing. A caller
  with a thing of its own under review supplies a card and a revision handler;
  a gate loop, a card contract or a tool surface of its own is a defect.
