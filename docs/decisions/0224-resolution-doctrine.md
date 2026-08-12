# ADR 0224 - Resolution doctrine: native default, explicit coarsening, sampled payload estimation, honest CUDEM fallback

Date: 2026-08-11

Status: accepted

## Context

The SCHISM PaHM surge arc (ADR 0217/0219) fetched bathymetry through a coarse
SCREENING path: `pahm_surge` always passed `screening_res_m` to
`_fetch_bathymetry_cog`, which for any non-None value forced
`skip_cudem=True, skip_land=True` on `fetch_topobathy`. The autoscaled cell
(`_autoscale_surge_domain`, ~199 m for the greater-Galveston domain) was the
SILENT default -- the user never chose it, and the fine NOAA NCEI CUDEM 1/9"
nearshore composite was NEVER read. The surge therefore ran on the GLOBAL ETOPO
2022 15" (~450 m) base, and the proof renders showed blocky ~450 m nearshore
structure instead of real bathymetry.

A follow-up review (the "0221" investigation, which wrote NO ADR -- recorded here)
found the root causes:

- **CUDEM DOES cover Galveston.** The hosted 1/9" collection has tiles that
  intersect the Bolivar/Galveston AOI (8 tiles for the fine bbox
  `[-95.05, 29.2, -94.6, 29.65]`). The ETOPO blockiness was NOT missing coverage
  -- it was `skip_cudem=True` forced unconditionally on the screening path
  (`tidal_hydro.py` when `screening_res_m` is not None; `pahm_surge.py` always
  passed it).
- **The GLOBAL-FALLBACK warning LIED.** `topobathy.py _select_and_merge` emitted
  "CUDEM's hosted 1/9\" collection omits this coast" whenever `cudem_count == 0`
  -- regardless of WHY it was zero. When CUDEM was SKIPPED by the caller (not
  absent), the message asserted a coverage absence that was false.

NATE's rulings (2026-08-11) that this ADR implements:

- **R-A** DEFAULT = NATIVE / MAX resolution. Coarsening is an EXPLICIT user
  declaration (it flips the autoscale-coarse default, it is never the silent
  default).
- **R-B** SAMPLED SIZE ESTIMATION. Estimate payloads by measuring a small native
  window's real emit density, scaling by AOI area; the measured number rides the
  `estimate_payload_mb` metadata seam into the existing payload gate so
  "proceed or coarsen" quotes REAL numbers.
- **R-C** HONEST fallback warning. Only a real tile-index intersect that returns
  zero may claim the collection omits a coast; a caller-skip / an unreachable
  index / a datum-gated-out set each name their own true cause.

## Decision

### R-A -- native default, explicit coarsening (surge bathymetry)

`schism_pahm_surge`: `resolution_m=None` (the default) now fetches NATIVE
bathymetry -- the fine CUDEM 1/9" composite, bounded only by `fetch_topobathy`'s
existing 12000 px-per-side guard. The autoscaled cell is surfaced as the
COARSENING HINT on the payload gate, never the silent fetch resolution. An
explicit `resolution_m` is honored verbatim as a declared coarsening
(`basis="user"`); the native default records `basis="derived"`,
`value="native (CUDEM 1/9\")"`.

`_fetch_bathymetry_cog` is re-cut around `resolution_m: float | None`
(`None` = native) with explicit `force_bathy_base` / `skip_land` levers. The
kwarg decision is a pure helper `_topobathy_fetch_kwargs` (unit-tested). The
3DEP land leg stays dropped for a surge (`skip_land`) -- its 0 m ocean fill would
clobber the ETOPO negative bathy over the open water beyond CUDEM coverage;
CUDEM itself carries the nearshore land above the waterline, so a surge mesh
needs no separate land DEM. The tidal (`coastal_tin`) native caller is byte-
identical (bare `_fetch_bathymetry_cog(bbox)` -> empty kwargs, land included).

### `skip_cudem` survives only as an evidence-based optimization

`skip_cudem` is NOT forced on the screening path any more. It fires only when an
EXPLICITLY-coarsened run requests a cell at/above `_CUDEM_SKIP_RES_M = 500 m` --
coarser than the ETOPO 2022 15" base's own ~450 m native cell, so CUDEM's fine
nearshore structure cannot survive resampling onto the requested grid and reading
dozens of per-tile CUDEM COGs would be wasted network cost with zero fidelity
gain. Below 500 m (including the native default) CUDEM IS read: it materially
refines the nearshore bed. This is the justified threshold the ruling demanded
(not an unconditional skip).

### R-B -- sampled payload estimator

A reusable `agent/tools/payload_sampling.py` seam: `estimate_mb(source_key, bbox,
analytic_mb, sample_fn, resolution_m, px_cap)` measures a source's emit density
(output COG bytes-per-pixel + native pixel density) from a small native window,
scales by AOI area BOUNDED by the 12000 px-per-side cap, and returns a
`SampledEstimate(mb, kind)` where `kind` is `"measured"` or (on sampling failure)
`"analytic"`. Densities cache per `source_key` + coarse region so the gate samples
a region at most once; the analytic fallback stays resolution-aware so the
coarsening suggestion is meaningful even offline.

