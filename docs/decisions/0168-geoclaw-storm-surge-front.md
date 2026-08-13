# 0168: GeoClaw parametric-Holland storm-surge front

Date: 2026-08-07
Status: landed

## Context

The GeoClaw `surge` scenario was a v0.1 STUB: the deck author only set a uniform
`geo_data.sea_level` offset -- no wind, no pressure, no storm. So the whole storm
surge scenario modeled nothing but a raised flat sea, and `drag_law` (the ADR 0143
`wind_drag_law_selection` row) was INERT: a drag law is meaningless without a wind
forcing to apply it to. ADR 0155 STOPPED that row for exactly this reason ("the
surge/wind module is absent; drag_law is inert without wind_forcing + a storm
file, inseparable from a parametric Holland storm module").

Clawpack 5.14.0 ships that module: `clawpack.geoclaw.surge`, with a Fortran
`storm_module` that reads a storm-track file and evaluates an analytic Holland-1980
wind + pressure field, plus three wind-stress drag laws (`storm_module.f90`:
`0=no_wind_drag`, `1=garratt_wind_drag`, `2=powell_wind_drag`). The canonical
example is `geoclaw/examples/storm-surge/ike` (Hurricane Ike 2008, Galveston).
Triage confirmed the module surfaces in the worker image; the Python
`Storm.write(file_format='geoclaw')` storm-file format is a plain 3-line header +
7-column rows (`t, lon, lat, max_wind_speed, max_wind_radius, central_pressure,
storm_radius`), so the deck author can write it DIRECTLY (no pandas-backed `Storm`
class import -- the worker stays clawpack-import-free + unit-testable).

## Decision

Build the real parametric-Holland surge front on the existing GeoClaw deck surface.

1. **Storm deck machinery** (`services/workers/geoclaw/setrun_builder.py`):
   - `render_storm_file(spec)` -- a PURE string render of the GeoClaw storm file
     (byte-format-identical to `Storm.write('geoclaw')`), from `spec.storm_track`
     (7-tuples, times SECONDS RELATIVE TO LANDFALL). No user track -> a
     NON-SITE-SPECIFIC synthetic demo storm (`_synthetic_demo_track`) making
     landfall at the AOI centroid, surfaced as synthetic in the driver descriptor.
   - `render_setrun_py` surge branch: `surge_data.wind_forcing/pressure_forcing =
     True`, `storm_specification_type='holland80'`, `storm_file`, the storm AMR
     refinement (`wind_refine`/`R_refine`); the surge geo constants (`rho`,
     `rho_air`, `ambient_pressure`, `coriolis_forcing=True`); the storm aux layout
     (`num_aux=7` = 3 shallow + 1 friction + 3 storm, matching surge_data's default
     wind_index=4/pressure_index=6 -> Fortran 5/6/7); and the run WINDOW opening
     BEFORE landfall (`clawdata.t0 = t0_s < 0`, `tfinal = t0_s + sim_duration_s`)
     so the storm spins up. dam_break / tsunami keep the 3-aux `t0=0` layout
     BYTE-IDENTICAL (guarded by a regression test).
   - `wind_drag_law` selector `none|garratt|powell -> drag_law 0|1|2` (an unknown
     name raises loudly): the ADR 0143 lesson -- a knob MUST land a distinct value.
   - Strict-parser allowlist gains `storm_track`, `wind_drag_law`, `t0_s`; parser
     version `geoclaw-spec-2 -> geoclaw-spec-3` (ADR 0158 stale-image guard).
   - `build_geoclaw_deck` writes `storm.storm` for surge (supersedes the v0.1 stub,
     which is DELETED, not disabled).

2. **Contracts** (`geoclaw_contracts.py`): `StormTrackPoint` (t_s/lon/lat/max wind
   speed+radius/central pressure/storm radius, ascending-time validated),
   `WindDragLaw` Literal, and `GeoClawRunArgs.{storm_track, wind_drag_law,
   surge_t0_s}` (additive, absence byte-neutral). The composer
   (`run_geoclaw.build_geoclaw_build_spec`) threads them for surge, deriving `t0_s`
   = explicit `surge_t0_s` else the track's earliest time else half the run before
   landfall.

3. **Template** `geoclaw_storm_surge` (`workflows/geoclaw/storm_surge/`): the
   LLM-facing surge question class (question CLASS, not place) -- "how high does a
   hurricane surge flood this coast", forced by a storm track with a selectable
   drag law. Rides the existing `model_geoclaw_inundation` fetch->deck->solve->
   postprocess chain (real coastal topo-bathy). Corpus + `categories.py` +
   `tools/__init__.py` registration; model-free `retrieve_visible_tools` surfaces
   it for surge/hurricane/drag-law prompts.

## Live evidence (through the rebuilt worker image)

WORKER-IMAGE LAW 0148 + CONTEXT-DRIFT LAW 0158: image rebuilt with absolute paths;
`docker history` carries ZERO `/home/nate/Documents/GRACE-2` references; the
rebuilt image reports `geoclaw-spec-3` + drag codes `{none:0, garratt:1, powell:2}`.

- **Ike anchor** (published NHC ATCF `bal092008` best track, Garratt drag,
  Galveston/Bolivar, idealized planar Gulf shelf, 30x24 base + 2-level AMR, 15 h
  window -12 h..+3 h): peak coastal surge surface elevation **5.4 m** on the RIGHT
  (east) side of the track near Bolivar (-94.21, 29.56); peak onshore inundation
  3.77 m; coastal gauge peak 1.79 m. Real Ike observed surge was ~4.5-6 m (15-20 ft)
  at Bolivar/Chambers County, on the right of the track -- the modeled peak is in
  the observed magnitude class AND on the physically-correct right-of-track side.
  QUALITATIVE anchor (idealized shelf + coarse grid + shortened spin-up under-
  resolve absolute magnitude; the point is the mechanism + order + geometry).

- **Drag-law A/B** (synthetic demo track, identical setup, only `wind_drag_law`
  differs -- Garratt vs Powell): peak coastal surge 5.096 vs 4.953 m (Delta 0.14 m);
  coastal gauge peak eta 1.989 vs 2.286 m (Delta 0.30 m); onshore inundation 3.99
  vs 3.75 m (Delta 0.24 m). The knob MEASURABLY moves the surge (Powell's high-wind
  drag saturation redistributes the stress) -- it is NOT a no-op (ADR 0143 gate met).

## Consequence

- The ADR 0143 `wind_drag_law_selection` row FLIPS from STOP -> LANDED (the drag
  law is now a live, measurable knob on a real wind forcing).
- Board rows unblocked (ml-signoff-shortlist GeoClaw storm-surge front, 4):
  `best_track_to_storm_file`, `parametric_holland_wind_surge_ike` (#20),
  `gridded_wind_*` (parametric path; a gridded-NetCDF wind upgrade remains a future
  fold), `wind_drag_law` (0155 retag). The surge scenario is no longer a sea-level
  stub.
- Registry pin 224 -> 225 (+`geoclaw_storm_surge`); EXPECTED_TEMPLATES 66 -> 67.
- SFINCS spiderweb-surge overlap (fidelity ladder): GeoClaw surge is the
  refinement-grade nonlinear-SWE+AMR path; SFINCS spiderweb stays screening-grade.

## Not done / future folds

- Gridded-wind (NetCDF OWI / HWRF) storm specification (`storm_specification_type <
  0`, `set_ascii_fields`/`set_netcdf_fields`) -- the Isaac gridded-wind row; a
  storm-file variant, deferred to a fold.
- An IBTrACS/best-track FETCHER already exists (`fetch_storm_tracks`); wiring it as
  an auto-source for `storm_track` (so the agent can name a historical storm) is a
  follow-up composer fold. This front hardcodes the published Ike track in the
  smoke fixture (published-first), which is the anchor's ground truth.
