# ADR 0256 - Knob-eligible trio from the 0255 sweep (SFINCS structures + ELMFIRE crown-fire V&V; GMPETable deferred)

Date: 2026-08-14
Status: accepted
Continues: ADR 0255 (long-tail sweep, which tagged these three KNOB-ELIGIBLE with
recipes), ADR 0123 (elmfire_verification_elliptical_replication - the verification
harness twinned here), ADR 0161 (crown-fire machinery), the Streeter-Phelps /
0153/0163/0167 closed-form V&V pattern.

## Context

ADR 0255 adjudicated 12 long-tail board rows and tagged three as KNOB-ELIGIBLE
(verified machinery, deferred to their own job): the SFINCS weir/thin-dam
structures cluster, the OpenQuake GMPETable gsim knob, and the ELMFIRE crown-fire
exact-solution regression gate. This job builds the two whose substrate is fully
in hand and confirms the third stays STOP/deferred on un-fetchable data.

## Front 1 - SFINCS hydraulic structures (LANDED)

Board rows `weir_levee_from_dem_derived_crest` + `thin_dam_flow_barrier` -> ONE
`StructureSpec` knob on the existing `sfincs_flood` template (registry unchanged
at 255, no new template).

hydromt_sfincs 1.2.2 `setup_structures(structures, stype='weir'|'thd', dep, dz)`
was wired into `sfincs_builder`:
- `StructureSpec(geometry_uri, stype, dz, par1)` + `BuildOptions.structures`;
  a single `setup_structures:` YAML block is emitted (yaml.safe_load collapses
  duplicate keys, so one structure block per deck - a run carries at most one
  spec, whose geofile may hold many same-type lines).
- A WEIR samples its crest from the model bed: `dep` reuses the SAME staged DEM
  `setup_dep` uses, and `dz` raises the crest that many metres above the terrain
  under the line (the DEM-derived-crest path). A THIN DAM needs no crest.
- Composer surface (`model_flood_scenario` / `sfincs_flood`): `structure_lines`
  (drawn/pushed lon/lat polylines) or `structure_uri` (a prior geofile), plus
  `structure_type` + `structure_crest_dz_m`. Geometry is written to an EPSG:4326
  LineString GeoJSON agent-side.
- INPUT-REVIEW gate: a weir crest is an un-fetchable engineering value, so a weir
  with no `structure_crest_dz_m` returns a typed `USER_INPUT_REQUIRED` failed
  envelope (never a fabricated crest); a thin dam needs none. (The byo-mesh /
  speculative-blocker generic gate NATE deferred is NOT built - only the explicit
  structure-file wiring.)

Host-side hydromt authoring, NO image rebuild (deltares/sfincs-cpu:sfincs-v2.3.3
consumes sfincs.weir / sfincs.thd natively via the weirfile/thdfile keywords).

Live in-image DISCRIMINANT (present vs absent, run through the sfincs docker
image; Chattanooga TN ~4 km AOI, 100-yr / 3-h design storm rain-on-grid):

| run | max depth (m) | wet cells |
|-----|---------------|-----------|
| A) plain (no structure) | 2.63 | 5718 |
| B) with thin dam | 2.95 | 5700 |

max abs depth diff 1.37 m; 228 cells changed > 5 cm; the barrier blocks lateral
flow and ponds water against the line (max depth 2.63 -> 2.95 m). Run ids
01M00PTH5HG740H8S5SJJ2J2T8 (plain) / 01M00PVVP75WHZNMTQD59FENJD (thd). Proof:
`docs/proof/templates/sfincs_flood_hydraulic_structure_weir_thd.png` (plain |
thd | difference, filled cells over Esri World Imagery, cyan structure line).

## Front 2 - ELMFIRE crown-fire ROS V&V (LANDED)

Board row `crown_fire_exact_solution_regression_gate` -> a new registered
verification template `elmfire_crown_fire_active_ros_verification` (registry
254 -> 255), the twin of `elmfire_verification_elliptical_replication` for the
crown-fire regime, plus the Cruz closed-form reference module + an offline test
gate (the Streeter-Phelps precedent style).

