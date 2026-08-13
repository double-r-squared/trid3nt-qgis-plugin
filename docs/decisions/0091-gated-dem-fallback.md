# ADR 0091 -- Gated cross-dataset DEM fallback (fetch_dem 3DEP -> Copernicus)

Status: accepted (2026-08-03)
Follows: ADR 0090 (fetch_dem STOP -- named the cross-tool provenance restamp as
the fold's biggest blocker). Implements the IDEAS.md 2026-08-03 norm "Loud,
user-gated cross-dataset fallbacks" (NATE).

## Context

`fetch_dem` (`source="auto"`) silently failed 3DEP (US 1-10 m LIDAR) over to
`fetch_copernicus_dem` (global 30 m RADAR) on a 3DEP service outage / timeout,
restamping `LayerURI.name` + `fallback_note`. That is a CROSS-DATASET swap: a
different measurement method at a coarser resolution. Silently substituting it
degrades map integrity while looking like success. The norm splits fallback
classes: same-data mirrors (identical dataset, different host) may fail over
silently; a cross-DATASET substitution must be LOUD and USER-GATED.

## Decision -- the auto path STOPS and asks; it never substitutes on its own

`source="auto"` still tries 3DEP first. On a 3DEP SERVICE failure it no longer
calls Copernicus; it raises `DemAutoFallbackGateError` (a retryable
`UpstreamAPIError`, `error_code="DEM_FALLBACK_GATE"`) whose message (a) states
3DEP failed and why, (b) names the explicit retry `source="copernicus"`, and
(c) states the tradeoff (30 m global radar vs 1-10 m lidar -- coarser terrain,
different measurement method). A clearly non-US bbox is caught BEFORE the 3DEP
attempt by a generous US-coverage envelope check and raises the DISTINCT
`DemOutOfCoverageError` (`error_code="DEM_OUT_OF_COVERAGE"`), also naming
copernicus. Explicit `source="copernicus"` and `source="3dep"` are UNCHANGED
(the user already chose).

## Mechanism chosen -- typed retryable error, NOT the granularity-gate card

Two candidate envelopes were considered:

1. The #154 granularity-gate confirm card (`_build_swmm_granularity_envelope` /
   `FETCH_CONFIRM_TOOLS` / `GranularitySuggestion`) -- a PRE-dispatch card the
   server raises before the tool runs, on parameters known up front.
2. A typed retryable error carrying a `.suggestions` list, surfaced by
   `summarize_tool_result` and riding the tool-retry loop (the same envelope the
   pinned `source="3dep"` error already uses).

Chosen: (2). Reason: the substitution decision is only knowable AFTER the 3DEP
attempt FAILS at runtime -- 3DEP service health is not a pre-dispatch parameter,
so a confirm card (which must know its inputs before the call) cannot express
it. The typed retryable error is exactly the mechanism IDEAS.md and the
data-source-fallback norm prescribe: tool errors feed back as `function_response`
so the agent narrates the tradeoff and the USER approves the swap
conversationally by retrying with `source="copernicus"`. It reuses the existing
`.suggestions` structured-recovery envelope (no new machinery) and keeps the
happy path byte-identical (a healthy 3DEP fetch never constructs the error).

## Per-consumer disposition (8 direct importers of `fetch_dem`)

The end-state per consumer: the gated error PROPAGATES honestly (pause-and-ask);
never swallowed to a success shape. Verified by the forced-failure drive + the
consumer test suites (412 passed).

