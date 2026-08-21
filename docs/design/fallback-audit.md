# Fallback audit -- the hidden-polymorphism inventory

Purpose: surface every path where TRID3NT, on failure or absence of a primary,
does something ELSE and continues -- and grade whether that swap is declared and
loud enough for the harm it can do. The charge (NATE): "the fallback degrades the
sim and makes it useless... I feel uncomfortable with the hidden polymorphism."
The exemplar is ADR 0281 SWAN: an agent-side postprocess fallback silently
covered a never-shipped in-image postprocess for the image's whole life -- the
fallback's SUCCESS hid the primary's death.

## Method + coverage

- Swept `trid3nt_server/` + `workers/` for the lead terms
  (`fallback|fail-open|synthetic|degrade|best-effort|except...continue/pass` near
  solver/fetch, register-vs-on-box forks, source ladders, default-when-missing
  branches). 330 files carry at least one lead token; most are comments. I read
  AROUND every physics- or data-consequential hit and every register/on-box fork.
- The bar applied is the existing norm set: the data-fallback ruling (same-data
  mirror = silent OK; CROSS-DATASET = loud + user-gated), the honesty floor (a
  degraded sim never reads `status=ok` without saying so), and the consequence
  razor (physics-consequential = user-facing).
- The DEAD-PRIMARY method is the SWAN method: is the primary's code actually in
  the image/venv it claims to run in? Checked statically against every Dockerfile
  and the solver dispatch model (`exec` vs `docker`).
- Operational/cosmetic fallbacks that cannot change an answer are grouped rather
  than enumerated one-per-line; every physics- or data-degrading path gets its
  own row.

## The table

Axes: LOUDNESS = user-gated | loud (narrated in the envelope) | logged-only |
SILENT. SEVERITY = physics (the sim's answer changes/becomes fake) | data (real
but worse inputs, incl. map integrity) | operational (retry/transport, answer
unaffected) | cosmetic.

### A. Data router / fetch ladders

| # | WHERE | TRIGGER | WHAT DEGRADES | LOUDNESS / SEVERITY | VERDICT |
|---|---|---|---|---|---|
| 1 | `data/fetchers/_router/hooks/dem_3dep.py:107` `DemAutoFallbackGateError` | 3DEP service failure/timeout on `source="auto"` | 3DEP (1-10 m LIDAR) -> Copernicus GLO-30 (30 m RADAR): different method, coarser | user-gated / data | OK-as-is (GOLD -- typed retryable gate names the substitute + tradeoff, user approves) |
| 2 | `data/fetchers/_router/hooks/dem_3dep.py` `coarsen_dem` | requested resolution finer than the pixel budget | delivered DEM coarser than asked | loud (coarsen stamp in layer name + `requested_res_m`) / data | OK-as-is (labeled degrade) |
| 3 | `data/fetchers/_router/executors/http_json.py:70` `_fetch_endpoint_fallback` | a mirror returns 5xx/429/timeout | next mirror of the SAME dataset tried | logged-only / operational | OK-as-is (same-data mirror = silent-OK norm; all-fail -> honest typed error) |
| 4 | `http_json.py:211` `_execute_parse_fallback` | USGS NWIS IV body empty/404 | IV WaterML-JSON -> Site-service RDB (same gauge, different endpoint) | logged-only / operational | OK-as-is (same measurement; all-empty -> honest EMPTY error, never a header-only FGB) |
| 5 | `data/fetchers/_router/hooks/topobathy.py` `_compose_fallback_warnings` + `_assert_nearshore_coverage` | a topobathy source leg missing/short | mosaic assembled from remaining legs | user-gated-capable (declared ladder + coverage on the envelope) / data | **WIRED** (ADR 0289 -- see below) |
| 6 | `data/fetchers/_router/executors/raster_cog.py:1725` PC href sign | per-href sign endpoint fails | token-path signing | logged-only / operational | OK-as-is |
| 7 | `data/fetchers/socioeconomic/geocode_location/...:792` | primary geocoder miss | secondary geocode source | loud (typed) / operational | OK-as-is |
| 8 | `_router/registration.py:406` `spec.fallback` -> `executors/vector_fgb.py:537` + the `http_json` chain | the primary endpoint fails | the spec's NEXT named endpoint OR, for some specs, a SIBLING TOOL | logged-only / **mixed** | **F2 MIGRATION** (see below) |

