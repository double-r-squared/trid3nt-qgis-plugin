# ADR 0286 -- emit-on-solve: the L-class SCHISM native-mesh leg (Option B)

Status: LANDED (offline build + live loop, all agent-side -- NO image rebuild binds
this leg). Date: 2026-08-18. Builds on ADR 0280 (the seam + the frozen `outputs.json`
schema), ADR 0282 (the M-class `frames_only` ruling), ADR 0283 (the L-class TELEMAC
native-mesh leg -- the pattern this ADR replicates EXACTLY), ADR 0285 (law 9 / the
SCHISM coastal-forcing refusals that gate the live drivers).

## Context -- the ruling: OPTION B (the 0283 precedent applied one-to-one)

The predecessor's recon (`/tasks` stop-clean report) refuted the emission-campaign
kickoff's central premise ("SCHISM produces ZERO temporal artifacts -> rasterize
per-step frames like SFINCS"). SCHISM is a **native-mesh-temporal engine, exactly
like TELEMAC**: the result IS a time-stepped UGRID netCDF (`out2d_1.nc` carrying
`elevation`/velocity/dry-flag dataset groups per `nspool` step; the 3D `salinity_1.nc`
for baroclinic) that QGIS/MDAL animates directly from its own dataset times. It was
ALREADY publishing a hand-wired temporal mesh layer (`layer_type="mesh"`) in every
composer -- the exact target design, but hand-wired (charter law-8 violation), the
identical situation ADR 0283 hit for TELEMAC's SELAFIN.

The orchestrator RULED **OPTION B** (applying NATE's ADR 0283 Fork-A precedent under
the AFK grant -- the SCHISM situation maps one-to-one to TELEMAC's): route the native
out2d/salinity mesh through the seam via `outputs.json kind=mesh` + `crs_authid`,
superseding the hand-wired `publish_input_layer(mesh_layer)` in ALL FOUR composers
against byte-equivalence; typed peak COGs + narration stay (`frames_only` builds the
mesh entry -- the 0283 generalization already does this). **NO per-step rasterization
anywhere.**

## Gate #1 -- THE MDAL DOCK-LOAD PROOF (the fork's genuine unknown, RESOLVED)

Before any build: real solved SCHISM out2d/salinity netCDFs (existing MinIO run
prefixes) staged the plugin's way (extension preserved, post-0283 fix) and loaded via
the REAL system-PyQGIS/MDAL offscreen harness (`QgsMeshLayer(local, name, "mdal")`).
Result (QGIS 3.40.6-Bratislava):

| File | isValid | dataset groups | temporal | time steps | reference time |
|---|---|---|---|---|---|
| `out2d_1.nc` (36-frame coastal) | True | 7 (elevation, depthAverageVelX/Y, dryFlag*, depth, bottom_index) | yes | 36 | 2008-09-12 00:00, hourly |
| `salinity_1.nc` (baroclinic 3D) | True | 1 (salinity) | yes | 24 | 2000-02-01 00:00, hourly |
| `out2d_1.nc` (12-frame baroclinic) | True | 7 | yes | 12 | -- |

MDAL loads SCHISM out2d/salinity natively, exposes the temporal dataset groups with a
real reference time + hourly dataset times -- **the Temporal Controller animates them
directly.** The fork's load-bearing unknown is resolved: Option B is viable.

## Decision -- what LANDED (the 0283 replication)

### 1. Seam producer half (new): `workflows/schism/results_mesh_seam.py`

`publish_results_mesh_via_seam(emitter, *, run_id, engine, peak_layers, mesh_uri,
mesh_name, crs_authid)` -- the SCHISM analogue of the TELEMAC helper. The composer
(acting as its own worker, host-exec) writes `outputs.json` via the host-exec writer
(peak raster entry(ies) + the `kind="mesh"` netCDF entry, `crs_authid` when
georeferenced), reads it back through the SEAM (`build_layers_from_outputs(
frames_only=True)`), and emits the mesh `LayerURI` via `publish_input_layer(
role="context")`. Best-effort: a write/read/emit miss degrades to peak-only, never
sinks the run. `crs_authid=None` supported (the idealized planar QuarterAnnulus mesh).

### 2. Postprocess: the three inline mesh `LayerURI` constructions DELETED

`postprocess_schism` / `postprocess_schism_waves` / `postprocess_schism_baroclinic`
each built + returned a `layer_type="mesh"` `LayerURI` (`schism-mesh-` /
`schism-wave-mesh-` / `schism-baroclinic-mesh-{run_id}`). All three DELETED; the
postprocess now returns ONLY the typed raster peak(s). `metrics` carries the
`mesh_uri` / `n_nodes` / `n_layers` / `is_geographic` the composer needs to author the
mesh entry (byte-identical name + uri + crs derivation).

### 3. The four composer forks (agent-side)

`tidal_hydro`, `pahm_surge`, `coupled_waves`, `baroclinic_circulation` each drop
`mesh_layer = layers[N]` + the hand-wired `publish_input_layer(emitter, mesh_layer,
role="context")` and call `publish_results_mesh_via_seam(...)` instead. The composer
keeps its OWN typed peak (elevation / Hs / surface+bottom salinity); the seam skips
the peak entries under `frames_only`, so no COG is registered twice.

### 4. Byte-equivalence (the supersession bar)

The seam mesh layer matches the deleted hand-wired one field-for-field:
name / `style_preset="mesh_grid"` (`model_results` registry row) / `role="context"` /
`crs_authid` / `uri` / `bbox=None`; only the `layer_id` STEM diverges (seam mints
`model-results-mesh-{run_id}`, the bespoke sites used `schism-*-mesh-{run_id}`). Per
the ADR-0281/0283 precedent the `layer_id` is an idempotence/dedup key; web temporal
grouping rides the `name` token (`detectSequentialGroups`), NOT the layer_id, so the
stem swap renders identically. Baroclinic's mesh uri is `out2d_1.nc` (the composer's
`mesh_uri` variable) -- IDENTICAL to what the hand-wired baroclinic mesh used, so the
migration is byte-preserving there too.

