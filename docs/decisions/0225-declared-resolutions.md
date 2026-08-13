# ADR 0225 - Declared resolutions: tools declare their valid ranges, out-of-range asks are quoted back

Status: accepted

Date: 2026-08-11

Cross-links: ADR 0224 (the native-default + sampled-estimator doctrine this builds on),
ADR 0223 (the labeled-clamp batch whose hecras/river_dye sites this upgrades), ADR 0219
(the R2 resolution-is-a-user-lever ruling), the granularity gate (#154).

## Context

NATE's clamp ruling (recorded in the granularity norm): **silent coercion to an
undeclared resolution is BANNED.** A tool must DECLARE the resolutions it can actually
run - as metadata, in its docstring, and on its gate card - so the user picks from
REALITY. An out-of-range ask gets the declared range QUOTED BACK (typed / gated),
never a silent snap.

ADR 0223 (audit finding #6) made the hecras `[20, 200]` m clamp VISIBLE by labeling
it, but it still SILENTLY snapped a 5 m ask up to 20 m. Under the full ruling a snap
to an undeclared value is exactly what is banned - the label was a half-measure. This
ADR carries the declaration through the whole surface and upgrades the snap to a
quote-back.

Two-layer truth (established architecture): DATA-native facts live with the FETCHER
(a source's native cell); SOLVER constraints live with the TEMPLATE (a mesh
generator's edge window, a node-budget ceiling, an output-raster cap). The gate card
COMPOSES both layers.

## Decision

### The declaration schema - `ResolutionSpec` (contracts)

A small shared pydantic model in `trid3nt_contracts.tool_registry`:

```
ResolutionSpec{ param, unit, min_value?, max_value?, native_hint?, options?, step?,
                constraint_source: 'solver'|'data', rationale }
```

- A validator enforces that a spec declares a REAL constraint: a continuous
  `min/max` window XOR a discrete `options` set; unbounded (both `None`, no options)
  is legitimate ONLY when the `rationale` says "unbounded" (so an empty spec cannot
  masquerade as a forgotten one); `min <= max`.
- Helpers ride the model so the declaration is read ONCE from three places:
  `contains()` (the in-range check), `range_phrase()` (`"20-200 m"` / `">=10 m"`),
  `docstring_line()` (the consistent per-tool docstring line), and `quote_back()`
  (the two-layer card: `"5 m requested; this tool supports 20-200 m (mesh/solver);
  data native 3DEP 10 m; pick a resolution_m in range."`).
- `AtomicToolMetadata` gains `resolution_specs: tuple[ResolutionSpec, ...]` (default
  `()`, zero impact on the ~200 non-granularity tools) + a `resolution_spec_for(param)`
  lookup. `SourceSpec` gains `resolution_declarations` so a fetcher's DATA-native
  facts ride from the `source.yaml` onto the synthesized metadata.

### The out-of-range behaviour - `resolution_declared.py` (server)

`enforce_resolution(spec, requested)` raises `ResolutionOutOfRangeError` (carrying a
wire-typed `ToolInputError(code="INVALID_ARG")` whose message IS the quote-back card)
when `requested` is out of the declared range. `None` (the native / autoscaled
default) is always in-range. A template lets it propagate or re-wraps it as its own
typed error carrying `str(exc)` (the message is already the full quote-back).
`resolution_review_note()` labels the WITHIN-range derivations that remain legitimate
(an in-range value the AOI autoscale coarsens for tractability) - those are labeled
degrades, not silent snaps; only the out-of-DECLARED-range case is the hard error.

### Adopted surfaces

| Tool | Param | Declared range | Owner | Out-of-range behaviour |
|---|---|---|---|---|
| `hecras_flood_2d` | `resolution_m` | 20-200 m | solver | ENFORCED - quote-back typed error (was ADR 0223's labeled snap) |
| `schism_pahm_surge` | `resolution_m` | 25-1000 m | data | ENFORCED - quote-back; in-range fine ask further bounded by the self-labeling 80x80 node budget |
| `sfincs_flood` | `quadtree_base_resolution_m` | >=10 m | solver | ENFORCED - sub-floor quoted back |
| `generate_mesh` | `min_edge_length_m` / `max_edge_length_m` | >=5 m | solver | DECLARED - realizability is the AOI-dependent <=8-side build check (a typed GenerateMeshError) |
| `landlab_*` (13) | `target_resolution_m` | >=10 m | data | DECLARED - 3DEP-native floor; DEM resampled to grid |
| `telemac_river_dye` / `telemac_do_sag` | `mesh_resolution_m` | >=3 m | solver | DECLARED - MESH_H_FLOOR_M; node-budget coarsening self-labels |
| `fetch_dem` | `resolution_m` | >=1 m | data | DECLARED - 3DEP 1-10 m / Copernicus 30 m (card quotes native) |
| `fetch_topobathy` | `resolution_m` | >=1 m | data | DECLARED - CUDEM 1/9" ~3 m / ETOPO ~450 m (card quotes native) |
| `postprocess_schism` | `max_px_per_side` | 128-2500 px | solver | OUTPUT-artifact cap, now OVERRIDABLE (was a hard silent cap) |

ENFORCED = `enforce_resolution` wired (a numeric range already governed the input, so
the quote-back replaces the snap). DECLARED = the range rides the metadata + docstring
+ gate card (the two-layer card can quote it); where a hard clamp did not already
exist, no new one is invented - realizability stays the existing typed build error
(mesh 8-side check, node budget), and the declaration documents reality. Unbounded
sides (no coarse ceiling) are declared as such with a rationale, per the ruling.

Scope boundary: `ResolutionSpec` covers RESOLUTION/granularity params only.
`river_dye`'s ADR 0223 `_clamp_domain_extent` (reach length / channel width / sim
duration) is DOMAIN-EXTENT, not resolution, and keeps its labeled-guardrail behaviour.

### The self-enforcing sweep (item 5)

`test_resolution_declared_0225.py` sweeps `TOOL_REGISTRY`: every tool with a NUMERIC
resolution-class param (name-token + float/int annotation; a `str` mode selector like
telemac `mesh_resolution` is excluded) must carry a `ResolutionSpec` OR be on the
explicit `_PENDING_DECLARATION` allowlist. A FUTURE tool that ships a resolution param
with neither FAILS the test - the ruling is self-enforcing. A companion test forbids
STALE pending entries (the list may only shrink). 21 tools declared; 7 params pending
(`compute_building_density`, `compute_home_range_kde`, `fetch_landcover`,
`fetch_population`, `pelicun_damage_assessment`, `swmm_urban_flood`,
`swmm_dual_drainage_coupling`).

## Consequences

- A finer-than-supported ask no longer silently degrades: the user is told the range,
  the constraint owner, and the data-native cell in one card, then must pick a real
  value. The two-layer composition (solver range + data native + measured MB) is
  proven on the surge payload card (ADR 0224) and the hecras quote-back.
- The declaration is the single source: docstring, gate card, and the sweep test all
  read the same `ResolutionSpec`. A tool cannot claim one range in its docstring and
  enforce another.
- New shared seams: `ResolutionSpec` (contracts), `resolution_declared.py` (server),
  `SourceSpec.resolution_declarations` (fetcher-side). The reference for future
  granularity params.

## Files

- `contracts/src/trid3nt_contracts/tool_registry.py` - `ResolutionSpec`,
  `ResolutionConstraintSource`, `AtomicToolMetadata.resolution_specs` +
  `resolution_spec_for`.
- `contracts/src/trid3nt_contracts/source_spec.py` - `SourceSpec.resolution_declarations`.
- `server/src/trid3nt_server/agent/tools/resolution_declared.py` (NEW) -
  `enforce_resolution`, `ResolutionOutOfRangeError`, `resolution_review_note`.
- `.../fetchers/_router/router.py` + `registration.py` - synthesize `resolution_specs`
  from the spec.
- `.../fetchers/terrain/fetch_dem/source.yaml`,
  `.../fetchers/ocean/fetch_topobathy/source.yaml` - `resolution_declarations`.
- `.../workflows/hecras/flood_2d/flood_2d.py` - `_RES_SPEC`, `_resolution_with_basis`
  upgraded to enforce, docstring.
- `.../workflows/schism/pahm_surge/pahm_surge.py` - `_SURGE_RES_SPEC`, enforce, docstring.
- `.../workflows/sfincs/flood/flood.py` - `_SFINCS_QUADTREE_RES_SPEC`, enforce.
- `.../workflows/mesh/generate_mesh/generate_mesh.py` - `_MIN_EDGE_SPEC` / `_MAX_EDGE_SPEC`.
- `.../workflows/landlab/run_landlab.py` (`LANDLAB_RES_SPEC`) + `_composer_common.py`
  re-export + 13 template metadata.
- `.../workflows/telemac/river_dye/river_dye.py`, `.../telemac/do_sag/do_sag.py` - specs.
- `.../workflows/schism/postprocess_schism.py` - `OUTPUT_RASTER_CAP_SPEC` +
  `max_px_per_side` override threaded through `_rasterize_nodes` / `_adaptive_grid`.
- `server/tests/test_resolution_declared_0225.py` (NEW) - schema + enforcement + the
  self-enforcing sweep; `test_hecras_flood2d_template.py` (updated: clamp -> quote-back).

## Live evidence

Foreground out-of-range drive (direct tool call, live geocode):

```
hecras_flood_2d(location="Wabash River near Lafayette, Indiana", resolution_m=5)
-> status=error  error_code=HECRAS_INPUT_INVALID
   "5 m requested; this tool supports 20-200 m (mesh/solver); data native
    3DEP 10 m (fetch_dem); pick a resolution_m in range."
```

In-range `resolution_m=60` on the same reach resolves basis `user`, no note (no snap).