`fetch_topobathy._sample_topobathy_density` reads a 512-px window from the finest
source tile over the AOI centre (CUDEM where present, else ETOPO), re-encodes it
as the SAME float32 LZW COG the fetch emits to measure real bytes/px, and derives
the native output pixel density from the tile's native cell projected to metres.
One header-range open + one window read -- gate-fast (~6 s cold, cached after).

`fetch_topobathy.estimate_payload_mb` and `schism_pahm_surge.estimate_payload_mb`
delegate to this seam (the surge fetch IS a `fetch_topobathy` call, so they share
the density cache -- never a parallel threshold check). The px cap gives the
measured estimate a CEILING (~620 MB for any AOI at/above the cap area), so a
large domain no longer over-quotes into an unbounded analytic runaway.

### R-B wired -- gate quotes real numbers

Each tool exposes an OPTIONAL `<estimator>_detail` companion returning a one-line
human string carrying the MEASURED-vs-analytic kind + a concrete coarsening
suggestion. The payload gate (`server._maybe_gate_on_payload_warning`) resolves it
and APPENDS it to the card `recommendation` -- no new envelope field, no new WS
event. The surge native card reads e.g. "native bathymetry ~620.5 MB (measured);
suggested coarsening 199 m ~1.9 MB; proceed native / coarsen (resolution_m=199) /
cancel". The estimator call itself is offloaded via `asyncio.to_thread` so a
sampling estimator's network read cannot stall the WS keepalive (no-sync-blocking
norm).

### R-C -- honest GLOBAL-FALLBACK warning

`topobathy.py` tracks `cudem_status` (`skipped` | `index_unreachable` |
`no_intersect` | `present`). The fallback-warning builder is a pure
`_compose_fallback_warnings` (both branches unit-tested). Only
`cudem_status == "no_intersect"` -- a real tile-index intersect that returned zero
-- may claim "CUDEM's hosted collection omits this coast". A caller-skip, an
unreachable index, and a datum-gated-out `present` set each name their own true
cause.

### Fetch performance (enabling native CUDEM)

Reading native CUDEM 1/9" for a real AOI transfers the AOI's pixels (CUDEM COGs
carry NO overviews, so a coarse output cannot be served from a cheap decimated
read). Three enablers keep it viable: (1) the composite reads each source CLIPPED
to the AOI window (skipping tile regions outside the AOI) + decimated to ~the
target cell (`_decimated_source_read`); (2) the `/vsicurl` env gains
`GDAL_HTTP_MULTIRANGE` + `MERGE_CONSECUTIVE_RANGES` + HTTP/2 so the many small
256x256-block range requests batch into few multiplexed transfers (without them a
native AOI read crawls block-by-block); (3) the surge/tidal fetch is offloaded via
`asyncio.to_thread` so the transfer never stalls the WS keepalive. With these the
fine Bolivar AOI (resolution_m=30, ~8 tiles) completes end-to-end in ~2 min. A
large native/coarse-CUDEM AOI still transfers a lot (the big Ike domain coarsened
to 199 m reads 21 tiles); the payload gate is the intended lever there.

## Consequences

- Surge runs with no `resolution_m` now attempt a native CUDEM composite; the
  payload gate is the safety valve (native is ~620 MB, over the 250 MB hard cap,
  so an interactive user is shown the coarsening suggestion). An explicit
  `resolution_m` (a declared coarsening) is the fast path.
- The `schism_pahm_surge` showcase seed carries an explicit screening
  `resolution_m` (a DECLARED coarsening for a fast barotropic demo) so it stays
  green + fast AND gets REAL CUDEM bathymetry coarsened to the screening grid
  (better than the old ETOPO-only blockiness); a real user's native default is
  proven by the fine-AOI re-drive + the native gate-card capture.
- Five `test_schism_pahm_surge` cases that encoded the old screening semantics
  were updated to the doctrine; a schema-compliance bug was caught in-wave (a
  `SyntheticInput.basis="native"` -- not in the Literal -- corrected to
  `"derived"`; the four-slices law analogue for tuple annotations).

## Files

- `agent/tools/fetchers/_router/hooks/topobathy.py` -- `cudem_status`,
  `_compose_fallback_warnings`, `_sample_topobathy_density`,
  `estimate_payload_mb(+_detail)`, `_analytic_payload_mb`, AOI-windowed +
  decimated `_decimated_source_read`, `/vsicurl` multirange/HTTP2 env.
- `agent/tools/payload_sampling.py` (NEW) -- the reusable sampled-estimator seam.
- `agent/workflows/schism/tidal_hydro/tidal_hydro.py` --
  `_topobathy_fetch_kwargs`, `_CUDEM_SKIP_RES_M`, `_fetch_bathymetry_cog` re-cut.