### 5. Per-template verdict (coverage law)

| Template | Native temporal field | Verdict | Migration kind |
|---|---|---|---|
| `schism_tidal_hydro` | out2d `elevation` | **MESH** (EPSG:4326 / None for QA) | supersede (byte-equiv) |
| `schism_pahm_surge` | out2d `elevation` (reproject_xy) | **MESH** (EPSG:4326) | supersede (byte-equiv) |
| `schism_coupled_waves` | out2d WWM groups | **MESH** + the Hs/Tp V&V chart | supersede (byte-equiv) |
| `schism_baroclinic_circulation` | 3D `salinity` + out2d mesh | **MESH ONLY** (3D column; per-step COG stack is lossy) | supersede (byte-equiv) |
| `transport_validation` | `temperature` (scheme contrast) | **CHARTS-ONLY** (no map animation) | not migrated (no mesh emit) |
| ICM / SED substrate smokes | scribed 3D tracers | **OUT of scope** (dev smokes, no composer) | n/a |

### 6. The `nspool` cadence lever (agent-side, NO rebuild)

`output_interval_min` (the charter law-8 universal name) added as an OPTIONAL param on
the three map composer tools (`schism_tidal_hydro`, `schism_pahm_surge`,
`schism_baroclinic_circulation`) + threaded through model -> driver -> the deck
authors. `deck_authoring._resolve_nspool(dt_s, output_interval_min)` maps DIRECTLY:
`nspool = round(output_interval_min*60/dt_s)` (>=1), wired in `_substitute_param_nml`
(tidal coastal_tin + surge), `_patch_transport_param`, `_patch_baroclinic_param`.
SCHISM requires `ihfskip` an integer multiple of `nspool`; the lever recomputes
`ihfskip = ceil(nsteps/nspool)*nspool` to preserve it. `None` = byte-identical hourly
default (unit-proven: `None == pre-lever` output for all three patch functions). The
QuarterAnnulus verification path keeps its published fixture cadence (verbatim stage).
Because SCHISM deck authoring is agent-side, the lever needs NO image rebuild.

