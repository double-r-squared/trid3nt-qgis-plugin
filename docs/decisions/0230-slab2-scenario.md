# ADR 0230 - Slab2 scenario source: the SCENARIO rung of the earthquake-source ladder

Status: Accepted
Date: 2026-08-12

Cross-links: ADR 0226 (the Okada front + the measured-inversion finite-fault rung this
extends), ADR 0229 (the deep-water topobathy rung the scenario run-up rides), ADR 0227
(the fetched bathymetry surfaced as a Case input layer), ADR 0225 (the declared-
resolution doctrine the new `target_resolution_m` honours).

## Context

ADR 0226 built the earthquake-source ladder with two rungs, both keyed on a REAL named
ComCat event:

1. MEASURED finite-fault inversion -- the event's published USGS finite-fault product
   (`finite_fault.py`) drives an N-subfault Okada dtopo. `basis="measured_inversion"`.
2. DERIVED single-subfault scaling -- a real event with NO finite-fault product ->
   one Wells & Coppersmith rectangle from Mw. `basis="derived"`.

A whole class of question falls to NEITHER rung: a SCENARIO ask -- "what if Cascadia
ruptures at M9?" -- is not a real event, so there is no measured slip to fetch and no
catalog epicenter to resolve. Under the old code such an ask could only reach the
single-rectangle derived rung (hand-typed `source_lonlat` + `source_magnitude`), which
renders as ONE straight uplift bar (NATE's catch on the 0226 single-subfault proof). A
subduction megathrust follows the CURVED trench; a straight rectangle is the wrong
geometry.

The 0226 scoped follow-up named the fix: for a scenario source, take the geometry from
the REAL published USGS Slab2 subduction-interface model (depth/strike/dip grids), tile
the interface into subfaults, distribute a target-Mw tapered slip, and reuse the SAME
`to_csvfault_text` -> `finite_fault_file` seam the measured rung uses.

## Decision

Land the SCENARIO rung as a `scenario_fault` front door on `geoclaw_inundation`,
producing the SAME `FiniteFaultModel` the measured rung produces -- so the entire
downstream seam is reused verbatim (`to_csvfault_text` -> `stage_finite_fault_csv` ->
`finite_fault_uri` -> the worker's `dtopotools.CSVFault` multi-subfault Okada dtopo).
**No worker/image change and no `GeoClawRunArgs` contract change**: a scenario is just
another way to fill `finite_fault_uri` + `finite_fault_footprint`.

### 1. Slab2 ingestion (`scenario_slab2.py`)

- Source truths (declared per ADR 0225; DOI 10.5066/F7PV6JNV, Hayes et al. 2018,
  Science 362:58-61): ScienceBase parent item `5aa1b00ee4b0b1c392e86467` carries one
  CHILD item per subduction zone; each child holds `<code>_slab2_{dep,str,dip}_*.grd`
  GMT-NetCDF grids (depth to slab top [km, negative down], strike [deg], dip [deg]).
  Zone codes: Cascadia = `cas`, Alaska-Aleutians = `alu` (the two US zones this landing
  wires; the roster carries ~27 on the same pattern).
- `fetch_slab2_grids(zone)` resolves the zone's child item + grid URLs by the
  ScienceBase children API + the `<code>_slab2_<param>_` file-name pattern (robust to
  ScienceBase per-file id churn), downloads + caches under `TRID3NT_CACHE_DIR/slab2`,
  and short-circuits on a fully-cached zone (the re-run fast path AND the offline seam).
  The HTTP boundary (`_http_get`) is monkeypatchable, mirroring `finite_fault.py`.
- `parse_slab2_grids` is a PURE parser (NetCDF via xarray): normalizes the Slab2 0-360
  longitude to -180..180 ascending, keeps the NaN-padded ragged edges, and returns a
  `Slab2Grids` with `interface_lon_at(lat, depth)` (traces the trench -- the lon
  migrates with lat AND depth, the source of the curvature) and `sample(lon, lat)`
  (nearest-cell depth/strike/dip, `None` off-slab so NaN edges never bleed).

### 2. Scenario resolver (`resolve_slab2_scenario`)

Given a zone + target Mw (+ optional epicenter/extent hints + `target_resolution_m`):

- **Rupture dimensions** -- Strasser, Arango & Bommer (2010), SRL 81(6):941-950,
  subduction-INTERFACE regressions: `log10 A = -3.476 + 0.952 Mw`,
  `log10 L = -2.477 + 0.585 Mw`, `log10 W = -0.882 + 0.351 Mw` (M9.0 -> A~1.24e5 km2,
  L~614 km, W~189 km -- a full-margin Cascadia rupture).
- **Tiling** -- along-strike extent = L centered on the epicenter latitude, clipped to
  the modeled interface; down-dip extent = a depth band whose dip-projected width = W.
  Each patch centroid is placed by TRACING the interface (`interface_lon_at`) so its
  lon follows the curve, and strike/dip are Slab2-sampled AT THAT centroid -- the
  curvature is real, not a straight rectangle. Rake 90 deg (pure megathrust thrust).
- **Slip** -- a tapered-cosine (Tukey 1967) window in both directions (flat interior,
  cosine-tapered to zero at the rupture edges -- a standard scenario slip taper that
  keeps peak/avg realistic, ~2x, not the ~4x of a full Hann), scaled so the summed
  moment `Sum(mu * area_i * slip_i)` EXACTLY equals `M0(Mw)` (Hanks & Kanamori 1979,
  `Mw = (log10 M0 - 9.1)/1.5`; rigidity mu = 30 GPa, the standard shallow-interface
  value, declared). Off-slab patches (NaN sample) are dropped. There is NO degrade rung
  -- a scenario the interface cannot support is a typed error, never a silent rectangle.

### 3. Composer surface (`geoclaw_inundation`)

- New knobs: `scenario_fault` (zone name), `scenario_magnitude` (REQUIRED target Mw),
  `scenario_epicenter_lonlat` (optional rupture center + domain source point),
  `target_resolution_m` (subfault patch edge, default 20 km).
- An `elif scenario_fault:` front door parallel to `earthquake_source`: resolve the
  scenario model, stage its CSV through the shared `stage_finite_fault_csv` seam, set
  `finite_fault_uri` + `finite_fault_footprint`, force scenario "tsunami", and stamp
  `basis="scenario_slab2"` provenance naming the zone/Mw/scaling+taper laws.
- `basis="scenario_slab2"` is a NEW `InputBasis` member (contracts) that
  `render_assumptions_line` renders as its own LOUD group -- "SCENARIO (hypothetical
  rupture on real published geometry, NOT a real event)" -- so it can never be mistaken
  for a real event. The measured/derived `fault_geometry` default_demo note is now
  guarded to fire only when NO finite fault (measured or scenario) supersedes it.

### 4. Resolution declaration (ADR 0225)

`target_resolution_m` is the new resolution-class param, named per the NATE convention.
`_SCENARIO_TILING_RES_SPEC` declares `>=5000 m` (`constraint_source="data"`: finer than
the ~5 km Slab2 grid native buys no interface-geometry fidelity; coarser is a valid
cheaper tiling, so no upper bound). `enforce_resolution` quotes an out-of-range ask
back. The self-enforcing 0225 sweep now covers it (the tool carries the spec).

## Consequence

- **+0 registered tools / templates**: knobs + a resolver on the existing
  `geoclaw_inundation`. Registry pin + EXPECTED_TEMPLATES UNCHANGED (36 registry/pin +
  geoclaw regression tests green).
- **No worker/image rebuild, no build_spec bump**: the scenario emits the SAME
  finite-fault CSV the measured rung emits (`geoclaw-spec-6` unchanged); the worker
  already builds the N-subfault dtopo from it. This is the payoff of the 0226 seam.
- +1 `InputBasis` member (`scenario_slab2`); +1 `ResolutionSpec`.

## Basis-verification note (honest scope)

The USGS Slab2 ScienceBase distribution (`www.sciencebase.gov`, Cloudflare-fronted) is
UNREACHABLE from this build datacenter (curl 000/502, WebFetch 403; `earthquake.usgs.gov`
and the S3 public-content bucket ARE reachable). The URLs + children-API contract are
declared against the published Slab2 data release; the production `fetch_slab2_grids`
children-API + download path is exercised by a monkeypatched offline test. For the LIVE
proof the Slab2 grid cache is PRE-SEEDED with a Cascadia interface grounded in the real
trench geometry (convex-west trench -124.5->-129, ~11 deg ENE dip, depth to 60 km) --
the curvature the deformation proof demonstrates is genuine. The live ScienceBase pull
is a flag for NATE to verify the exact child-item URLs once (see the final report).

## Live evidence (Cascadia M9.0 scenario, local-docker)

Direct-call `geoclaw_inundation(scenario_fault="Cascadia", scenario_magnitude=9.0,
scenario_epicenter_lonlat=(-125.5, 45.0), target_resolution_m=25000, bbox=Newport OR,
coastal_gauge_lonlat=(-124.10, 44.62), sim_duration_s=3600, amr_levels=2,
fgout_frames=15)`:

- **The scenario resolved through the REAL composer** (not a unit test): the log line
  `resolve_slab2_scenario zone=cas Mw=9.00 -> 200 subfaults, A=123595 km2 L=614 km
  W=189 km, slip 0.00-20.38 m, realized Mw=9.000, footprint=(-127.49, 42.24, -122.38,
  47.76)`. The composer then enclosed the domain to the rupture footprint
  (`bbox=(-127.79, 41.94, -122.08, 48.06)`, a full-margin Cascadia domain) and engaged
  the ADR 0229 deep-water `force_bathy_base` rung -- confirming the scenario front door,
  the moment normalization (realized Mw EXACTLY 9.000), the curved footprint, and the
  domain enclosure end to end on the live path.
- **The curved-interface money shot**: `scripts/proof_slab2_scenario_geometry.py` renders
  the 279-subfault tiling (target_resolution_m=20 km) over Esri World Imagery -- the
  subfault grid FOLLOWS THE CURVED trench (centroid lon migrates with latitude,
  corr(lon,lat) = -0.62), the slip is Tukey-tapered (0 at the edges, ~20 m interior),
  and it sums to Mw 9.00. `docs/proof/templates/geoclaw_scenario_cascadia_geometry.png`.
  This is the direct answer to the NATE straight-bar catch, without needing the solve.

### The bathy-scale wall on the full-margin run-up SOLVE (honest scope)

The full M9 run-up SOLVE did NOT complete on this 15 GB datacenter: the topobathy fetch
over the ~6 x 6 deg full-margin domain is bottlenecked assembling the DENSE Cascadia
CUDEM 1/9" nearshore composite (90 of 930 tiles intersect the domain; ETOPO deep base +
CUDEM merge over ~10^8 cells). This is the SAME class of limit ADR 0226 flagged
("topobathy over a large rupture-enclosing domain") -- and it is WORSE at Cascadia scale
than the sparse-CUDEM Alaska domain ADR 0229 solved: 0229 removed the 3DEP-clobber, but
the sheer CUDEM tile density of a full Cascadia margin is a compute/RAM scale limit, not
a correctness defect. It is NOT a scenario-source defect.

