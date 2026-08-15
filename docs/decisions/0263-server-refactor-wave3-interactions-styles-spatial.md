# ADR 0263 - server-refactor wave 3: interactions / styles / spatial extraction

Status: LANDED (2026-08-14). Wave 3 of the server-refactor series (ADR 0261 =
wave 1 package skeleton + errors/config; ADR 0262 = wave 2 cloud-seam chop).
Strictly behavior-preserving pure moves: three low-coupling regions extract out
of `_core.py` into sibling package modules, and the package facade generalizes
so a monkeypatch write reaches whichever module now owns the binding.
Date: 2026-08-14
Supersedes-nothing (continues ADR 0261/0262; recon map at
`docs/design/server-refactor-recon-2026-08-14.md`).

## Context

Wave 1 established the package pattern: `_core.py` is the module of record, the
`__init__` facade proxies attribute reads and monkeypatch writes to `_core`, and
a region wave adds a sibling module + re-imports its names by name into `_core`.
Wave 3 extracts the next three lowest-coupling regions the recon flagged: the
pending-interaction registries, the raster-style/publish-preset helpers, and the
bbox/AOI + spatial pending-input registries. SessionState (the ~4,700-line
class) and the dispatch machinery are deliberately NOT touched -- anything
entangled with them stays in `_core`, flagged for the session wave.

## Decision

### Extractions

- `interactions.py` (18 symbols, 465 LOC): the three pending-interaction gates,
  each the same `register` / `pop` / `resolve` owner-checked shape.
  - tool-choice (ADR 0018 tool-candidates card): `_PENDING_TOOL_CHOICES`,
    `_register_pending_tool_choice`, `_pop_pending_tool_choice`,
    `_resolve_pending_tool_choice`.
  - catalog offers (FR-DS Mode 2 offer-to-add): `_PENDING_CATALOG_OFFERS`,
    `_CATALOG_OFFER_MAX`, `_prune_catalog_offers`, `_register_pending_catalog_offer`,
    `_pop_pending_catalog_offer`, `_slug`, `_probe_catalog_endpoint_sync`,
    `_probe_catalog_endpoint`, `_complete_catalog_entry`,
    `_handle_catalog_addition_response` (the probe + entry-completion + overlay
    append flow).
  - credentials: `_PENDING_CREDENTIALS`, `_register_pending_credential`,
    `_pop_pending_credential`, `_resolve_pending_credential`.
- `styles.py` (5 symbols, 85 LOC): the publish-wrap raster styling seam --
  `_FLOOD_DEPTH_STYLE_TOKENS`, `_DEFAULT_FLOOD_DEPTH_STYLE_PRESET`,
  `_is_flood_depth_cog`, `_resolve_publish_wrap_style_preset` (the QGIS
  duplicate-flood-layer safety net), `_is_droppable_object_store_raster`.
- `spatial.py` (13 symbols, 265 LOC): two groups.
  - bbox/AOI helpers (pure, no session state): `_is_finite_bbox4`,
    `_coerce_bbox4`, `_aoi_zoom_to_bbox`, `_last_zoom_to_bbox`.
  - the spatial pending registries (same shape as the interaction gates):
    region-choice (`_PENDING_REGION_CHOICES` + register/pop/resolve) and
    spatial-input (`_PENDING_SPATIAL_INPUTS` + register/pop/resolve +
    `_fail_pending_spatial_input`, the eager typed-error fail path).

Each module carries `from __future__ import annotations` (session/contract types
stay string annotations under `TYPE_CHECKING`, no runtime import, no cycle), a
`logging.getLogger("trid3nt_server.server")` matching `_core` (same singleton by
name), and imports its `.config` / `.errors` / `trid3nt_contracts` deps directly.
`_core` re-imports all 36 moved names by name (mirroring the wave-1 pattern) so
its internal bare-global references and the facade-proxied reads resolve
unchanged.

### The facade generalization (the reusable part)

Wave 1's facade proxied reads and writes to `_core` only. That is sufficient
while every reader of a moved name lives in `_core`. Wave 3 breaks that
assumption: a moved function reads a moved sibling-scope name as its OWN module
global -- `_register_pending_catalog_offer` (now in `interactions`) reads
`_CATALOG_OFFER_MAX`, and `_handle_catalog_addition_response` calls
`_probe_catalog_endpoint`, both bare globals resolving in `interactions`'s
namespace. A test monkeypatching `server._CATALOG_OFFER_MAX` (or
`server._probe_catalog_endpoint`) through the `_core`-only facade rebound
`_core`'s copy, which the sibling function never reads -- a real behavior change
the offline suite caught (`test_offer_registry_capped`).

Fix: `_ServerFacade.__setattr__` / `__delattr__` now write to `_core` AND to any
sibling extraction module (`errors`, `config`, `interactions`, `spatial`,
`styles`) whose `__dict__` already defines the name. This restores the
monolith's single-namespace semantics exactly -- one logical namespace across
the split -- with ZERO test changes. Names living only in `_core` (the vast
majority) still hit `_core` alone; the sibling loop finds no match.

### Flagged entanglements (stay in `_core`, for the session/dispatch wave)

- `_union_pinned_tool`: tool-choice-adjacent but reads `SessionState`
  (`state.allowed_tool_set`) + `TOOL_REGISTRY` -- session-coupled, not moved.
- `_emit_spatial_input_and_wait`, `_handle_request_spatial_input`,
  `_set_drawn_geometry_from_payload`: the drawn-geometry region the recon placed
  with spatial. They live deep in the dispatch loop and take `SessionState`; only
  the pending REGISTRIES moved, the emit+wait gates and geometry writer stay.
- The credential / region-choice emit+wait gates (`_emit_credential_request_and_wait`,
  the region/spatial emit sites) stay in `_core` -- registry accessors moved,
  the loop-integrated emit halves did not.

## Consequence

- `_core.py` = 12,041 lines (was 12,717; net -676 = 722 moved out, +46 the
  three by-name re-import blocks). interactions 465 + styles 85 + spatial 265.
- Behavior preserved: the four directly-affected test files
  (tool-candidates / catalog-offer / credential-pipeline / spatial-input /
  region-choice / duplicate-flood-layer / aoi) pass UNCHANGED -- all reference
  the symbols through the facade; the facade generalization keeps the two
  monkeypatched sibling globals (`_CATALOG_OFFER_MAX`, `_probe_catalog_endpoint`)
  reaching the live binding. No source-inspection test broke (the
  `inspect.getsource(server._core)` / AST tests target `_invoke_tool_via_emitter`,
  `_dispatch_model_turn_and_persist`, `run_server`, and the gate-timeout
  expressions -- all still in `_core`).
- Registration-neutral: no `@register_tool` / spec change.
- GATES (wave close): <FILL: four-slice offline suite, workflows import + registry,
  ws_smoke, flood canary>.
