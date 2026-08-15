# ADR 0261 - server.py refactor wave 1: package skeleton + errors/config extraction + dispatcher rename

Status: LANDED (2026-08-14). First wave of a strictly behavior-preserving,
extraction-over-rewrite series that turns the 12,979-line
`server/src/trid3nt_server/server.py` monolith into a
`trid3nt_server/server/` package. This wave establishes the package pattern all
later region waves follow, extracts the two lowest-coupling regions (the typed
error taxonomy and the env-knob config helpers), and renames the core turn
dispatcher off its Gemini-era name. Zero logic/signature/side-effect-order
changes.
Date: 2026-08-14
Supersedes-nothing (opens the server-refactor series; recon map at
`docs/design/server-refactor-recon-2026-08-14.md`).

## Context

`server.py` was one module of 12,979 lines, 167 top-level defs, 9 classes, with
a small fan-in (4 importers: `main`, `telemetry`, `tool_catalog_http`,
`cases/ingest_user_layer`) and a large test surface (11,523 collected tests,
119 references to `trid3nt_server.server`). The recon flagged Gemini vocabulary
residue (the misnamed dispatcher `_dispatch_gemini_and_persist`, which today
drives the pluggable model path, not Gemini) and cloud-era leftovers. NATE
ordered a refactor. The constraint that dominates the design: many tests
MUTATE module attributes -- `monkeypatch.setattr(server, "get_persistence",
...)`, `setattr(server, "_SYNC_OFFLOAD_MODE", ...)`, ~40 such sites -- and the
monolith's own code reads those names as bare module globals. A naive
`from ._core import *` bridge would give the package and `_core` two separate
bindings, so a patch on the package would NOT reach `_core`'s internal
references. That would be a behavior change.

## Decision

### The package pattern (the reusable part every later wave inherits)

`server.py` -> `server/_core.py` (the module of record; shrinks wave by wave).
The package `__init__` installs a `ModuleType` subclass facade over the package
module object whose `__getattr__` / `__setattr__` / `__delattr__` proxy
straight to `_core`. Result: `trid3nt_server.server.X` READS resolve to
`_core.X`, and monkeypatch-style WRITES rebind `_core.X` so `_core`'s own
internal references observe the patch -- exactly the single-namespace semantics
the monolith had. No importer and no test changes behavior; symbols the
monolith exposed at `trid3nt_server.server.X` still resolve there.

Because `_core` sits one package level deeper, its 46 single-dot relative
imports (`from .main` ...) became double-dot (`from ..main` ...). Sibling
package modules are imported into `_core` BY NAME (`from .errors import ...`,
`from .config import ...`) so bare-global references and facade-proxied
monkeypatch targets keep resolving.

### Wave-1 extractions

- `errors.py`: the typed error taxonomy -- `ToolNotFoundError`,
  `PayloadWarningCancelledError`, `CodeExecConfirmationCancelledError`,
  `CodeExecApprovalTimeoutError`, `SolverConfirmationCancelledError`,
  `SpatialInputInvalidResponseError`. Pure exception types (an `error_code` +
  `retryable` flag `summarize_tool_result` harvests); no state. Moved verbatim,
  docstrings swept to model-neutral wording.
- `config.py`: the pure `env -> value` helpers -- `_tool_retrieval_k`,
  `_tool_retrieval_mode`, `_code_exec_approval_timeout_s`, `_env_flag`,
  `_ambiguity_margin_threshold`, `_tool_choice_timeout_s`,
  `_catalog_offer_ttl_s` -- plus their private constants
  (`_TOOL_RETRIEVAL_VALID_MODES`, `_TOOL_RETRIEVAL_MODE`,
  `CODE_EXEC_CONFIRM_TIMEOUT_SECONDS`, `CODE_EXEC_APPROVAL_TIMEOUT_DEFAULT_S`).
  Session-coupled helpers that merely READ env (`_session_routing_mode`, which
  takes a `SessionState`) stayed in `_core`.

### The dispatcher rename

`_dispatch_gemini_and_persist` -> `_dispatch_model_turn_and_persist` at all 9
sites (definition, call site, `__all__`, and comment mentions). Gemini
vocabulary in the docstrings/comments touched by the rename was swept to "the
model". Per comments-are-constraints-never-history, the sweep was scoped to the
renamed function only; the remaining ~60 Gemini and 9 grace identifiers in
`_core` belong to the region waves that own them (no whole-file sweep).

## Consequence

- `_core.py` = 12,685 lines (was 12,979; -294 net: 277 lines of moved defs +
  edits). It shrinks further each wave.
- Behavior preserved: the facade makes reads and monkeypatch writes transparent;
  moved symbols are re-imported by name so isinstance identity holds across the
  package boundary and internal bare-global references are unchanged.
- The package pattern is now the template: a region wave adds a sibling module,
  moves defs into it verbatim, imports them by name into `_core`, and the facade
  re-exposes them at `trid3nt_server.server.X` for free.
- NOT this wave (wave-2 chop list, deferred): the aws-batch backend switch,
  TiTiler style fallbacks, the dormant adapter.py Vertex path, and DynamoDB TTL
  residue. Those are live-but-cloud-shaped seams that need a delete decision,
  not a move -- registered as the next wave.