### 7. ADR-0244 sweep-guard allowlist updated for the retired sites

`tests/test_input_layer_surfacing.py::_ALLOWLISTED_INPUT_EMISSION`: `tidal_hydro`
(1->0, removed) and `coupled_waves` (1->0, removed) drop out; `baroclinic` (2->1,
bottom-salinity result only) and `pahm_surge` (2->1, storm best-track only) decrement;
`schism/results_mesh_seam.py` (1) ADDED as the framework-emission home. Sweep green.

## Consequences

- All four temporal SCHISM composers publish their result netCDF through the
  framework-owned emit-on-solve seam instead of hand-wired emission (law 8). The three
  inline postprocess mesh constructions are gone.
- The 3D baroclinic salinity's fidelity is preserved (the native mesh carries the
  vertical column; a 2D COG stack could not) -- the case that decided Option B over
  per-step rasterization.
- Offline suite unmoved: schism suite green (76), sweep guard green (20), law9 guard
  green (15), outputs-manifest schema + landing green (30). `test_schism_baroclinic`
  reconciled (`len(layers) == 2`, mesh rides the seam).

## Live loop -- EXECUTED (2026-08-18, all agent-side direct-call, local schism docker)

Migrated classes solved LIVE through the composers (`proof_schism_seam_0286.py`); each
wrote `outputs.json` with the `kind="mesh"` entry (+ peak raster), the seam built the
`layer_type="mesh"` LayerURI (`mesh_grid`/`context`, crs populated), the typed peak
survived, and the native netCDF loaded through REAL QGIS/MDAL temporal:

| Class (postprocess path) | run_id | mesh name | crs | MDAL groups / steps |
|---|---|---|---|---|
| pahm_surge (`postprocess_schism`, covers tidal too) | `01M0AP17H1729N4XWPJF0SBCM9` | SCHISM mesh (64 nodes) | EPSG:4326 | 7 / 24 |
| baroclinic (`postprocess_schism_baroclinic`) | `01M0ANVQ7SKY2M5XTNKDR3VKFV` | SCHISM 3D mesh (56 nodes x 10 layers) | EPSG:4326 | 7 / 48 |

(pahm_surge ran on a CONSENTED synthetic domain, `allow_synthetic_domain=True`, with a
REAL Ike-2008 HURDAT track -- the law-9 driver here; coastal_tin needs a GSHHG shoreline
shapefile not staged on this box. baroclinic ran law-9-compliant: NWM-derived discharge
+ user salinity 33.5 psu, Delaware Bay.)

**nspool 2-cadence** (baroclinic Delaware Bay, 2-day, two solves differing ONLY in
`output_interval_min`): `=30` -> 96 out2d dataset-times (run `01M0AP4FHNDQNA3MYSJWKWEQKB`),
`=120` -> 24 (run `01M0AP8F138EDC7H1MA27SVZH7`); baseline `None` -> 48. Arithmetic: sim
48 h; 48/(oi/60): 30->96, 120->24, 60->48; 96/24 = 4.0 = 120/30 exactly. The `=120`
times sampled at [2,4,6,8,10,12] h confirm the 2-hourly cadence. The DECK-SIDE lever
moved the netCDF frame count end-to-end, no rebuild.

**Reopen check**: the seam mesh LayerURI rehydrates (WS `model_dump`) with
`layer_type="mesh"` + `crs_authid=EPSG:4326` + `style_preset="mesh_grid"` +
`role="context"` + the exact name -- the row the plugin `_add_mesh` reads on reopen.
Verified for both the baroclinic + surge runs.

**Gates**: schism suite (76) / sweep guard (20) / law9 guard + outputs schema (24) /
landing + schema (30) green; cadence None byte-identity unit-proven; daemon restart +
`ws_smoke all_passed=True`.

coupled_waves (`postprocess_schism_waves`) shares the IDENTICAL seam wiring + is
offline-green (`test_schism_coupled_waves`); its live WWM-coupled solve is a slow tail.