Published-first: Cruz, Alexander & Wagner (2005), Can. J. For. Res. 35:1626-1639,
active crown-fire rate of spread `R = 11.02 * U10^0.90 * CBD^0.19 * exp(-0.17 *
EFFM)` [m/min]. ELMFIRE (elmfire_spread_rate.f90:177-179) implements it verbatim
with the 20-ft -> 10-m open-wind conversion `WS10KMPH = WS20MPH * (1.609/0.87)`;
`cruz_crown_fire.py` reproduces the m/min closed form and cites both.

The composer authors an ALL-CONSTANT canopied deck (SH7 fuel, cbd 0.18 kg/m3,
flat terrain, uniform wind), crown model on, the Cruz rate ceiling LIFTED
(uncapped, so the closed-form rate carries the front, not the MIN() cap) and the
critical canopy cover set below the deck cover (so the burn is ACTIVE crown),
solves in the container, measures the numerical HEAD spread rate (head extent /
duration off the ToA raster), evaluates the Cruz form at the deck's own inputs,
and gates the relative error to 5 %.

Live in-image V&V (trid3nt-local elmfire image; 20 mph @20ft, cbd 0.18, EFFM 3 %,
30 m, 0.4 h; run 01M00PNSM36HJTK7DM5FWRGKC6): numerical head ROS 123.75 m/min vs
Cruz closed form 123.16 m/min = **0.48 % relative error**, active-crown area
0.947 km2, no edge touch, PASSED. (ELMFIRE's own vs-raster peak 123.16 m/min
matched the closed form exactly; the independent geometric head measurement
matched to 0.48 %.) Proof:
`docs/proof/templates/elmfire_crown_fire_active_ros_verification.png` (filled-cell
active-crown ellipse + numerical-vs-Cruz panel).

## Front 3 - OpenQuake GMPETable (DEFERRED, STOP confirmed)

Board row `tabulated_gmpe_hazard_curve` stays DEFERRED per ADR 0255: GMPETable is
importable in-venv but the `gmpe.hdf5` tabulated-ground-motion table is
UN-FETCHABLE (no US fetcher; needs a shipped demo table or an input-review-gated
user table). No data path -> no live V&V -> left STOP/deferred with that reason.

## Decision

Land the SFINCS structure knob (host-side, input-gated, live discriminant 1.37 m)
and the ELMFIRE crown-fire Cruz-ROS verification template (0.48 % live error).
Keep GMPETable deferred on un-fetchable data.

## Consequence

- Registry 254 -> 255 (one new template: elmfire_crown_fire_active_ros_
  verification). SFINCS structures are a knob on the existing sfincs_flood, no
  registry change.
- New contract `ElmfireCrownRosVerificationLayerURI`; new closed-form module
  `cruz_crown_fire.py`; offline test `test_elmfire_crown_ros_verification.py`.
- Two board rows LANDED (weir/thd folded into one), one V&V row LANDED, one row
  (GMPETable) confirmed DEFERRED.
- Coverage metric: +1 registered template (255), +1 knob on sfincs_flood.

## Addendum 2026-08-14 - proof re-framed to directional flow (NATE catch)

NATE caught that the landed thd proof
(`docs/proof/templates/sfincs_flood_hydraulic_structure_weir_thd.png`) used
rain-on-grid forcing: rain falls on both sides of the barrier, so panels A and
B look nearly identical and the signal hid entirely in the diff panel. A
barrier proof needs DIRECTIONAL flow crossing the line so A and B diverge
visibly. Re-ran the discriminant using the composer's live coastal
water-level-boundary path (`sfincs_flood(coastal=True)`, `structure_lines` +
`structure_type="thd"`) instead of pluvial rain-on-grid.

Three real US AOIs were tried (elevation-profiled offline via
`fetch_topobathy` before each pick -- never guessed):

1. **Grand Isle, LA** -- mostly submerged marsh/bay; the whole ~3x3 km AOI sat
   below the composer's `SEAWARD_BOUNDARY_ZMAX_M=2.0 m` cap, so ALL FOUR edges
   became msk==2 boundary and the domain equilibrated near-instantly to a
   uniform ~10 m depth (the resting bathymetric water column) -- no
   directional signal at all (both cases: 9900/9900 wet cells, max diff
   0.004 m). Ruled out.
