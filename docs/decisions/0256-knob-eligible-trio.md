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
