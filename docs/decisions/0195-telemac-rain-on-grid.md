# ADR 0195 - TELEMAC-2D rain-on-grid foundation (validation primitives + CN infiltration + precip/mesh verdicts)

Date: 2026-08-08
Status: Accepted (foundation landed offline; the registered template + worker
image rebuild + live Coweeta proof are a scoped follow-on build wave)
Source: Godara, Bruland and Alfredsen 2024, "Comparison of TELEMAC-2D and
HEC-RAS 2D for rain-on-grid flash-flood modelling in a steep catchment"
(Frontiers in Water 6:1384205, doi 10.3389/frwa.2024.1384205). NATE-provided.

## Context

NATE approved building toward a rain-on-grid (RoG) flash-flood capability
replicating the Godara et al. 2024 protocol on a US catchment: NSE/R2 validation
primitives, a TELEMAC RoG template, and (later waves) a HEC-RAS RoG twin plus a
full protocol replication. This ADR records THIS wave: the shared validation
primitives, the SCS-CN infiltration preprocessing, and the two research verdicts
(native CN model, precipitation product) that shape the template build.

RoG is a DISTINCT question class from the `rainfall_evaporation_forcing` knob
that landed on `telemac_river_dye` in ADR 0190. That knob adds a distributed
rain source ON an inflow-boundary-driven reach. RoG asks "what discharge /
inundation does THIS rainfall produce over THIS catchment" -- a hydrodynamic
rainfall-runoff model with NO inflow hydrograph, the outlet discharge being the
PRIMARY product.

## Decision 1 - shared validation primitives (LANDED)

Two paper-exact metrics live with the shared metric code (NOT a new top-level):

- `nash_sutcliffe_efficiency` (eq 14) and `pearson_r2` (eq 13) added to
  `compute_skill_metrics.py` and exported. Both delegate to
  `spotpy.objectivefunctions` (nashsutcliffe / rsquared) -- the V&V build
  contract forbids bespoke metric math, and spotpy implements EXACTLY the paper
  equations (verified: hand-computed fixtures match to 6 dp). Non-finite pairs
  dropped; zero-variance / <2-pair series return `None`, never a fabricated
  number.
- `build_hydrograph_overlay_chart` added to `charts_common.py` -- the
  computed-vs-observed discharge overlay (two colour-split line series on a
  shared numeric-or-temporal time axis, NSE/R2 folded into the caption, dock
  render_spec geometry matching the sibling `build_*_chart` engine-output
  builders). Honesty floor: `None` when the computed series has <2 finite points.

Usable from the code_exec playground and importable by validation templates.
Offline: `server/tests/test_hydro_validation_metrics.py` (12).

## Decision 2 - CN infiltration path (LANDED as preprocessing; NATIVE model wired by design)

`workflows/telemac/rain_on_grid/cn_infiltration.py` (pure, offline-tested, 15
tests in `server/tests/test_telemac_rain_on_grid_cn.py`).

Research verdict (installed TELEMAC v9.0.0, from the built `telemac:latest`
image sources `runoff_scs_cn.f` + `telemac2d.dico`):

- The native SCS-CN runoff model EXISTS (`RAINFALL-RUNOFF MODEL = 1`, Ligier
  2016). `ANTECEDENT MOISTURE CONDITIONS` (1 dry / 2 normal / 3 wet) and
  `OPTION FOR INITIAL ABSTRACTION RATIO` (1 = IA/S 0.2 standard / 2 = 0.05
  revised) are native keywords. The spatially-variable CN2 field is read
  per-node from `FORMATTED DATA FILE 2` and mapped onto the mesh by `HYDROMAP`.
- TWO hardcoded limits in the installed build:
  1. The Huang-2006 steep-slope correction is present in the source but compiled
     OFF: `STEEPSLOPECOR = .FALSE. !CAN BE A KEYWORD?` -- a hardcoded flag, not a
     keyword. Enabling it natively would require patching + recompiling the
     solver in the image.
  2. Rainfall in the runoff branch is `RAINDEF = 1` (a single CONSTANT intensity
     over the rain duration), also hardcoded. A time-varying real hyetograph
     cannot drive the NATIVE model without a recompile.

Path taken (no solver recompile):