2. **Bay St. Louis, MS** -- a genuinely south-only boundary (elevation
   profiled: south edge mean -1.8 m, north/east/west mean +2.7..+5.3 m NAVD88),
   but the bluff is a SHARP natural crest that already stops a 1000-yr
   (~4.4 m) surge on its own within 24 hr -- the plain run's surge never
   crossed the bluff top either, so the dam had no headroom to matter (max
   diff 0.80 m, but concentrated in a single row right at the line with zero
   difference beyond it in either run). Ruled out.
3. **Waveland, MS** (FINAL, used in the regenerated proof) -- same clean
   south-only boundary, but a GENTLE slope (~0 m -> ~5 m NAVD88 over ~3.5 km)
   instead of a sharp crest, giving the surge (and the co-occurring
   design-storm rain -- see below) real room to propagate past the dam's
   position when the dam is absent.

**HONEST FINDING on the sign**: `sfincs_flood` always emits the
return-period design-storm precipitation ALONGSIDE the coastal surge (there
is no coastal-surge-only mode in the current composer) -- so the barrier is
tested against a COMPOUND surge+rain deck, not surge alone. In the lee band
immediately north of (behind) the dam, the dammed run (B) trends WETTER than
the plain run (A) (0.82 m -> 0.85 m mean, max local diff 0.39 m, 385/2227
lee cells changed >5 cm), not drier: the barrier excludes Gulf surge from the
south but also blocks the co-occurring rain from draining south to the sea,
trapping it immediately behind the line -- the real, well-documented "leveed
interior cannot self-drain" problem (why real levee districts need interior
pump stations), not a bug in the structure knob or the smoke's diff math.
Verified NOT edge leakage: a column-wise diagnostic across the lee band shows
the diff signal concentrated at the structure line's MIDPOINT (0.39 m) vs its
ENDPOINTS (0.0005 m) -- genuine blocking, spatially aligned with the drawn
line.

Live in-image runs (Waveland, MS ~3.4x3.9 km AOI, 1000-yr surge / 24-hr,
south-edge-only water-level boundary + design-storm rain): plain run_id
`01M010DRQZHXY53NP3AEJMGSH8`, thd run_id `01M010JV7CEPMDF9QBER37CNN9`. Full
numbers in `docs/proof/sfincs_structure_smoke_result.json`.

Driver: `scripts/run_sfincs_structure_smoke.py` (rewritten for the coastal
directional path + the honest lee-band/structure-alignment gate, replacing
the old rain-on-grid present-vs-absent check). Proof regenerated IN PLACE:
`scripts/proof_sfincs_structure.py` ->
`docs/proof/templates/sfincs_flood_hydraulic_structure_weir_thd.png` (A |
B | difference, filled cells over Esri World Imagery, thin-dam line drawn,
caption carries the numbers and the directional-flow + trapped-rain
framing). No other files under `docs/proof/templates/` were touched.

No composer/contract code changed -- this addendum is proof-methodology only
(a different, more honest live-run configuration of the same landed
`StructureSpec` knob).

## Addendum 2 2026-08-14 - the RAIN LEVER + a clean surge-only levee proof

The first addendum's HONEST FINDING was that `sfincs_flood` had no
surge-only mode: it ALWAYS co-emitted the return-period design-storm rain, so
the barrier was tested against compound surge+rain and the dammed run trapped
rain behind the line (WETTER, not drier). This addendum lands the fix and a
proof that actually shows a levee working.

### RAIN LEVER (composer + builder)

`model_flood_scenario` / `sfincs_flood` grow a `rainfall` parameter:
`"design_storm"` (default, behavior byte-identical) emits the Atlas-14
return-period precipitation; `"none"` builds a SURGE-ONLY deck with NO
`setup_precip_forcing` block, so a storm-surge inundation question is answered
without co-occurring rain wetting the whole domain / both sides of a barrier.

- Composer: `rainfall` is normalized + validated (an unknown value is a typed
  `RAINFALL_MODE_INVALID` failed envelope, never a silent coercion), and it is
  mutually exclusive with `forcing_raster_uri` (an observed-precip forcing).
  On `rainfall="none"` the precip lookup + `ForcingSummary` build are skipped
  and a `ForcingSpec(forcing_type="surge_only")` (envelope
  `ForcingSummary.forcing_type="storm_surge"`) flows to the builder.
