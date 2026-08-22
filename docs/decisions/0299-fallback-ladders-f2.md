# ADR 0299 -- fallback ladders, wave F2: the audit migrates, and one of its rows was wrong

Status: LANDED. Date: 2026-08-21. Closes the F1 arc (ADRs 0289 / 0290 / 0291 /
0292 / 0293) by taking `docs/design/fallback-audit.md`'s 32 rows onto the
declared-ladder regime, under the full-coverage law: inventory first, a verdict
per row, a sweep guard against re-entry.

## What the inventory found

Two of this wave's load-bearing premises did not survive contact with the code.

### 1. `spec.fallback` never substituted anything

The audit registered `spec.fallback` (row 8) as "a second, undeclared
substitution mechanism", with `fetch_gridmet`'s `fallback: [fetch_era5_reanalysis]`
as "a genuine CROSS-DATASET swap (gridMET 4 km CONUS -> ERA5 ~31 km global
reanalysis) riding silently". It is not, and it never was.

`spec.fallback` is read by exactly ONE function in the repo,
`vector_fgb.resolve_endpoints`, which resolves each entry as
`spec.endpoints.get(fb)` -- a key in the spec's OWN endpoints block, never the
tool registry. `http_json` has no code path that reads it at all (its two chains
are driven by `ingest.http_source.endpoint_fallback` / `parse_fallback` and the
build hook's plans). So a sibling-TOOL entry resolved to `None`, was dropped by
the `if ep is not None` guard, and contributed nothing.

Measured, not argued -- 101 composed specs:

- 36 files carried the literal key; 27 declared `[]`.
- 9 declared a non-empty list, 11 entries total.
- 1 entry resolved to an endpoint: `fetch_nhd_waterbodies` -> `medium`
  (USGS NHDPlus HR -> the medium-resolution NHD service: SAME dataset, SAME
  producer, a resolution tier -- a `same_data` mirror the floor lets walk
  silently).
- The other 8 named sibling tools and were structurally unreachable. Six of the
  eight would not have reached `vector_fgb` at all, because `select_executor`
  routes them to `library_delegate` / `http_json` / `record` / `tiled_mosaic`
  first; the remaining two were dropped by the endpoint lookup regardless.

The harm was real, just not the harm the audit named. `registration.spec_card`
projects `list(spec.fallback)` verbatim, and `stratified.py` renders it into the
composed fetcher declaration the model reads. The model was told nine sources
have fallbacks. Eight of those promises could not be kept by any code path. A
false promise printed as catalog text is the same failure as a silent swap, told
from the other side.

### 2. The mesh's "topobathy" was a land DEM

`generate_mesh._fetch_topobathy` called `fetch_dem(source="3dep")` -- LAND ONLY --
wrote the result as `topobathy.tif`, handed it to the in-container water-edge
mesher as its depth source, and sampled it into `bed` for every node of a
COASTAL mesh. Those meshes feed `schism/tidal_hydro` and
`schism/baroclinic_circulation`, where the bed IS the physics.

Live, AOI `(-85.45, 29.90, -85.35, 30.00)` (Mexico Beach, FL):

| | old (`fetch_dem` 3dep) | new (`fetch_topobathy`) |
|---|---|---|
| CRS | EPSG:5070 | EPSG:4326 |
| min / max (m) | -0.87 / 10.62 | -10.05 / 11.34 |
| share below 0 m | 0.0247 | 0.3011 |
| **share EXACTLY 0.0 m** | **0.2946** | **0.0000** |

29.46% of that AOI was flat sea-level fill. It is the SWAN rectangle
(`docs/proof/templates/swan_bathy_forensics.png`, 12.99% exactly 0.0) in a
different seam and twice as large.

A second defect rode with it: the in-container sizer builds its depth
interpolator from the raster's own transform and queries it with lon/lat, so the
EPSG:5070 grid put every query out of bounds and the wavelength-to-depth term
silently read its `fill_value` -- while `sizing_source` claimed
"distance-to-shore + wavelength-to-depth sizing". And `dem_source` was the
constant `"topobathy (3DEP + NOAA CoNED where available)"`: CoNED was never
fetched.

## What changed

### `spec.fallback` -> `spec.endpoint_fallback`

The word now means exactly one thing: SAME-DATA mirrors of this spec's own
endpoints. The 8 dead cross-dataset lists are DELETED (ledger row); the 27 empty
declarations are removed (the field defaults empty, and an empty declaration of a
mirror chain says nothing); `fetch_nhd_waterbodies` keeps its mirror under the
new name. `spec_card` emits `endpoint_fallback`; the candidate block renders
"same-data endpoint mirrors: ...".

`_validate_hooks` REFUSES at load an entry naming no endpoint of its own spec,
naming the ladder as the home for a cross-dataset alternative. The
dead-declaration class is now unrepresentable rather than merely absent.

### The gridmet -> ERA5 rung is NOT built (parked, see below)

The kickoff's headline migration assumed a silent swap to convert. There is
none. Building the ladder would CREATE a cross-dataset degradation the system
does not have today -- a 4 km CONUS station-blend answering as a ~31 km global
reanalysis. Deleting the false promise is the migration that the finding
actually calls for; adding the capability is a separate decision and it is
NATE's. gridMET's docstring already routes the model correctly ("Non-CONUS
bboxes -- use `fetch_era5_reanalysis`"), which is where a cross-dataset
alternative belongs: as advice to a model that can call the other tool itself
and label it, not as a swap under one tool's name.

### The coastal mesh bed

`_fetch_coastal_bed` routes `fetch_topobathy(target_crs="EPSG:4326",
fallback=("etopo_bathy_base",))`. The mesh IS the wet domain, so every node needs
a real below-waterline bed; where CUDEM stops mid-AOI the global ETOPO relief is
a REAL bed, coarse and loudly labeled, and a REFUSAL is the honest outcome when
even that cannot serve. `target_crs` is 4326 deliberately -- it is the one CRS
both consumers agree on (see above). `dem_source` is now derived from the
activation rows, `bed_fallback_note` rides `LayerURI.fallback_note`, and the
mesh carries a `SyntheticInput(param="mesh_bed", consequence="physics")` naming
what its elevations came from.

REJECTED alternative: rename and refuse for coastal AOIs. `_fetch_coastal_bed`
is reached only from `_build_coastal`, so every call is a coastal AOI -- refusing
would delete the coastal mesh mode rather than fix it, and a real bed is
available.

### `regional_fine` becomes a declared rung (ADR 0292's parked schema gap)

New consequence class `enhancement`: a source BETTER than the primary (finer,
more local) that a capability may lay under part of a request. It is:

- declared, so `_reconcile_to_paint` names it instead of logging an unknown key;
- NOT in `DEGRADATION_CLASSES`, so the loudness floor ignores it and
  `Activation.degraded` stays False;
- NOT in `Ladder.alternatives`, so no call site can permit it through
  `fallback=` -- permitting a rung is how a caller accepts a COST, and this one
  has none;
- NOT marked GATE-UNSEEN when the capability lays it down, because that text
  means "an unapproved substitution" and a free upgrade is not one.

`Ladder` validates rungs below the primary against `BELOW_PRIMARY_CLASSES`
(degradations plus `enhancement`). The walker's module docstring is corrected:
only degradation rungs are gated at all.

`include_regional_fine` comes OFF `coverage_exempt_params`. The premise recorded
there -- "no rung's share is measured" -- has been false for that param since
0292 made every leg's share a measured footprint fraction, and with a rung to
stamp against the rows are evidence rather than arithmetic.

### `dem_uri` is a declared param

The `user_supplied` TOP rung named a param the model could not see: it was
absorbed by `**_extra_ignored` for the whole F1 arc. It is now in
`fetch_topobathy`'s `params` (hence its `inputSchema`) and its docstring. Cache
behaviour is unchanged -- `_canonicalize_params` omits `None`.

### The ad-hoc policy params (kickoff item 2e)

Nothing dies. Per-param reasoning:

| param | verdict | reason |
|---|---|---|
| `force_bathy_base` | KEEP, demoted | Also the `etopo_bathy_base` rung's own injected param, so it cannot be deleted without redesigning the rung's invocation form (machinery frozen). As a USER-facing hatch it now reads second in the refusal text, which states its cost: it turns the coverage question off, so the result carries the warning WITHOUT per-rung numbers. |
| `skip_cudem` | KEEP | Distinct purpose: a COST lever in the resolution doctrine (at cells >= 500 m, reading dozens of CUDEM COGs buys no fidelity). Not a fallback policy; no rung covers it. |
| `skip_land` | KEEP | Disables the 3DEP land leg. Not a fallback policy; changes what a gap COSTS, which the refusal text already reflects. |
| `include_regional_fine` | KEEP | Now the switch for the declared `regional_fine` rung. The rung exists so the walker can NAME the source; the param is still how it is turned on. |

### Sweep guard

`tests/test_fallback_sweep_guard.py`, structural rather than grep-for-the-word:

1. no `endpoint_fallback` entry names anything but an endpoint of its own spec;
   none names a registered tool; `_validate_hooks` refuses one at load; the card
   key and the candidate-block wording say what the mechanism is;
2. the coastal mesh bed comes from a topobathy source with a declared rung, and
   never from `fetch_dem`; its provenance derives from activation rows;
3. every registered ladder is well-formed, and every source
   `topobathy._rung_coverage` can report is a declared rung;
4. every `fetch_topobathy` call site in `trid3nt_server/` passes `fallback=` or
   is allow-listed with a reason (the allow-list is empty);
5. a REGISTER of the parked SILENT sites (rows 12, 18, 20) keyed by marker: a
   failure means the site was fixed (delete the row, cite the ADR) or moved
   (update the marker), never that it drifted out of view.

## The verdict table (all 32 rows + the F1-arc discoveries)

Verdicts: MIGRATED (now on a declared rung) / CONVERTED (refuses) / HONEST
(keep, reason) / DEAD (deleted) / PARKED (fork for NATE) / OUT-OF-SCOPE
(behaviour fallback, not an alternative-to-data -- the ladder design excludes
these by construction).

| # | site | verified state | verdict |
|---|---|---|---|
| 1 | `dem_3dep.py` `DemAutoFallbackGateError` | unchanged; typed retryable gate names the substitute + tradeoff | HONEST (gold; the pattern the ladder generalised) |
| 2 | `dem_3dep.coarsen_dem` | unchanged; coarsen stamp in the layer name + `requested_res_m` | HONEST (labeled degrade) |
| 3 | `http_json._fetch_endpoint_fallback` | unchanged; hook-plan mirror chain, all-fail -> typed retryable | HONEST (same-data mirrors; floor says silent) |
| 4 | `http_json._execute_parse_fallback` | unchanged; NWIS IV -> Site RDB, all-empty -> typed EMPTY | HONEST (same measurement, different endpoint) |
| 5 | topobathy coverage / SWAN exhibit | WIRED F1-F1e; extended here (`regional_fine` rung, `dem_uri` visible) | MIGRATED (this wave completes it) |
| 6 | `raster_cog.py` PC href sign | unchanged | OUT-OF-SCOPE (transport) |
| 7 | `geocode_location` secondary source | unchanged; typed + loud | HONEST |
| 8 | `spec.fallback` | 9 non-empty lists, 8 structurally unreachable, 1 same-data mirror | DEAD (8 deleted) + HONEST (1, renamed `endpoint_fallback`) |
| 9 | `pipeline_emitter.py:755` s3fs anonymous | the cited line is a COMMENT; the file has no s3fs call, only boto3. The row described a mechanism that did not exist at audit time | DEAD (audit row deleted; no code change) |
| 10 | `quantity_styles.resolve_style_preset` | moved to :115; unchanged behaviour, warning + auditable counter | HONEST (honest neutral, never a wrong physical map) |
| 11 | `_raster_postprocess/cog.py:128` CRS -> 3857 | unchanged, live via `sfincs_reader` -> the sfincs entrypoint; logged-only. An UNAUDITED duplicate exists at `workflows/shared/cog_io.py:90` | PARKED (fork: raise vs guess; two sites, one of them a worker) |
| 12 | SFINCS wide active-mask | `_mask_note` is BUILT and reaches only `logger.warning` -- envelope wiring never landed. Two independent copies (`_sfincs_build/deck.py`, `workflows/sfincs/sfincs_builder.py`) | PARKED (registered in the sweep guard; the fix is composer/envelope wiring, not a rung -- there is no alternative SOURCE to declare) |
| 13 | `deck.py` `bbox-area-fallback` | unchanged; `source=` stamped in the estimate reason | HONEST (sizing estimate, never the solved mask) |
| 14 | `deck_quadtree.py` `center_band_fallback` | unchanged; `refine_source` stamped in the deck, but repo-wide grep shows NOTHING downstream reads it back | PARKED (physics-loudness class) |
| 15 | `deck.py` netamt rainfall | moved to :2022; deck-visible comment + `ForcingSummary.source`; a hard `FORCING_OUT_OF_RANGE` gate prevents silent invocation on absence | HONEST |
| 16 | `swmm_network.py` synthetic diameters / topology / sub-area | sub-area IS labeled `SyntheticInput`; diameter defaulting and `n_topology_snapped` are NOT -- only a free-text `label_suffix` | PARKED (the audit's "loud" grade is half wrong; corrected in the audit doc) |
| 17 | `raster_cell_mesh.py` roughness / imperviousness | WORSE than audited: the log fires only when the user OVERRIDES physics, never on the default path. No field, no note | PARKED (now graded SILENT, not logged-only) |
| 18 | `raster_cell_mesh.py` outfall relocation | confirmed still SILENT: no log, no note, `BuildResult.outfall_cell` does not say which path produced it | PARKED (registered in the sweep guard) |
| 19 | `gwt_adapter.py` SFR streambed gradient | the "primary" (`river_rbot_by_cell`) is populated by NO live caller and is not on `MODFLOWRunArgs`. The demo gradient is UNCONDITIONAL, and the audited `SyntheticInput` label does not exist on this path | PARKED (a law-9 fork: a dead primary hiding an unconditional demo constant) |
| 20 | `_landlab_postprocess` `src_crs = dst_crs` | confirmed still SILENT, still line 134, live on every landlab job | PARKED (registered in the sweep guard; the honest fix is RAISE, a worker change needing its own image smoke) |
| 21 | `landlab/run_chain.py` result.json write fail | unchanged; the composer's recompute is documented as an honest recompute from the primary COG contract | HONEST |
| 22 | outputs.json seam -> register-only -> on-box | unchanged; three-tier fork, typed `*_NO_LAYERS` gate on zero layers | HONEST (byte-equivalent per 0280/0281) |
| 23 | river-dye `bank_source` gate | mechanism at `:1872` (raise) / `:2601, :3350` (call sites), not the cited docstring line; behaviour gold | HONEST (gold) |
| 24 | river-dye NHDArea -> constant width | NO LONGER a silent degrade: the caller raises `BanksUnavailableError`, surfacing the SAME retryable `TELEMAC_BANKS_UNAVAILABLE` gate as row 23. Only the stale log text still says "constant-width fallback" | HONEST (merged into row 23; log text corrected in the audit doc) |
| 25 | river-dye water-polygon domain -> ribbon | moved to :1216; still `LOG.warning` only; `domain_mode` stamped internally, not LLM-facing | PARKED (physics-loudness class) |
| 26 | river-dye 3DEP DEM rung | moved to :1576-1696; STAC x3 -> 3DEP -> honest typed error | HONEST (the data-source norm, correctly applied) |
| 27 | HEC-RAS demo geometry | SPLIT. `riverine_flood` / `levee_breach` gained a real gate (`run_demo_geometry` opt-in + typed `HECRAS_DEMO_GEOMETRY_REQUIRED` + input review). `flood_2d` / `culvert_embankment_flow` never baked Muncie at all -- their `_FIDELITY_NOTE` is about solver maturity | HONEST (2 rows) + DEAD (2 rows never matched the description) |
| 28 | `input_review` headless fail-open | SAFER than audited: `physics_refusal_reason()` now refuses a headless physics demo-default instead of proceeding | HONEST |
| 29 | `tool_gating` cold-index fail-open | unchanged (the empty-`ranked` early return is silent, narrower than "logged-only") | OUT-OF-SCOPE (routing, not physics) |
| 30 | region-choice headless fail-open | MOVED to `gates/confirm.py:993` (headless) / `:945` (timeout); behaviour unchanged | OUT-OF-SCOPE (citation corrected) |
| 31 | `actionability` classify fault | unchanged; no log at all in the except | OUT-OF-SCOPE (message selection) |
| 32 | `uri_registry` unknown-URI pass-through | CHANGED: an unregistered object-store URI now RAISES `URI_HANDLE_UNRESOLVED`; only non-object-store strings pass | HONEST (stricter than audited) |
| F1-a | `spec.fallback` sibling-retry (row 8) | see above | DEAD / HONEST |
| F1-b | mesh `_fetch_topobathy` misnomer | land-only DEM as a coastal mesh bed | MIGRATED |
| F1-c | `dem_uri` schema invisibility | absorbed by `**_extra_ignored` | MIGRATED |
| F1-d | `regional_fine` has no rung (0292 park) | ladder schema had no non-degrading slot | MIGRATED |
| F1-e | `force_bathy_base` / `skip_cudem` exemption (0291-0293 notes) | see 2e table | HONEST (2) + PARKED (1 fork) |
| F1-f | interior nodata inside a painted extent | carried from 0290/0291, unchanged | PARKED (documented open edge) |

## Parked forks for NATE

1. **Does `fetch_gridmet` want a real ERA5 rung?** Today nothing degrades.
   Recommendation: NO. gridMET is a 4 km CONUS station-blend; ERA5 is a ~31 km
   global reanalysis -- a different product, not a coarser view of the same one,
   and the docstring already tells the model to call ERA5 itself for a non-CONUS
   AOI, where it gets ERA5's own name, caveats and layer. The same question
   applies to the seven other deleted pairs (era5 -> mrms/hrrr, aorc ->
   mrms/era5, gtsm <-> coops, nwis -> nwm, lter -> nwis, esri_landcover ->
   landcover). If any of them SHOULD degrade, each needs its own rung and its
   own consequence argument.
2. **`force_bathy_base` / `skip_cudem` still exempt the coverage stamp.** Their
   recorded premise ("no rung's share is measured") is as stale as
   `include_regional_fine`'s was: 0292 measures every leg. Dropping their
   exemption too would give the escape hatches measured rows -- but it changes
   0292's pinned evidence C and its tests, which is wider than this wave's
   pre-approved slot.
3. **Row 19, `DEFAULT_SFR_STREAMBED_GRADIENT`.** The DEM-rbot primary has no
   live caller and no contract field, so a demo constant drives Manning flow on
   every `stream_depletion` run, unlabeled. This is a law-9 question (refuse vs
   label), not a ladder question.
4. **Row 11, the COG CRS guess**, at two sites (`_raster_postprocess/cog.py` and
   the unaudited `workflows/shared/cog_io.py`). Raise or keep guessing?
5. **The physics-loudness wave** (rows 12, 14, 16, 17, 18, 20, 25). These are
   not ladders: no alternative SOURCE exists to declare, only a default constant
   or an assumed value. They are the audit's Option 1 (universalise
   `fallback_note` + a lint), which NATE has not green-lit. Registered in the
   sweep guard so the set cannot grow quietly.

## Evidence

Live, this box, MinIO + local docker.

**gridMET A/B** (`variable=pr`, `2023-08-01..02`):

- A, AOI Paris `(2.2, 48.8, 2.4, 49.0)`, no `fallback=`:
  `GRIDMET_INPUT_ERROR: bbox ... does not intersect CONUS`. No substitution.
- B, same AOI, `fallback=("fetch_era5_reanalysis",)`: byte-identical refusal.
  `get_ladder("fetch_gridmet")` is `None`; the only registered ladder is
  `fetch_topobathy`.
- CONUS control, Story County IA: SERVES,
  `s3://trid3nt-cache/cache/static-30d/gridmet/cff88da2...tif`, `fallbacks=[]`,
  `fallback_note=None`.
- The card now reads `endpoint_fallback: []`; the docstring's ERA5 mentions are
  routing advice and stay.

**Sonoma restamp**, AOI `(-123.50, 38.735, -123.47, 38.765)`,
`include_regional_fine=True`:

- rows `cudem_nearshore / primary / 0.5` + `regional_fine / enhancement / 0.5`;
- `rung_coverage={'cudem_nearshore': 0.5, 'etopo_bathy_base': 0.0,
  'regional_fine': 0.5}`;
- walker WARNING output captured verbatim: `''` -- no unknown-key line, no
  share-sum line;
- `fallback_note=None` (no UNMEASURED note): before this wave the same call
  stamped `fallbacks=[]`, `rung_coverage=None` and the unverified note.

**Exhibit AOI regression** `(-85.55, 29.70, -85.40, 29.85)` -- unchanged from
0292: A `TOPOBATHY_COVERAGE_GAP` 89%, retryable=False; B rows
`cudem_nearshore 0.888889` + `etopo_bathy_base 0.111111`; C (`force_bathy_base`)
`fallbacks=[]`, `rung_coverage=None`, the UNMEASURED note.

**Coastal mesh**, AOI `(-85.45, 29.90, -85.35, 30.00)`, built end to end through
`trid3nt-local/mesh:latest`:

- 740 nodes, 1247 cells, 67.33 km2, UTM 32616;
- bed min **-9.568 m**, max 7.283 m;
- mesh nodes below 0 m: **26.62%**; nodes at EXACTLY 0.0: **0.0000**;
- `dem_source: "topobathy: cudem_nearshore 100%"`, `bed_fallback_note: None`;
- the bed raster comparison in "What the inventory found" above is the same AOI.

**Gates.** Four slices at baseline: `[a-e]` 1475 passed / 5 skipped; `[f-o]`
6628 passed, the 4 `fetch_resolution` failures; `[p-r]` 2102 passed, the 2
`river_dye` failures; `[s-z]` 1405 passed / 6 skipped. contracts 721.
`ws_smoke.py` `all_passed=True`. Flood canary `status=ok`, depth COG
`s3://trid3nt-runs/01M0KK9GWJAGD0EC9A4BZ4YSB1/overviews/01M0KKASN0VTH72BJ26X28PQ6H.tif`.

No `workers/` path touched, so no image rebuild.

## Consequences

- `SourceSpec.fallback` is gone; the field is `endpoint_fallback` and a spec
  declaring an entry that names no endpoint of its own fails at registration.
- The spec card key changed from `fallback` to `endpoint_fallback`.
- `FallbackConsequence` / `Consequence` gained `enhancement`; a consumer
  switching exhaustively on the literal must handle it.
- `Ladder` validates rungs below the primary against `BELOW_PRIMARY_CLASSES`.
- `fetch_topobathy` gained a `dem_uri` param in its inputSchema. Cache keys are
  unchanged (`None` params are omitted).
- `include_regional_fine` no longer suppresses the coverage stamp: that request
  now returns rows and a `rung_coverage` map.
- `generate_mesh` coastal builds fetch `fetch_topobathy`, not `fetch_dem`. A
  coastal AOI where neither CUDEM nor ETOPO can lay a bed now REFUSES instead of
  meshing flat water.