- The module builds the per-node CN2 field (`node_curve_numbers`) for the native
  path -- uniform (`curve_number` knob) or land-cover-distributed (NLCD class ->
  CN/Manning Table-1 analog, or a direct `fetch_gcn250_curve_numbers` raster).
  When steep-slope correction is requested it is applied to the CN field in
  preprocessing via `huang_steep_slope_cn` (the EXACT rational Huang-2006 formula
  the engine's own dormant branch uses), reproducing eq-9 intent without a
  recompile. The engine then runs its native SCS-CN with these values +
  the AMC / IA-ratio keywords -> the native runoff model is genuinely used.
- For a time-varying MRMS hyetograph, `rainfall_excess_hyetograph` applies the
  SCS-CN transform (eq 7-8) up front and the excess (net) series is fed to
  TELEMAC as time-varying rain with `RAINFALL-RUNOFF MODEL = 0` (no
  double-counting). This is the paper's fallback preprocessing path, used here
  specifically to overcome the `RAINDEF=1` constant-rain limit.

The AMC conversions and the Huang formula are BYTE-PARITY with `runoff_scs_cn.f`
(asserted in tests) so the preprocessing and native paths agree.

Note on the paper's eq 9: the paper prints `CN_corr = CN2 * exp(0.0065*slope)`
and cites Huang 2006, but the ACTUAL Huang-2006 formula (and TELEMAC's
implementation) is the rational `(322.79 + 15.63*a)/(a + 323.52)` factor. The
module defaults to the engine-consistent rational form and keeps the paper's
exponential as `paper_exponential_steep_slope_cn` for exact-paper comparison.

## Decision 3 - precipitation product verdict

`fetch_mrms_qpe` (NOAA MRMS MultiSensor QPE Pass2, gauge-corrected, CONUS
~1 km) publishes a `1h` (hourly) accumulation -- the finest window and the right
flash-flood product. gridMET (`fetch_gridmet pr`) is DAILY, too coarse for a
sub-daily flash flood; ERA5 (`fetch_era5_reanalysis`) is global hourly but 27 km
and needs a CDS key. Honest limitation: the MRMS S3 archive begins ~2020-10, so
candidate storm events must post-date that (older events would need AORC or
Stage-IV, neither of which is a current fetcher -- flagged in the recon doc).
The template's precip step is structured so a user-supplied hyetograph can slot
in behind the same gate.

## Decision 4 - mesh acquisition (promotion plan; not yet promoted)

The template's domain is watershed-first: the ADR 0193 pysheds catchment polygon
meshed with the 0193 watershed mesher (custom SDF interior + distance-to-river
refinement, GPL-isolated `mesh:latest`). Promotion is structured as its OWN
template step (`build_watershed_mesh.py` logic lifted into an importable
`mesh_acquisition` function) so a user-supplied mesh can later slot in via the
planned precondition gate; the standalone sandbox stays standalone. The ADR 0194
coastal-mesh files (`water_edge.py`, `build_coastal_mesh.py`, delaware/tampa
outputs) are NOT touched.

## Deferred to the build wave (with reasons)

The registered `telemac_rain_on_grid` template body, the worker deck/entrypoint
changes (RUNOFF MODEL keywords + FORMATTED DATA FILE 2 CN writer + normal-depth
outlet BC + max-velocity COG), the strict-parser bump to `telemac-reach-3` +
rejection test, the `telemac:latest` image rebuild, and the live Coweeta proof
are a scoped follow-on wave. Reason: the worker-image law requires a full rebuild
+ behavior-proving live smoke THROUGH the image, and the live Coweeta run is
hours-class -- neither is completable or verifiable inside a single build session,
and shipping unverified worker/deck/template code would violate the offline-first
and worker-image-staleness hard rules (worker code is inert until rebuild;
offline-green != deploy-green).

## Applicability envelope (bake into the template docstring)

Per the paper's conclusion: RoG reproduces SINGLE-STORM flash-flood events
(~10-20 h) in small steep catchments. Multi-peak / sustained rain-on-snow events
are NOT reproduced -- infiltrated water is permanently lost (no soil-routine /
subsurface return flow), so inter-peak baseflow is missed. TELEMAC-2D's
triangular mesh is stable on steep terrain (a paper finding vs HEC-RAS's
structured grid). US-only via our fetchers; the paper's own site (Sleddalen,
Norway) is the METHODOLOGY source, replicated on a US steep gauged catchment
(Coweeta, NC) per the US-cases rule.

## Consequences

- +0 registered tools this wave (primitives are functions on existing modules;
  the template is deferred). Registry unchanged.
- No worker image rebuilt this wave; no flood seam touched -> no flood canary.
- Offline: `test_hydro_validation_metrics.py` (12) + `test_telemac_rain_on_grid_cn.py`
  (15); no regression in `test_compute_skill_metrics` / `test_chart_tools` /
  `test_engine_chart_emission` (65).