- Builder: `forcing_type="surge_only"` emits NO precip block. Invariant 7 gate:
  a surge-only `ForcingSpec` with NO surge / tide / discharge driver hard-errors
  (`FORCING_OUT_OF_RANGE`) at the top of `build_sfincs_model` rather than
  authoring a silently-empty (zero-forcing) deck.
- Offline pins (`server/tests/test_sfincs_rainfall_mode.py`, 3 tests):
  design-storm deck emits `setup_precip_forcing` with the design-storm
  magnitude; surge-only deck OMITS the precip block but KEEPS
  `setup_waterlevel_forcing`; surge-only-with-no-driver raises
  `FORCING_OUT_OF_RANGE`.
- Registry unchanged (a parameter on the existing template, no new tool). Two
  surge-only queries added to the flood `corpus.yaml`.

### THE PROOF - surge-only levee DISTRICT (Waveland, MS)

Two live in-image runs on the SAME Waveland AOI (~3.4x3.9 km,
`(-89.40, 30.265, -89.36, 30.300)`), `rainfall="none"`, coastal
1000-yr / 24-hr surge. Two live-run facts drove the final configuration:

1. **DETERMINISTIC surge.** The composer's coastal auto-wire pulls LIVE NOAA
   CO-OPS observed tides (anchored on wall-clock), so back-to-back runs got
   different surge series (one filled the whole domain to a resting column, one
   a clean south flood) -- not reproducible, and A vs B not even comparable.
   The proof now synthesizes the DETERMINISTIC parametric design-storm surge
   ONCE (`_synthesize_parametric_surge_forcing`, peak ~4.4 m at 1000-yr) and
   drives BOTH runs with the identical `surge_forcing` water-level boundary.
2. **Return walls, not a bare line.** SFINCS `setup_mask_bounds` marks EVERY
   active-domain edge cell at/below +2 m NAVD88 as an msk==2 surge inlet, and
   along this coast the WEST and EAST domain edges are also low (bayou/sound) --
   so a bare shore-parallel line is FLANKED: surge enters from the side edges
   north of the line (measured: protected mid-third -57 %, but west/east thirds
   0-1 %). Real levee districts solve exactly this with RETURN WALLS that turn
   landward and tie into high ground, so the barrier is a U open to the high
   (non-boundary) north edge: a shore-parallel south line + a west wall + an
   east wall. The south line is placed just inland of the permanent Gulf/channel
   waterline (rows whose deep-cell span clears a min-span threshold, so isolated
   spurious deep DEM pixels -- e.g. a single -42 m pit reading 46.5 m -- do not
   drag the placement); the walls run to past the north edge. Permanent deep
   water (> 5 m, a resting column the levee cannot dry) is excluded from the
   protected-district metric.

Result (protected DISTRICT = strictly inside the enclosure, dry land only):

| run | district land-wet cells | mean depth (m) | max (m) |
|-----|-------------------------|----------------|---------|
| A) no levee (surge-only) | 1298 | 0.80 | 3.99 |
| B) levee district (surge-only) | 59 | 1.14* | 2.57 |

**95 % drier** inside the district (1298 -> 59 cells; the diff panel shows a
clear blue dry-out signal, not the near-zero of the compound-rain runs).
*B's few residual cells sit at the wall corners, so their MEAN reads higher
than A's on far fewer cells -- the COUNT is the signal. No wall leak (B
near-wall max 2.34 m <= interior 2.57 m). Live run ids: plain
`01M014P5DQGWF1YK8W3X42SJ57`, levee `01M014V1KC24CB7GYKT4XRW6XT`. Full numbers:
`docs/proof/sfincs_surge_only_smoke_result.json`.

Driver: `scripts/run_sfincs_surge_only_smoke.py` (deterministic surge +
data-driven waterline placement + return-wall enclosure + district metric).
Proof regenerated IN PLACE:
`docs/proof/templates/sfincs_flood_hydraulic_structure_weir_thd.png` (A | B |
difference, filled cells over Esri World Imagery, cyan U-shaped barrier,
"surge-only (rainfall=none)" framing + the numbers). No other files under
`docs/proof/templates/` were touched.
