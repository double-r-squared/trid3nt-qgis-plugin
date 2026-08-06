# ADR 0156 - SCHISM CAND-S tail: one transport-validation template + HA STOP/DOC

Date: 2026-08-05
Status: accepted

## Context

Four SCHISM CAND-S board rows closed the "easy tier": (1)
`transport_scheme_accuracy_comparison` (upwind vs TVD^2 numerical mixing of a
heat/tracer front), (2) `generic_passive_tracer_mass_conservation` (a conservative
GEN tracer's domain-integrated mass), (3) `tidal_constituent_extraction_inrun`
(SCHISM's in-run harmonic analysis, iharind), (4)
`harmonic_analysis_ccrm_wiki_method_notes` (the harme.53 -> combine_outHA ->
ad2tct.f legacy chain). All four are S-tier hypotheses under the triage-first law.

Triage against the in-image binaries (v5.11.0: `pschism_TVD-VL` hydro-core,
`pschism_WWM_GOTM_TVD-VL`, and the full-monty
`pschism_WWM_COSINE_ICM_FIB_SED_ANALYSIS_PREC_EVAP_PAHM_HA_MARSH_GEN_AGE_TVD-VL`)
and the published `test_suite.md` module-needed column established, per row, what
actually runs before any build:

1. **Test_HeatConsv_TVD / Test_HeatConsv_Upwind = "Module needed: None"** -> the
   transport-scheme contrast runs on the CLEAN hydro-core binary. Empirically
   confirmed: the barotropic (ibc=1) run FREEZES T/S ("Barotropic model without ST
   calculation"), so a live tracer needs `ibc=0` (baroclinic) + a 3D vgrid.
   `itr_met=1` is REJECTED ("Unknown tracer method 1"); the documented scheme
   toggle is `itr_met=3` (horizontal TVD) reading a per-element `tvd.prop` (1 =
   TVD^2 limiter, 0 = first-order upwind everywhere) + `h_tvd`. So both schemes run
   on the SAME binary through the SAME code path, differing only by `tvd.prop` -
   the identical-flow control that isolates numerical mixing.
2. **Test_GEN_MassConsv = "Module needed: GEN"** -> USE_GEN is a full-monty-only
   build, and the full-monty binary unconditionally initializes EVERY compiled
   module (needs icm.nml, sediment.nml, cosine.nml, ...): heavy and fragile. But
   the mass-conservation MECHANISM rides the same transport solver; a conservative
   TEMPERATURE tracer on the hydro-core binary demonstrates it with zero namelist
   burden.
3. **iharind needs USE_HA** (param.nml comment + Dockerfile: USE_HA is only in the
   full-monty toggles, NOT the hydro build). Empirically confirmed: the tidal
   binary `pschism_TVD-VL` runs to completion with `iharind=1` (param.out echoes
   IHARIND=1) but writes NO harmonic-analysis output - the flag is a SILENT no-op
   because USE_HA is not compiled. In-run HA is genuinely unavailable on the
   shipped barotropic-tidal surface.
4. The legacy fortran chain (harme.53 / combine_outHA / ad2tct.f) does NOT ship in
   the runtime image (`ls /opt/schism/bin` = the 3 pschism binaries only; no HA
   post-processors). It is documentation-class, as predicted.

## Decision

**Rows 1 + 2 -> ONE landed template `schism_transport_validation`** (registry 216
-> 217, EXPECTED_TEMPLATES 58 -> 59). A thin composer that advects a temperature
FRONT across SCHISM's own QuarterAnnulus M2 tidal channel TWICE through the
identical flow - once with the TVD^2 limiter (`tvd.prop=1`), once with first-order
upwind (`tvd.prop=0`) - on the EXISTING hydro-core binary via the generic
run_solver seam. Deliverables:

- **Row 1 (numerical mixing):** the fraction of the initial front spatial VARIANCE
  each scheme retains at run end. Upwind is more numerically diffusive.
- **Row 2 (mass conservation):** the domain-integrated conservative-tracer MASS
  drift over the run - the numerical-scheme sanity gate. The GEN-module-specific
  path (full-monty, every-namelist) is documented in the result's
  `gen_module_note`, not built.

Both are plain arithmetic off the scribed `temperature` netCDF (invariant 1). No
LayerURI: the mesh is schematic (planar verification geometry), so the product is
the scheme-contrast CHARTS + typed `SchismTransportValidationResult` scalars.
**No worker-image change** - the deck (temp.ic front, 3D vgrid, `tvd.prop`, ibc=0
param.nml) is authored server-side; entrypoint.py + the binaries are untouched.

**Row 3 -> STOP with recipe.** In-run HA is not a knob on the shipped tidal
binary. Recipe to land: either (a) add a `USE_HA=ON` hydro variant to the worker
Dockerfile (a 4th targeted binary, mirroring the WWM+GOTM leg) + author harm.in
(the 6-constituent S2/M2/N2/K1/O1/Q1 sample) + set `iharind=1`, then combine the
per-rank harme output; or (b) drive the full-monty binary (which HAS USE_HA -
`_HA_` in its name) with the every-module namelist kit. Either is a worker-image
rebuild, out of easy-tier scope. Zero registry growth.

**Row 4 -> DOC (method notes).** The harme.53 -> combine_outHA -> ad2tct.f chain
is Dr. Andre Fortunato's ADCIRC-derived offline post-processor (CCRM wiki): it
reads the per-rank `harme.53` least-squares harmonic fit SCHISM writes when
USE_HA+iharind are on, `combine_outHA` stitches the subdomain outputs into
global per-node amplitude+phase per constituent, and `ad2tct.f` converts to a
tidal-constituent table. It is functionally equivalent to an offline t_tide/UTide
fit of the elevation time series, differing in that the fit is assembled DURING
the run (no full time-series output needed) rather than after. None of these
utilities ship in the image (they are not built by the SCHISM cmake used here);
the offline t_tide/UTide route over a station/out2d elevation series is the
already-available alternative. No template, zero registry growth.

## Consequence

- Live product-path smoke (run_solver + MinIO, sim_days=2.0, ~4 M2 cycles):
  130 nodes / 108 elements / 5 layers. TVD retains 95.7% of the front variance;
  upwind retains 90.7% -> upwind numerically mixes 2.16x more. Mass drift TVD
  -0.69%, upwind -1.04% (both within the +/-3% conservative-tracer sanity bound;
  the drift is open-boundary tidal exchange, oscillating at the M2 period).
  validated=True. Matches the direct-docker probe (95.70/90.69/2.17) exactly.
- Proofs (dock render_spec interpreter): `schism_transport_validation_mixing.png`
  (the two variance-retention curves visibly diverging) +
  `schism_transport_validation_mass_conservation.png` (the mass-drift oscillation
  within the bound), under docs/proof/templates/.
- The transport-scheme axis (itr_met/tvd.prop) and conservative-tracer mass
  conservation are now surfaced SCHISM aspects; the GEN-module and USE_HA builds
  remain documented recipes for a future worker-image wave.