Critically, the scenario's ONLY new code is the geometry generation (`scenario_slab2.py`).
The ENTIRE downstream chain -- staged finite-fault CSV -> the worker's
`dtopotools.CSVFault` N-subfault Okada dtopo -> `dtopo.tt3` + `deformation_dz.asc` ->
the GeoClaw solve -> coastal gauge mareogram + fgout decay -- is the IDENTICAL
`finite_fault_uri` seam ADR 0229 already proved LIVE end to end for the real 294-subfault
Chignik inversion (amplitude decays with distance 0.138->0.062->0.032 m, arrival orders
by distance, leading-depression mareogram). A scenario produces byte-identical-format
CSV, so it drives that already-proven path. The remaining gap is purely the full-margin
Cascadia bathy assembly (a tractable-domain live solve, or a coarser-domain-bathy knob
for scenario runs, is the scoped follow-up -- see the final report flags).

Proof scripts ready for a tractable-domain or completed solve:
`scripts/drive_geoclaw_scenario_cascadia.py` (the live driver, pre-seeds the Slab2 grid
cache -- ScienceBase-walled here) + `scripts/proof_geoclaw_scenario_cascadia.py` (the 5
run-up renders: deformation / bathy input / max amplitude + AMR mesh / gauge / transect).

## Files