- `agent/workflows/schism/pahm_surge/pahm_surge.py` -- native-default resolution
  block, estimator + `_detail`, review entries.
- `server/src/trid3nt_server/server.py` -- `_maybe_gate_on_payload_warning`
  estimator offload + `<estimator>_detail` append.
- `server/tests/test_resolution_doctrine_0224.py` (NEW),
  `server/tests/test_schism_pahm_surge.py` (updated),
  `server/tests/test_payload_warning_flow.py` (pump helper for the estimator
  thread-hop).
- `scripts/seed_showcase_cases.py` -- the seed auto-confirm coarsens
  (`narrow_scope`) when a native default trips the hard cap.
- `scripts/sandbox/schism/render_pahm_surge_fine_proof.py` +
  `docs/proof/templates/schism_pahm_surge_fine.png` -- re-rendered over the
  real-CUDEM re-drive (run 01KZSKS296C6FHGPN3JE48W3HE), the 0221 "CUDEM omits
  this coast" caption corrected.

## Live evidence

- Fine Bolivar AOI (bbox=[-95.05,29.2,-94.6,29.65], resolution_m=30, sim_days=1.5)
  through the daemon: run 01KZSKS296C6FHGPN3JE48W3HE, status=ok, **8/930 CUDEM
  tiles read** (vs 0 under the forced skip), coastal_tin 64 nodes, elev_max 2.186
  m (peak surge 2.19 m; was 2.32 m on the ETOPO fallback). Proof re-rendered.
- Big Ike showcase (native default): the gate emitted the MEASURED estimate
  `estimated_mb=620.50 over_hard_cap=True`, the seed auto-confirmed the coarsening
  (`narrow_scope resolution_m=199`), and the re-dispatch read 21 real CUDEM tiles
  -- the native->gate->coarsen path proven end-to-end.

## Amendment (2026-08-11) -- mesh-density regression: an explicit resolution must drive the MESH, not only the fetch

NATE caught it on the re-rendered fine proof: the fine Bolivar re-drive read real
CUDEM (8 tiles) but the SURGE FIELD looked COARSER than a prior version, because the
internal TIN was built at **8x8 = 64 nodes** -- the autoscale FLOOR.

**Root cause (decoupling).** The resolution rewiring above routed `resolution_m` into
the bathymetry fetch (`fetch_res_m`) but the internal TIN was still sized ONLY from
`_autoscale_surge_domain(bbox)` -- a bbox-only rule (`_SURGE_TIN_KM_PER_NODE = 6 km`
per node, clamped `[8, 40]` per axis). For the 44x50 km Bolivar box that floors at
`round(44/6)=7 -> 8` and `round(50/6)=8`, i.e. **8x8 = 64 nodes regardless of the
requested cell**. So a fine 30 m ask bought a fine bathymetry FETCH but the same
coarse 64-node MESH -- the two were decoupled
(`pahm_surge.py` `_build_internal_tin(bbox, nx=autoscale["tin_nx"], ny=autoscale["tin_ny"])`).

**Fix (fidelity-first + declared clamps).** An explicit `resolution_m` now keys the
TIN density too, via `_resolution_driven_tin_dims(bbox, resolution_m)`: target cell =
`max(resolution_m, _SURGE_TIN_CELL_FLOOR_M=25 m)`, nodes per axis = AOI extent / cell,
bounded by a DECLARED node-budget ceiling `_SURGE_TIN_DIM_MAX_FINE = 80` per axis. The
ceiling is evidence-based, not a guess: the 0217-0219 runs solved ~440 nodes in ~30 s,
so 80x80 = 6400 nodes (~15x the node count) stays a few-minute local screening solve.
Per the declared-clamps ruling the ceiling is NEVER silent -- when it binds the
`resolution_m` envelope entry states the requested cell, the unclamped node grid, the
clamped grid, and the resulting effective mesh cell (and notes the bathymetry is still
read at the requested resolution). The NATIVE (`resolution_m=None`) path is unchanged:
mesh density keys to the AOI-driven autoscale suggestion (that path was fine).

For the fine Bolivar box the TIN goes from **64 -> 6400 nodes** (30 m ask wanted a
1454x1658 TIN; clamped to 80x80 by the budget, effective mesh cell ~545 m -- the
declared ceiling); the proof field is now visibly FINER than every prior version.

- Files: `pahm_surge.py` (`_resolution_driven_tin_dims`, `_SURGE_TIN_DIM_MAX_FINE`,
  `_SURGE_TIN_CELL_FLOOR_M`, explicit-path mesh wiring, resolution envelope note);
  `test_resolution_doctrine_0224.py` (mesh-density section: fine ask scales past the
  floor, density tracks the requested cell, ceiling self-labels, native unchanged);
  `test_schism_pahm_surge.py` (end-to-end: explicit fine ask -> dense mesh entry +
  labeled clamp).
- Re-drive: see the amendment live evidence appended below.