| Consumer | Call site | Disposition |
|---|---|---|
| `flood.py` (`sfincs_flood`) | `_fetcher_chain` land branch | PROPAGATES: the fetcher-chain `except` builds a typed FAILED `AssessmentEnvelope` (`envelope_type=modeled`, empty `layers`, `solver_version=failed:DEM_FALLBACK_GATE`, `workflow_name=...:FAILED:DEM_FALLBACK_GATE`). Honest failure, not success-shaped. Residuals: (a) no `dem_source` passthrough through the deep `model_flood_scenario` signature -- named, not wired; (b) `_build_failed_envelope` threads `error_code` but not `error_detail`, so the copernicus PROSE is not narrated (a pre-existing, general limitation affecting all fetcher failures, not DEM-specific). |
| `fetch_topobathy` | `_fetch_3dep_land_to_file` nested land leg | SWALLOWED BY DESIGN (best-effort land leg): `except Exception` -> `None` -> merge continues on bathy. This is topobathy's OWN internal fallback, explicitly scoped as a characterize-only FOLLOW-UP by the kickoff (see the ledger row + the characterization paragraph). Not changed this wave. Residual named. |
| `compute_contours` | `_resolve_dem_uri` | PROPAGATES: `fetch_dem(bbox)` is unwrapped; the gate error rises out of the registered tool to the dispatch site -> honest error envelope with `.suggestions`. No change. |
| `extract_model_at_observations` | `_fetch_ground_dem` | PROPAGATES (converted to a typed error): caller catches, logs, then raises `PairingQuantityMismatchError` with an escape hatch (pass `ground_elevation_uri`). Honest, not success-shaped. Residual: the copernicus `.suggestions` is subsumed into the pairing-error guidance rather than surfaced structurally. No change. |
| `run_elmfire` | DEM input leg | PROPAGATES: wrapped in `ElmfireWorkflowError("ELMFIRE_INPUT_FETCH_FAILED", f"... {exc}")`; the gate message (copernicus + tradeoff) rides in the prose. `.suggestions` lost in wrap. Residual named. No change. |
| `model_dambreak_geoclaw_scenario` | `_fetch_topo_for_geoclaw` (fetch_dem = fallback of topobathy) | PROPAGATES: caught -> `GeoClawComposerError("GEOCLAW_DEM_FETCH_FAILED", f"both DEM sources failed ... {exc}")`; gate message in prose. No change. |
| `model_urban_flood_swmm` | `_fetch_dem_for_urban` (fetch_dem = 10 m fallback of 3DEP-extra 1 m) | PROPAGATES: caught -> `UrbanFloodWorkflowError("SWMM_DEM_FETCH_FAILED", f"... {exc}")`. The 3DEP-1m -> 3DEP-10m ladder is a SAME-dataset resolution mirror (norm-permitted silent). No change. |
| `model_landslide_scenario` | `_fetch_dem_for_landslide` (same pattern as SWMM) | PROPAGATES: caught -> `LandslideWorkflowError("LANDLAB_DEM_FETCH_FAILED", f"... {exc}")`. No change. |

No scenario signature was redesigned; passthroughs were named as residuals per
the kickoff (none were a small mechanical addition given the depth of the
signatures).

## Consequences

- `fetch_dem` no longer imports/calls `fetch_copernicus_dem` on the auto path.
  The silent name/`fallback_note` restamp (ADR 0090 blocker 1, "cross-tool
  provenance restamp") is DISSOLVED -- that blocker no longer exists, though the
  fetch_dem FOLD itself is a separate future job (blockers 2-4 stand: the
  bespoke `py3dep` leg + bounded timeout + coverage gate remain).
- Two new typed errors: `DemAutoFallbackGateError` (`DEM_FALLBACK_GATE`) and
  `DemOutOfCoverageError` (`DEM_OUT_OF_COVERAGE`), both retryable, both carrying
  `.suggestions`.
- A generous US-coverage envelope (`_US_3DEP_COVERAGE_ENVELOPES`: CONUS + AK +
  the Aleutian antimeridian tail + HI + PR/USVI). Deliberately generous so a
  border-straddling bbox falls through to a real 3DEP attempt -- a
  misclassification can only DOWNGRADE the distinct out-of-coverage message to
  the outage gate (which still names copernicus), never turn a foreign miss into
  a false success.
- Offline baseline UNCHANGED at EXACTLY 9 failures (`test_fetch_resolution_gate`
  x4 + `test_run_river_dye_scenario` x5). The `[fetch_dem-dem]` /
  `[fetch_topobathy-topobathy]` gate members fail IDENTICALLY pre/post
  (`assert 'local' == 'fetch'`, unrelated to this change -- diffed empty).
- Coded-tool / coded-fetcher counts UNCHANGED (behavior change inside an existing
  fetcher; no new registered tool).
- FLOOD CANARY: happy path (3DEP up) status=ok + depth COG published (gate
  invisible when the primary works); forced-failure drive proves the gate text
  end-to-end + honest propagation through `sfincs_flood`.