- `server/.../geoclaw/scenario_slab2.py` (NEW) -- ingestion + resolver.
- `server/.../geoclaw/inundation/inundation.py` -- `scenario_fault` front door + knobs +
  `_SCENARIO_TILING_RES_SPEC`.
- `contracts/.../common.py` -- `scenario_slab2` InputBasis + the LOUD scenario group in
  `render_assumptions_line`.
- `server/tests/test_geoclaw_scenario_slab2.py` (NEW) -- parse / tiling / moment / fetch
  I/O boundary / ladder-label tests.
- `scripts/_slab2_fixture.py`, `scripts/drive_geoclaw_scenario_cascadia.py`,
  `scripts/proof_geoclaw_scenario_cascadia.py` -- the fixture + live driver + proofs.

## 0230 follow-up: scenario-scale coarse bathymetry + the completed M9 solve (2026-08-12)

The original 0230 wave PROVED the curved-interface geometry (the deformation money-shot)
but the full-margin run-up SOLVE was bottlenecked assembling the dense NOAA CUDEM 1/9"
nearshore composite across a whole ~6x6 deg Cascadia domain (~1e8 nearshore cells / ~15 GB
of detail a deep-ocean propagation grid cannot hold). NATE FLAGGED the row PARTIAL. This
follow-up removes that bottleneck and completes the live M9 solve + proofs + showcase.

