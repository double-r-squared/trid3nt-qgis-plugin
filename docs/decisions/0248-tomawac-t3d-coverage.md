# ADR 0248 - TOMAWAC + TELEMAC-3D coverage close-out (board reconciliation, one STOP)

Status: LANDED (2026-08-13). Board-reconciliation + adjudication wave on the two
live TELEMAC wave/3D legs. 0 new tools, 0 image rebuild, 0 parser bump, 0 new
proof renders. Registry unchanged at 252.
Date: 2026-08-13

## Context

The MODULE-COVERAGE-BOARD carried open CAND rows in its TOMAWAC (7 across two
duplicate `### TOMAWAC` blocks) and TELEMAC-3D (3 in the first `### TELEMAC-3D`
block) sections. This wave adjudicated each row knob-or-STOP against the two live
worker legs (`tomawac_build.py` / parser `tomawac-wave-2`, `telemac3d_build.py` /
parser `telemac3d-strat-1`) baked in `trid3nt-local/telemac:latest`, verifying
keyword support against the in-image v9.0 tomawac/telemac3d dicos BEFORE any
build.

The pivotal finding: every physics-visible landing these rows describe was
ALREADY productionized end-to-end under ADR 0236 (TOMAWAC) and ADR 0241
(TELEMAC-3D). The open CAND tags were residual bookkeeping, not open work. The
only genuinely-unbuilt row is the 3D culvert.

## Verification (in-image, this session)

- Image `trid3nt-local/telemac:latest` present (3.55 GB, rebuilt 2 h prior).
- Workers confirmed live: `tomawac_build.py::solve` dispatches 4 modes
  (fetch_growth / shoaling / bottom_friction / wave_current);
  `telemac3d_build.py::solve` dispatches 3 modes (stratification /
  wind_circulation / salt_wedge).
- Registration confirmed: `tomawac_wave_field` and `telemac3d_stratified_flow`
  both in `EXPECTED_TEMPLATES`; registry pinned at 252 (unchanged).
- telemac3d.dico (in-image, `/opt/conda/opentelemac/sources/telemac3d/`)
  verified for the culvert row: `NUMBER OF CULVERTS`, `CULVERTS DATA FILE`,
  `OPTION FOR CULVERTS`, `MAXIMUM NUMBER OF SOURCES` present - culverts are
  treated as paired source/sink terms. So the culvert STOP is NOT binary-missing.
- Slices GREEN from repo root (venvs/agent, TRID3NT_CACHE_BUCKET unset): 
  `test_door_dissolution.py` 3 passed; `test_catalog_surfacing.py` 14 passed
  (registry_size == 252); `services/workers/telemac/test_entrypoint.py` 16 passed
  (parser pins telemac-reach-8, and tomawac-wave-2 / telemac3d-strat-1 present).

## Per-row disposition

TOMAWAC (both `### TOMAWAC` board blocks) -- ALL 7 rows -> LANDED. The two blocks
duplicate the same 4 question classes (an L-tier and an M-tier row per class,
plus the unpaired bottom-friction row). All fold into the ONE registered
`tomawac_wave_field` tool, live over real Lake Superior (ADR 0236 COMPLETE):

| row(s) | mode | live/proof |
|---|---|---|
| wind_generated_wave_growth [L] + fetch_limited_wind_wave_growth [M] | fetch_growth | Lake Superior upwind 0.40 m vs downwind 3.04 m, flips with wind dir |
| nearshore_wave_refraction_shoaling [L] + nearshore_shoaling_breaking_benchmark [M] | shoaling | dip-rise-break Hs peak 1.997 m |
| wave_current_interaction [L] + wave_current_opposing_interaction [M] | wave_current | opposing amplifies to 4.097 m |
| bottom_friction_wave_dissipation [L] | bottom_friction | Hs OFF 1.612 vs ON 1.450 m (-10%) |

TELEMAC-3D (first `### TELEMAC-3D` block) -- 2 rows -> LANDED, 1 -> STOP-RECIPE:

- `thermal_stratification_lake_reservoir` -> LANDED. SAME question class as the
  COVERED `thermal_stratification_reservoir` row (ADR 0241): 
  `telemac3d_stratified_flow` mode=stratification. Calm KEEPS the thermocline vs
  a 12 m/s wind MIXES it in the shallow discriminant; persisting surface~18 C /
  bottom~16 C over real 320 m Lake Superior. WAQTEL THERMIC atmospheric
  heat-exchange (waqtel4telemac3d.so baked) is a documented future addition.
- `saline_density_intrusion_estuary` -> LANDED. SAME class as the COVERED
  `salinity_intrusion_estuary` row (ADR 0241): mode=salt_wedge, the classic
  lock-exchange gravity current (DENSITY LAW 2), front advances linearly at
  0.171 m/s (Fr 0.386) through the baked binary. LIMITATION (documented in
  0241): idealized lock-exchange only; a real TIDAL estuary needs a tidal LIQUID
  BOUNDARY in 3D - a documented follow-up (added machinery), not a re-open of the
  salt-wedge physics class.
- `vertical_culvert_recirculation_structure` -> STOP-RECIPE (see below).

## The one STOP: vertical_culvert_recirculation_structure

Not a binary-missing STOP (the dico supports culverts, verified above). It is a
HEAVY-NEW-MACHINERY STOP. Productionizing needs:

1. A NEW `write_culvert_data_file()` authoring path in `telemac3d_build.py` - a
   distinct structure-file format (paired endpoint nodes, invert elevations,
   section area, loss/valve coefficients) the current worker does not write, plus
   an `OPTION FOR CULVERTS` knob.
2. Un-fetchable site-specific structure ENGINEERING parameters (culvert invert
   levels, diameter, discharge coefficients). No fetcher serves these; they are
   user-supplied labeled-default inputs through the input-review gate, not a data
   layer.
3. A liquid-boundary-FORCED channel / reservoir-intake archetype (inflow/outflow
   through the structure), fundamentally distinct from
   `telemac3d_stratified_flow`'s closed-basin archetype (stratification and
   wind_circulation are closed-basin; salt_wedge is idealized lock-exchange - none
   carry through-flow liquid boundaries).

RECIPE if ever built: add the culvert-data-file author + OPTION FOR CULVERTS knob;
a forced channel-with-intake mesh archetype + liquid boundaries; a labeled-default
structure-parameter input block (input-review gate); bump parser
`telemac3d-strat-1` -> `-2`; rebuild + through-image smoke; discriminating pair =
the 3D vertical recirculation cell around the culvert vs the 2D depth-averaged
omission. The 3D-meshing prerequisite (the row's original blocker) is REMOVED by
ADR 0241; the structure-file + forced-channel machinery is the remaining lift.

## Consequences

- Board delta: 9 CAND -> LANDED (bookkeeping flip of already-shipped ADR
  0236/0241 physics), 1 CAND -> STOP-RECIPE. Rolling metric updated on the board
  TOTALS line.
- No coded-tools delta, no LOC in services/ or server/, no image rebuild, no
  parser bump, no new proof renders - the discriminating-pair proofs already
  persist (tomawac_wave_field_lake_superior_fetch_pair.png,
  telemac3d_stratified_flow_vertical_profiles.png,
  telemac3d_wind_driven_circulation_chart.png). This is a reconciliation wave:
  the board now reflects the shipped truth.
- HOLDS respected: no Ensembles/Monte-Carlo, no WAQTEL rows, no SnapWave, no
  existing STOPs touched. The WW3 spectral-boundary-data fetcher gap remains the
  already-flagged STOP for open-coast TOMAWAC (not re-adjudicated here).
