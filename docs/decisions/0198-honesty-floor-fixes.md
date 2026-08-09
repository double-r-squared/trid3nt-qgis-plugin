# 0198 - Honesty-floor fixes: dev-tool-invoke error surfacing + shared watershed delineation

Status: accepted
Date: 2026-08-08
Relates: 0196 (telemac RoG build - flagged both defects), 0193 (pysheds watershed
mesh), 0114 (dev-tool-invoke / !run seam). ErrorCode contract: contracts ws.py A.6.

Two defects flagged during the ADR 0196 RoG build, both fixed here.

## Defect 1 - dev-tool-invoke swallowed typed tool errors (honesty-floor)

### Context

`_dispatch_tool_and_persist` (server.py) is the `/invoke` + `!run`
(`dev-tool-invoke`) surface. It is dispatched via `asyncio.create_task` with NO
awaiter. It caught only `ToolNotFoundError` and `PayloadWarningCancelledError`;
every OTHER typed tool exception (`MeshAcquisitionError`,
`TelemacRainOnGridError`, `HydrologyUpstreamError`, `HydrologyAoiTooLargeError`,
...) escaped the task -> an "asyncio Task exception was never retrieved" log line
while the CLIENT received no `error` envelope. The plugin silently showed
nothing; the showcase seeder saw NO_RESULT. Reproduced live 3x (agent.log
warnings on 2026-08-08 up to 16:05:42, e.g. a `MeshAcquisitionError` degenerate
catchment swallowed silently).

### Decision

A broad `except Exception` after the two typed catches (and after the
`CancelledError` re-raise) routes through `_send_error` so a failing direct
invocation always reaches the client as a structured `error`.

The A.6 `ErrorCode` Literal is a CLOSED set, so a tool's own code (e.g.
`TELEMAC_ROG_POUR_POINT_OFF_DEM`) cannot be the wire `error_code` - constructing
`ErrorPayload` with an out-of-enum code raises INSIDE `_send_error` and (per the
documented ws.py CONTEXT_WINDOW_EXCEEDED bug) skips the send entirely, the very
silence this fix closes. So the catch:

- passes a tool's typed code through only when it is already a valid `ErrorCode`
  (so `DEM_SOURCE_UNAVAILABLE`, `TOOL_TIMEOUT`, and an upstream-provider
  `LLM_UNAVAILABLE` ride the wire verbatim - NOT internalized), else
- falls back to `INTERNAL_ERROR` with the specific code LEADING the message as a
  `[MARKER]` (house convention, see `_notify_layer_auto_publish_failed`) - honest
  and greppable, no enum widening. `retryable` is harvested from the exception.

`_VALID_ERROR_CODES = frozenset(get_args(ErrorCode))` gives the runtime membership
test.

### Live proof

Mid-ocean `!run telemac_rain_on_grid(pour_point=(0.0, 0.0), ...)` through the
seeder client returned an `error` envelope:
`INTERNAL_ERROR | [HYDROLOGY_UPSTREAM_ERROR] fetch_copernicus_dem failed ... no
'cop-dem-glo-30' item intersects bbox` (retryable=true) - NOT a silent
no-result. agent.log shows the new `/invoke directive tool raised ...
code=HYDROLOGY_UPSTREAM_ERROR` line and ZERO "Task exception was never retrieved"
after the fixed daemon started.

## Defect 2 - shared delineate_watershed coordinate-snap sliver bug

### Context

`delineate_watershed` traced the catchment with `grid.catchment(xytype=
"coordinate")`, whose coordinate->cell round-trip can land on a NEIGHBOUR cell and
collapse the basin to a 1-cell sliver on certain grid alignments. The RoG mesh
step already had a proven robust delineation
(`mesh_acquisition._delineate_catchment`): snap the outlet to the
MAX-accumulation cell in an 8-cell window, then trace in INDEX space
(`xytype="index"`), which is alignment-invariant. Two implementations existed.

### Decision

One shared implementation, `_hydrology_common.snap_and_delineate_index_space`
(snap-to-max-accumulation-window + index-space catchment + polygonize), used by
BOTH callers:

- `delineate_watershed` keeps its public contract (same args incl.
  `snap_threshold`, same `WatershedLayerURI`, same emitted layer): the existing
  `_snap_to_stream` coarse snap onto a channel is retained (handles a pour point
  clicked far off any stream), then the shared helper does the fine
  window-refine + INDEX-space trace.
- `mesh_acquisition._delineate_catchment` delegates to the same helper, keeping
  its geodesic-area convention and typed `TELEMAC_ROG_POUR_POINT_OFF_DEM`
  off-DEM error.

### Live proof

Re-seeded the Coweeta RoG showcase (`--only telemac_rain_on_grid`, natural
prompt "Otto, North Carolina", pour point -83.40402/35.05746). GREEN: mesh
acquired 4986 nodes / 9781 cells / 30.51 km^2 (EPSG:32617), two max-depth raster
layers persisted (1 survived the reconnect verify) - vs the pre-fix coordinate
path that collapsed the same outlet to 2 D8 cells (0.002 km^2) and errored.
New showcase case id: 01KZJ5MR3N6GVXTETAP3924D23 (supersedes the ADR 0196 C3
Coweeta showcase case).

## Offline

- `test_hydrology_primitives.py`: +2 (convergent-bowl DEM where the coordinate
  path slivers to ~14 cells while index space captures the basin; alignment-
  invariance across sub-cell origin shifts). +the existing suite green.
- `test_telemac_rain_on_grid_mesh_acquisition.py`: unchanged green (the
  index-space + degenerate-guard tests already covered the delegated path).
- `test_dev_tool_invoke_handler.py`: +3 (typed tool code -> INTERNAL_ERROR +
  `[MARKER]`; a valid-enum tool code passes through; an untyped exception ->
  `[TOOL_EXECUTION_FAILED]`).
- Full suite slices [a-e]/[f-o]/[p-r]/[s-z]: 9 failures, all the exact baseline
  (fetch_resolution x4 + river_dye x5), zero regressions.

## Consequence

- Every failing direct (`/invoke`, `!run`) tool invocation reaches the client as
  a structured `error` instead of dying silently on the create_task path; the
  specific tool code is preserved as a `[MARKER]` in the message with no enum
  widening.
- One watershed delineation implementation shared by the tool and the RoG mesh
  step; the sliver-prone coordinate path is gone from the product surface.
- +0 registered tools; contract enum unchanged.