### Design: a DECLARED basin-scale bathymetry floor (not a universal rollout)

Two module constants in `inundation.py` and one new kwarg thread:

- `_SCENARIO_BATHY_TARGET_RES_M = 1852.0` (1 arcminute at the equator) -- the scenario
  front door's DECLARED domain-wide bathymetry cell. Deep-ocean tsunami propagation is
  well-resolved at the arcminute class (ETOPO 2022's ~15" deep-ocean native; NOAA's
  operational tsunami propagation grids run ~4'), so an arcminute ETOPO base is the
  correct, cheaper substrate for a basin-scale run.
- `_GEOCLAW_CUDEM_SKIP_RES_M = 500.0` (the ADR 0224 precedent) -- at/above this cell the
  fine CUDEM 1/9" nearshore composite is SKIPPED domain-wide: it is far finer than the
  coarse basin grid's own cell, so its structure cannot survive resampling and reading the
  dozens of per-tile CUDEM COGs is wasted network cost with zero fidelity gain.
- `_fetch_topo_for_geoclaw(..., target_resolution_m)` threads the floor: when set it caps
  the composite (`min_pixel_m`) and, at/above the skip threshold, passes `skip_cudem=True`
  and emits the LOUD log line. `None` on every non-scenario path keeps the byte-identical
  native full-resolution fetch. `model_geoclaw_inundation(..., bathy_target_resolution_m)`
  carries it from the scenario front door only.

Crucially the COARSENING IS DECLARED, not silent: a `SyntheticInput(param=
"bathy_target_resolution_m", basis="default_demo", real_source_if_any="ETOPO 2022 ...")`
provenance row renders in the assumptions line, and the fine coastal AOI STILL nests its
own fine SHORE topo (`_fetch_fine_nearshore_for_geoclaw`) so the run-up stays resolved.
Same declared-per-tool pattern the 0224 surge/tidal path already carries -- NOT a global
default change.

### Verification: the live Cascadia M9.0 solve (LANDED)

Live local-docker solve (`drive_geoclaw_scenario_cascadia.py`, Slab2 cache pre-seeded with
the real-curved-geometry fixture -- ScienceBase is Cloudflare-walled from this box, flag b):
run 01KZW4N9RDHKECRF8C1JHP9T3C, completion.json status=ok, ~45 min wall (uncontended;
base grid 90x90 -> amr_levels=5 planned over the tiny coastal AOI, 66348 active cells). The
coarse-mode declaration fired verbatim: `SCENARIO-scale bathy target 1852 m >= 500 m ->
CUDEM 1/9" nearshore composite SKIPPED (basin-scale run, ETOPO 2022 deep-water column
only)`; the staged base topo is 634x467, min -4377 m, wet_frac 0.828 (flat-ocean gate PASS
on real deep water), with the fine ~10 m nearshore nested separately over the AOI.

Physics asserts on the M9 result (proofs in `docs/proof/templates/`):

- Deformation dipole tracks the CURVED trench: max uplift +5.42 m (seaward subfault band,
  convex-west, following the trench), max subsidence -2.88 m (landward) -- a signed
  megathrust dipole, NOT a straight bar. `geoclaw_scenario_cascadia_deformation.png`.
- Nonzero coastal amplitude at the Oregon coast: Newport gauge (-124.10, 44.62) mareogram
  rises coseismically to ~0.42 m then peaks 1.07 m peak-to-trough over 30 min; max coastal
  fgout surface perturbation 3.39 m (fgout monitor is placed over the coastal AOI).
- Peak overland inundation: max_depth 25.45 m, flooded_area 2.86 km2, 398422/987772 ocean
  cells masked (computed by `postprocess_geoclaw` before the original run's disk-full COG
  write; the deformation COG was rebuilt from the staged `deformation_dz.asc`).
- Decay/arrival transect: for a DISTRIBUTED full-margin M9 the classic radial-decay
  diagnostic is DEGENERATE and this is itself the correct physics -- the 614x189 km rupture
  coseismically displaces the whole footprint at once, so the four transect points (which
  sit ON the ruptured seafloor, the domain extending only to -127.79 W, barely past the
  trench) all show a simultaneous ~180 s coseismic offset (+2.9 m uplift far / -3.1 m
  subsidence near) with no radial spreading. The compact-source transect built for the
  Chignik point-source does not transfer to a full-margin source; the deformation dipole +
  coastal mareogram carry the physics. `geoclaw_scenario_cascadia_transect_chart.png`.

Five proof renders (EPSG:3857 over Esri World Imagery, captions declaring the coarse
basin bathy): deformation / bathy input / max amplitude + AMR mesh / Newport gauge / transect.

### Offline

`test_geoclaw_scenario_bathy_scale.py` (NEW, 5 cases): native fetch never skips CUDEM;
scenario-scale target skips + floors (min_pixel_m == target, resolution_m clamped <=1000);
below-threshold floors but keeps CUDEM; the scenario default is at/above the skip
threshold; the skip is LOUD. Full geoclaw + topobathy + slab2 suites green (306 passed, the
single fetch_topobathy fetch-resolution-gate failure is the known baseline transient, not
geoclaw). Four alphabetical slices from repo root = the exact SIX baseline failures
(fetch_resolution x4 + river_dye x2), no geoclaw regressions.

### Flags

- (a) full-margin M9 solve: CLEARED (run 01KZW4N9..., status ok, physics verified above).
- (b) ScienceBase Slab2 grid URLs: OPEN -- NATE's one-time reachability check (Cloudflare-
  walled from this datacenter; the live proof + showcase pre-seed the real-geometry fixture,
  the production children-API path is exercised by the monkeypatched offline test).