Row 5 -- the SWAN bathymetry exhibit, WIRED (wave F1, ADR 0289). The row's silent
half was PARTIAL CUDEM coverage: `_compose_fallback_warnings` fires only when the
1/9" composite is ENTIRELY absent, so an AOI CUDEM merely runs short of got no
warning at all, and the 3DEP land leg's flat ~0 m ocean fill painted the
uncovered water -- the fake-land rectangle in
`docs/proof/templates/swan_bathy_forensics.png` (12.7% of the SWAN grid exactly
0.0). `fetch_topobathy` now declares a ladder
(`user_supplied -> cudem_nearshore -> etopo_bathy_base -> REFUSE`) walked by the
shared walker: undeclared, a coverage gap is a typed `TOPOBATHY_COVERAGE_GAP`
naming where CUDEM ends; declared, the ETOPO rung fills it loudly with the
coverage split on the envelope (live A/B: 88.9% / 11.1%; exactly-0.0 cells
12.99% -> 0.00%). The ZERO-CUDEM path (this row's original subject) keeps its
loud warning unchanged and joins the ladder in wave F2.

Wave F1b (ADR 0290) closed the panel's findings on this row: all four exposed
`fetch_topobathy` callers now declare the rung (SFINCS coastal, GeoClaw
non-tsunami and SCHISM tidal_hydro joined SWAN -- the first turned the gap into a
failed envelope, the other two silently degraded to the LAND-ONLY `fetch_dem`);
the merge reconciles its footprint PROMISE against the tiles that actually
painted; and an exempted request (`force_bathy_base` / `skip_cudem` /
`include_regional_fine`) stamps NO coverage claim, carrying a `PARTIAL-CUDEM
BATHYMETRY` warning instead.

Wave F1c (ADR 0291) closed what a SECOND panel found still open on this row. The
merge read only CUDEM's paint flags, so ETOPO's were discarded; and on TOTAL
CUDEM LOSS the auto-engaged ETOPO base was clobbered by the un-masked 3DEP land
leg, returning a land-fill ocean as SUCCESS with a `cudem_nearshore / 1.0` row.
Now: every leg's paint flag is consumed, the land leg is masked whenever an
ETOPO base is present, a painted-short AOI with no ETOPO paint REFUSES even
under an exempting param, and activation rows carry the MEASURED share
(`rung_coverage`) rather than `1.0 - <promise>`. The exempted serve is visible
(an UNMEASURED note on `fallback_note`, plus the hoisted `fallback_warning`)
without ever being numeric.

Row 8 -- `spec.fallback`, a PRE-EXISTING NAME COLLISION, registered here and NOT
touched by F1/F1b. It is a second, undeclared substitution mechanism that predates
the ladders and shares their word: 32 source specs carry a top-level
`fallback: [...]` list, resolved into an endpoint CHAIN by
`vector_fgb._endpoint_chain` (and the equivalent `http_json` chain) and surfaced
verbatim on the spec card by `registration.py:406`. Its consequence class is MIXED
and undeclared: in `vector_fgb` the entries name alternate ENDPOINTS of the SAME
dataset (a `same_data` rung, silent-OK by the floor), but `fetch_gridmet`'s
`fallback: [fetch_era5_reanalysis]` names a SIBLING TOOL -- a genuine
CROSS-DATASET swap (gridMET 4 km CONUS -> ERA5 ~31 km global reanalysis) riding
silently with no rung, no gate and no activation row. F2 must inventory all 32,
classify each entry, and migrate the cross-dataset ones onto declared rungs; the
same-data ones can either become `same_data` rungs or keep the endpoint chain
under a renamed key so the word `fallback` means exactly one thing.

### B. Cache / transport

| # | WHERE | TRIGGER | WHAT DEGRADES | LOUDNESS / SEVERITY | VERDICT |
|---|---|---|---|---|---|
| 8 | `data/cache.py:343-429` read-through provenance sidecar + s3 read/write | any storage fault | returns uncached (re-fetch) | logged-only / operational | OK-as-is |
| 9 | `emission/pipeline_emitter.py:755` s3fs anonymous | no creds resolved | anonymous S3 read | logged-only / operational | OK-as-is (documented boto3-not-s3fs lesson) |

### C. Emission / styling

| # | WHERE | TRIGGER | WHAT DEGRADES | LOUDNESS / SEVERITY | VERDICT |
|---|---|---|---|---|---|
| 10 | `emission/quantity_styles.py:90` `resolve_style_preset` -> `NEUTRAL_FALLBACK_PRESET` | quantity has no registered colormap | neutral ramp instead of a physical colormap | logged-only + process counter / cosmetic | OK-as-is (honest neutral, never a silently-WRONG physical map; counter is auditable) |
| 11 | `workers/_raster_postprocess/cog.py:128` CRS -> `EPSG:3857` | no `crs` variable resolvable from the netCDF | COG CRS tag may not match pixel coords (misplacement) | logged-only / data | NEEDS-LOUDER + DEAD-PRIMARY-adjacent (see finding D-2) |

### D. Solve-time deck authors + mesh (physics-consequential)

| # | WHERE | TRIGGER | WHAT DEGRADES | LOUDNESS / SEVERITY | VERDICT |
|---|---|---|---|---|---|
| 12 | `workers/_sfincs_build/deck.py:852-930` wide active-mask bounds; surfaced only at `workflows/sfincs/flood/flood.py:1748-1754` | DEM elevation range unreadable | active-cell mask widened -- domain includes cells a real DEM range would EXCLUDE; flooded-area answer changes | **SILENT to user (logger.warning only)** / **physics** | **NEEDS-LOUDER** (SILENT+physics -- see verbatim below) |
| 13 | `workers/_sfincs_build/deck.py:1324` `bbox-area-fallback` | DEM unreadable for the autoscaler | assumes whole bbox active for the SIZE estimate only | loud (`source=` stamped in estimate string) / operational | OK-as-is (sizing estimate, not the solved mask) |
| 14 | `workers/_sfincs_build/deck_quadtree.py:384` `center_band_fallback` | no z=0 land-sea interface resolved in AOI | refinement follows a fixed cross-shore center band instead of the coastline -- resolution lands where waves may NOT be | logged-only (refine_source stamped in deck; log line) / physics | NEEDS-LOUDER |
| 15 | `workers/_sfincs_build/deck.py:2006-2037` netamt rainfall fallback | precip accumulation path unavailable | rainfall magnitude derived from the netamt fallback formula | loud (locked + noted in deck) / physics | OK-as-is (documented, deck-visible) |
| 16 | `mesh/swmm_network.py:71-77,632,750` synthetic diameters / topology / sub-area | imported GIS carries no size/topology/sub-catchment | demo-default pipe diameter, junction depth, fully-synthetic sub-area | loud (`SyntheticInput(basis=...)` labels) / physics+data | OK-as-is (declared synthetic) |
| 17 | `mesh/raster_cell_mesh.py:1183` roughness/imperviousness demo defaults | user did not set the constitutive lever | Manning n / imperviousness fall to historical literals (drive routing) | logged-only (labeled demo default) / physics | NEEDS-LOUDER (demo-default physics knob, deck-only provenance) |
| 18 | `mesh/raster_cell_mesh.py:1220,1433` outfall cell -> globally-lowest active | no lowest-boundary cell found | drainage outfall placed at the globally lowest cell | SILENT / physics | NEEDS-LOUDER (drainage point moves; no log, no note) |
| 19 | `workers/modflow/gwt_adapter.py:179` `DEFAULT_SFR_STREAMBED_GRADIENT` linear profile | no DEM rbot supplied for the SFR reach | streambed slope = flat demo gradient (drives Manning flow) | loud (SyntheticInput label on the archetype deck) / physics | OK-as-is (archetype benchmark, "not a site") |

### E. Worker postprocess (the SWAN class)

| # | WHERE | TRIGGER | WHAT DEGRADES | LOUDNESS / SEVERITY | VERDICT |
|---|---|---|---|---|---|
| 20 | `workers/_landlab_postprocess/postprocess.py:134` `src_crs = dst_crs` | landlab output raster has `crs is None` | assumes EPSG:4326 -- if the grid is projected, the layer lands in the WRONG place/scale | **SILENT** / **data (map integrity)** | **NEEDS-LOUDER** (SILENT, geospatial) |
| 21 | `workers/landlab/run_chain.py:202` result.json write fail | result block write raises | composer recomputes metrics from the field COGs | logged-only / operational | OK-as-is (COGs are the primary contract; honest recompute) |
| 22 | `workers/swan|geoclaw|sfincs` outputs.json seam absent -> publish_manifest -> on-box download (`workflows/*/{wave_field,inundation,flood}`) | `outputs.json` / `publish_manifest` absent | falls to the register-only, then the agent-side on-box publication path | logged-only / operational | OK-as-is NOW (byte-equivalent per ADR 0280/0281; WAS the dead-primary -- see finding D-1) |

### F. TELEMAC river-dye (reference-good gating)

| # | WHERE | TRIGGER | WHAT DEGRADES | LOUDNESS / SEVERITY | VERDICT |
|---|---|---|---|---|---|
| 23 | `workflows/telemac/river_dye/river_dye.py:432` bank_source gate | real-bank mesh geometry unavailable | forces the user to explicitly retry `bank_source="constant_ribbon"` (assumed channel width) -- NEVER silently substituted | user-gated + `fallback_note` (idealized-bed) / physics | OK-as-is (GOLD -- gated + narrated) |
| 24 | `workers/telemac/telemac_river_dye_build.py:839` NHDArea fetch fail -> constant width | NHDArea polygon fetch fails INSIDE the ribbon path | channel width becomes a constant | logged-only / physics | NEEDS-LOUDER (width assumption not surfaced beyond the gate note) |
| 25 | `telemac_river_dye_build.py:1205` water-polygon domain fail -> ribbon | water-polygon domain build fails | domain shape reverts to the geometric ribbon | logged-only / physics | NEEDS-LOUDER |
| 26 | `telemac_river_dye_build.py:1593-1703` 3DEP DEM fallback rung | primary DEM fetch fails after retries | USGS 3DEP ImageServer point-sample bed | loud (data-source norm: primary->fallback->honest typed) / data | OK-as-is |

### G. HEC-RAS demonstration geometry

| # | WHERE | TRIGGER | WHAT DEGRADES | LOUDNESS / SEVERITY | VERDICT |
|---|---|---|---|---|---|
| 27 | `workflows/hecras/{riverine_flood,levee_breach,flood_2d,culvert_embankment_flow}` `_DEMO_GEOMETRY_NOTE`/`_FIDELITY_NOTE` | off-scope arbitrary-AOI request | the solve runs on BAKED demonstration geometry (Muncie), not the user's AOI -- the answer is a demo, not the site | loud (`fallback_note` hoisted to the LLM) / physics | NEEDS-GATE (narrated but not user-CONFIRMED; a place-named request silently answers with foreign geometry) |

### H. Gates + registry (fail-open)

| # | WHERE | TRIGGER | WHAT DEGRADES | LOUDNESS / SEVERITY | VERDICT |
|---|---|---|---|---|---|
| 28 | `gates/input_review.py:256` headless fail-open | direct-call/offline turn has no turn entry | INPUT_REQUIRED gate proceeds with labeled inputs | loud (labeled "fail-open") / operational | OK-as-is (live WS turns have a turn entry; gate fires) |
| 29 | `gates/tool_gating.py:161` cold-index fail-open | empty/cold ranking or any fault | registry left ungated for the turn | logged-only / operational | OK-as-is (routing, not physics) |
| 30 | `gates/cards/region_choice.py:93` headless fail-open | headless client, no confirm channel | keeps the whole-state bbox default | loud (labeled honest degrade) / operational | OK-as-is |
| 31 | `gates/actionability.py:84` classify fault -> safe degrade | internal classification fault | safe default message | logged-only / operational | OK-as-is |
| 32 | `emission/uri_registry.py:29,898` unknown-URI pass-through | non-registered storage URI | user-supplied/external URI passes untouched | logged-only / operational | OK-as-is (by design -- never block user sources) |

## Summary by class

Rows tabled: 32 (physics- and data-consequential paths enumerated; operational
gate/transport clusters represented by one row each).

By SEVERITY:
- physics: 12, 14, 15, 16, 17, 18, 19, 23, 24, 25, 27  (11 rows)
- data / map-integrity: 1, 2, 5, 11, 20, 26  (6 rows)
- operational: 3, 4, 6, 7, 8, 9, 21, 22, 28, 29, 30, 31, 32  (13 rows)
- cosmetic: 10  (1 row)
- (row 16 spans physics+data)

By LOUDNESS:
- user-gated: 1, 23  (2)
- loud (envelope-narrated): 2, 5, 7, 15, 16, 26, 27  (7)
- logged-only: 3, 4, 6, 8, 9, 10, 11, 14, 17, 21, 22, 24, 25, 29, 31, 32, plus 13, 28, 30 label-in-log  (~19)
- SILENT (no log, no note): 12 (to the user), 18, 20  (3)

By VERDICT:
- OK-as-is: 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 13, 15, 16, 19, 21, 22, 23, 26, 28, 29, 30, 31, 32  (23)
- NEEDS-LOUDER: 11, 12, 14, 17, 18, 20, 24, 25  (8)
- NEEDS-GATE: 27  (1)
- DEAD-PRIMARY-RISK: the image-COPY gap (finding D-3, currently DORMANT)

### SILENT + physics/data rows -- verbatim

Row 12 -- SFINCS wide active-mask (SILENT to user, physics):
`workers/_sfincs_build/deck.py:852-930` computes the active-cell mask; when the
DEM elevation range is unreadable it returns
`(_MASK_FALLBACK_ZMIN=-1000.0, _MASK_FALLBACK_ZMAX=9000.0, adaptive=False)` -- a
window wide enough to keep the whole domain active. `workflows/sfincs/flood/flood.py:1748`
detects `adaptive is False` and builds `_mask_note` ("SFINCS active-cell mask used
a WIDE FALLBACK elevation window (DEM range unreadable).") but the ONLY consumer is
`logger.warning` at line 1754. `_mask_note` is never attached to the result
envelope or a `fallback_note`. Consequence: the solved domain includes cells a real
DEM range would exclude -- flooded-area and the map change -- and the user is told
nothing. This is the honesty-floor line: a degraded sim reading `status=ok` with no
narration.

Row 18 -- SWMM outfall relocation (SILENT, physics):
`mesh/raster_cell_mesh.py:1220` -- when `_lowest_boundary_cell` returns None the
outfall silently falls to `_lowest_active_cell` (the globally lowest cell). The
drainage discharge point moves with no log and no note. Answer-affecting for the
network's routing/outfall load; the user cannot see it happened.

Row 20 -- Landlab CRS assumption (SILENT, data/map-integrity):
`workers/_landlab_postprocess/postprocess.py:134` -- `if src_crs is None: src_crs =
dst_crs  # fallback: assume already 4326`. If a landlab output grid is in a
projected CRS but carries no CRS tag, the layer is reprojected AS IF it were 4326 --
it lands in the wrong place at the wrong scale, silently.

## Dead-primary findings (the SWAN method applied)

D-1. SWAN + GeoClaw emit-on-solve (RESOLVED, verified alive). The exemplar class.
ADR 0281 fixed it: the SWAN Dockerfile now COPYs `_swan_postprocess` +
`_raster_postprocess` (they were never in the image -- the composer comment "the
SWAN worker does NOT emit a manifest yet, so today this always falls back" was
literally true) and the entrypoint uploads `pp.cog_paths`. Verified: both
`workers/swan/Dockerfile:145-171` and `workers/geoclaw/Dockerfile:231-259` COPY
the postprocess dirs AND run an in-image import smoke. Both proven live through
rebuilt images (runs `01M08ACMKWQ7XV23ZFJ06SND76`,
`01M089JY3DWBZ9ZREE0TWG9ZQN`). Primary alive.

D-2. SFINCS COG CRS fallback (row 11) -- alive but untested-in-anger. The
`EPSG:3857` fallback fires only if `sfincs_map.nc` carries no resolvable `crs`
variable. SFINCS/cht reliably writes it, so in practice this never hits -- which is
exactly the SWAN risk shape: a silent fallback nobody watches. If a future SFINCS
build stops writing `crs`, every COG silently tags 3857 and the flood layer
misplaces, logged-only. Not currently dead, but it is a candidate for a hard error
(no CRS -> raise) rather than a guess.

D-3. landlab / openquake worker IMAGES omit the postprocess COPY
(SUSPICIOUS, currently DORMANT). Static check:

- `workers/landlab/Dockerfile:85`,
  `workers/openquake/Dockerfile:98` COPY ONLY their own `workers/<engine>/` dir --
  NOT `_<engine>_postprocess/` nor `_raster_postprocess/`. Their build-time smokes
  import only the solver chain, never the postprocess.
- Each entrypoint does `from workers._<engine>_postprocess import
  run_<engine>_postprocess` inside a `try/except Exception -> LOG.warning("...
  postprocess failed (non-fatal)")` (swmm:451-465, landlab:638-659,
  openquake:469-483). In a clean image that import raises ModuleNotFoundError,
  swallowed, `publish_manifest_uri` stays None.

So in those IMAGES the postprocess is DEAD -- byte-for-byte the SWAN bug shape. Why
it is DORMANT rather than live-broken: these three engines dispatch with
`exec_kind="exec"`, NOT `docker` (swmm `run_swmm.py:591`, landlab
`run_landlab.py:459`, openquake `psha.py:1769`). The `exec` path runs the entrypoint
as `sys.executable -m workers.<engine>.entrypoint` with the REPO ROOT on PYTHONPATH,
so `_<engine>_postprocess` resolves from the working tree and the primary is alive
on the live local stack. The images are the decommissioned cloud/Batch path (AWS
torn down 2026-08-06). The landmine: anyone who (a) resurrects a container dispatch
for these engines, or (b) trusts the register-only publish_manifest and deletes the
on-box composer path (openquake `psha.py:1562` has NO on-box fallback -- a None
manifest is a hard "empty solve?" error), gets a silently dead postprocess. The COPY
lines should be added now (cheap, matches geoclaw/swan) so the image is never a trap.

## Mechanism options for NATE

The root discomfort is hidden polymorphism: a fallback that continues without
declaring itself at a severity the swap deserves. Three options.

Option 1 -- lightweight: universalize the EXISTING `fallback_note` seam +
activation telemetry.
`LayerURI.fallback_note` already exists, is set by HEC-RAS/SCHISM/GeoClaw/river-dye,
and is hoisted into the LLM payload by `adapters/adapter.py:2497`. Make it the
mandatory narration channel: every physics/data fallback activation (rows 12, 14,
17, 18, 20, 24, 25) writes a `fallback_note` AND a structured
`fallback_activated` JSONL telemetry line (name, severity, trigger). Add a
lints/test that greps the physics-severity fallback sites for a `fallback_note`
write, mirroring the honesty-floor tests. Cost: ~1 day; touches ~8 sites; no new
contract. Weakness: it is convention-enforced, not type-enforced -- a new fallback
can still forget the note unless the lint catches the site.

Option 2 -- middleweight: a declared `FallbackSpec` registry + severity-based
gating.
Each fallback is a registered object: `FallbackSpec(id, primary, substitute,
severity, loudness_required)`. Activation goes through one `activate_fallback(spec,
context)` seam that ENFORCES the loudness floor -- `physics` severity cannot
activate without either a `fallback_note` (loud) or, above a configured threshold,
a user-gate (like the DEM gate and the river-dye bank_source gate already do). The
registry is auditable (a page listing every declared fallback + its live activation
count, the same shape as `unknown_quantity_fallback_count`). Cost: ~3-4 days; a new
contract + a refactor of the ~11 physics sites to route through the seam. Weakness:
real surface area for a single-user product -- risks the "abstraction for futures
nobody asked for" charter smell if only a handful of fallbacks ever gate.

Option 3 -- status quo + fix the NEEDS-* rows ad hoc.
Leave the architecture; fix the 8 NEEDS-LOUDER rows (attach the note), gate row 27,
add the D-3 COPY lines. Cost: ~1 day, zero new mechanism. Weakness: the NEXT
fallback is born hidden again -- this treats symptoms, not the class, which is
exactly the pattern that produced the SWAN bug.

### Recommendation

Option 1, with the two hard fixes from Option 3 folded in (row 27 gate + the D-3
COPY lines). Rationale: the declaration channel (`fallback_note`) and an auditable
counter pattern (`unknown_quantity_fallback_count`) ALREADY exist and are already
proven-good on the GOLD rows (DEM gate, river-dye gate, HEC-RAS note). Universalize
what works and make it lint-enforced rather than build the FallbackSpec registry
(Option 2) that a single-user product does not yet justify -- the charter's "no
reader, no feature" bar. The one thing Option 1 must borrow from Option 2 is the
severity concept: the lint keys on a `severity="physics"` tag at each fallback site
so the "did you narrate it" check has teeth. Revisit Option 2 only if the activation
telemetry shows a class of fallbacks that genuinely need gating (not just
narration) at a volume that a registry would tame. Costs: Option 1 ~1 day + the two
fixes ~0.5 day; net ~1.5 days to close every NEEDS-* row and make the next hidden
fallback fail a test instead of shipping.