- (c) showcase: seeded via `seed_showcase_cases.py --only "Slab2 scenario Cascadia"` (the
  entry's over-specified `sim_duration_s` 3600->1800 + `output_frames` 12->10 aligned to
  the PROVEN driver run, `timeout_s` 2400->14400 for the real wall; daemon Slab2 cache
  pre-seeded, `TRID3NT_SOLVER_TIMEOUT_S=14400`). Case 01KZWCHV2E054X057CHE0TFSFJ created,
  !run verified. The composer path is VERIFIED end to end and the SOLVE COMPLETED
  SUCCESSFULLY (run 01KZWCMQ3RRJK8S5XXJH1EX6BD, worker stdout "runclaw: Done executing",
  ~48 min compute, all outputs -- fort.q / 15 fgout / fgmax / gauge -- uploaded to s3 and
  intact). The composer's postprocess+publish was then REFUSED by MinIO `XMinioStorageFull`
  (the s3 backend hit its minimum-free-drive threshold: each geoclaw run persists ~7.7 GB
  of fort.q to data/minio and the shared box was at 99% disk), so the case has no published
  layers YET. This is an INFRA disk-capacity limit, NOT a scenario / coarse-mode /
  composer-code defect -- the outputs exist. A GREEN case needs the MinIO backend freed (a
  bulk s3-delete is guarded in this session) + a seed re-run, or a postprocess harvest of
  the intact 01KZWCMQ `_output`. Root cause note for the machine: fort.q accumulation in
  data/minio (22 GB across runs) is the disk pressure -- a retention/cleanup policy on old
  run `_output` would prevent the recurring XMinioStorageFull wall the original 0230 solve,
  this re-run, and the harvest all hit.
