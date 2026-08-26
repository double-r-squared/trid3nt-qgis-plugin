# ADR 0315 - The coastal split, the resolution label, and the context slot

Status: LANDED (TELEMAC workflows refactor, wave B - the engine-specific half).
Follows ADR 0314 (the static plan and the style contract), which is wave A.
Supersedes nothing.

## Context

Wave A made the six TELEMAC templates read as declarations. What it deliberately
left is what is TELEMAC about them, and four of those things were the same shape
of problem: a value or a source the code chose, presented as if the question had
asked for it.

- The coastal PEAK raster was per-node max WATER DEPTH over every frame including
  t=0, with no subtraction of the initial water line. The permanently submerged
  bay floor therefore rendered in the same "inundation depth" ramp as land the
  tide actually reached. On the canary run 44.8% of the wet raster was deeper
  than 2 m. `flooded_land_km2` had always done the right `bed > init_wl`
  discrimination, so the scalar and the picture disagreed and only the scalar was
  right.
- The coastal worker laid its grid LOCAL (node 0 at 0,0), computed the domain's
  UTM south-west corner two lines later, and threw it away. `res_coastal.slf`
  therefore carried metres-from-zero while `results_mesh_seam` published it as
  `EPSG:<utm>`, so the animated result mesh landed at the zone's false origin -
  roughly 1600 km off, near the equator - beside a peak COG that was correct.
- `artemis_harbor_agitation`'s deck writer called the Overpass API itself, from
  three hardcoded mirrors, whenever the caller named no breakwater. That is a
  step FETCHING, which the no-double-middleware law forbids (no cache, no
  declared ladder, no provenance, no typed refusal, and outside the F2 audit's
  denominator); and it is an OPINION - "if you did not say, I will go and find
  the real one" - inside the one tool whose entire question is whether a
  particular structure shelters anything. When Overpass came back empty the step
  meshed "a LABELED schematic breakwater" nobody had asked for.
- Six templates had answers that a coarse mesh reads WRONG, in a known direction,
  and said nothing about it. The 2026-08-25 refined-mesh pass measured the sizes:
  dye peak 6x low, flooded land 4x low, a crest artifact 2x high, upwind Hs -62%,
  Kd absolutes -30 to -50%, stratification dT -25%. Every one in the unsafe
  direction.

## Decision

### 1. The coastal inundation product SPLITS (NATE ruling b)

TWO layers, because one raster was answering two questions:

| layer | role | what it is |
| --- | --- | --- |
| `coastal_inundation.tif` | primary | peak depth over land that was DRY at t=0 |
| `coastal_depth_max.tif` | context | total peak water depth, permanent water included |

The primary is the planning quantity and uses the SAME discrimination
`flooded_land_km2` counts, so the picture and the scalar finally agree. The
context layer keeps the old filename because it is the old raster, honestly
renamed: "Total water depth at peak" is where the water is, rather than where the
tide went.

The t=0 mask has two routes, in preference order, and the run says which one ran
(`inundation_basis` on the layer and on the answer). First choice is the worker's
own rule reproduced from the result SELAFIN - `BOTTOM > init_wl`, the
DATUM-CORRECTED initial water line the run cold-started from. Fallback is frame 0
of WATER DEPTH, which is the same discrimination read off the field instead of off
the bed, for a result that carries no bed or reported no initial stage.

Every pre-existing scalar is UNCHANGED and was re-verified against the canary;
`inundation_peak_depth_m` and `inundation_basis` are added. The measured gap is
the argument for the split: on the Apalachicola canary the total-depth peak is
6.6471 m and the INUNDATION peak is 0.1517 m. The old raster's headline number
was six metres of permanent bay.

### 2. The resolution-sensitivity label is skeleton machinery (ruling b)

`workflows/lib/resolution.py` holds four MEASURED classes, each with the direction
a coarse mesh reads it: PEAK (a coarse element averages a peak away - reads low),
EXTENT (a wet/dry front lands between nodes - reads low), LOCATION (the feature
moves with the element that resolves it) and GRADIENT (the gradient is flattened
across the element - reads low). The converged classes - integrals, saturated
maxima, ratios - are NOT labeled, because labeling everything is the same as
labeling nothing.

A template declares which ANSWER fields sit in which class
(`sensitivity=(("flooded_land_km2", "extent"), ...)`); the skeleton's `checks()`
hook turns that into ONE note per run, because four fields on the same mesh is one
fact about the mesh. The resolution LEVER is read off the declared
`ResolutionSpec` rather than restated, so there is no second name for it.

WHAT THE LABEL IS CONDITIONED ON is the interesting part, and it is deliberately
not a threshold. Nobody has run the convergence study that would justify "coarse
below N metres", and inventing the number would be exactly the baked opinion this
campaign removes. It is conditioned on the run's own SHEET: whether the resolution
lever came through the USER door or was left at the template's labeled default. A
default-spacing run is the case the evidence above was measured on and reads
"RESOLUTION-LIMITED, TREAT AS A BOUND"; a run the user refined reads
"RESOLUTION-SENSITIVE" and says that refining is not a demonstrated convergence.
Both sentences are true, and neither needs a number nobody has earned.

The mechanism is engine-neutral. MODFLOW plume peaks are next, and they cost one
declaration line each.

### 3. The structure is a CONTEXT SLOT, and the fetch becomes a spec

`Data("structure").supplied(geometry="polyline").optional()`. Producer-less, so
the template names no default source for somebody's breakwater; it says what
SHAPE it accepts and stops. Three ways to fill it, all proved live: a LAYER
handle (typically from the new fetcher), a DRAWN or typed line, or nothing.
Absence is legal and LABELLED - the domain solves as open water and the run says
so on the layer, in provenance and in the journal.

`fetch_osm_breakwaters` lands as a standalone router spec beside
`fetch_roads_osm`, sharing the way-geometry hook family. It is deliberately NOT
bbox-clipped: a road is a network you measure inside an area, but a breakwater is
an object, and clipping one at the AOI edge opens a gap in the middle of a
structure that has none. `man_made=pier` stays excluded - a pier is the berthing
dock being sheltered, not a wave barrier.

DELETED: the buried Overpass call, its three hardcoded mirrors, the
FlatGeobuf-and-boto3 re-upload that existed only because that fetch bypassed
emit-on-fetch (a supplied layer is already on the canvas), and the pinned-segment
coercion the slot replaces.

The plan expresses the guard through the SLOT rather than through a literal
`When`. Demand-pulled producers mean an unfilled optional slot costs no fetch and
binds to `None`, so the barrier is meshed exactly when the slot is filled - and a
literal `When` could not have been written, because the only `Ref`-able body would
have had to contain the whole solve tail (a `When` body is a scope).

### 4. The coastal result mesh gets its origin, in the header

Fixed where it was lost. `build_coastal_mesh` keeps the south-west corner on the
mesh and `write_slf` passes it as `add_mesh(orig=...)`, which fills IPARAM(3)/(4).
TELEMAC copies those from the geometry into the results file
(`read_mesh_info.f` -> `write_mesh.f`) and MDAL honours them. Integer metres,
because the Fortran declares `X_ORIG` as an INTEGER.

Our own `read_selafin` ignores IPARAM, so every postprocessor reads exactly what
it read before: the numbers do not move, the header gains the fact. Verified on
the canary run - `IPARAM[2]=691577, IPARAM[3]=3286076`, local `0..11391` metres,
which reprojects to lon -85.02..-84.90, lat 29.69..29.80: Apalachicola Bay.

The same LOCAL-coordinates-with-a-zero-origin shape is present in three more legs
(telemac3d, tomawac, artemis). It is LATENT there rather than broken, because
none of them publishes a mesh layer and each postprocessor re-adds the origin
itself. Recorded, not fixed: it is the blocker under the 3D-rendering track and
belongs with the decision about which AOI templates ship mesh layers at all.

### 5. Sim-duration doors, per template

Reviewed all seven. Two flipped:

- `tomawac_wave_field` and `telemac3d_stratified_flow`: `sim_duration_hours`
  SCENARIO -> CONSTANT. Neither window is a choice about the scenario; both are
  "long enough" - long enough for the sea to reach its fetch-limited steady state,
  long enough for the column to settle. A shorter one reports an unconverged
  answer and a longer one reports the same one more slowly, which is a numerics
  fact, not a question. The user keeps the lever; the model is not offered it.
- `coastal_tidal_surge`: `duration_hours` keeps its USER door and its
  derived-from-the-series-span rule, and the UNDECLARED third rung dies. The step
  held a 30 h constant nobody had declared, reached whenever there was no ask and
  no series. It is now `SYNTHETIC_WINDOW_HOURS` beside the param whose
  `derived_when_absent` sentence promises it, and every run emits a
  `duration_hours` provenance row naming which of the three rungs set it.

Unchanged and verified correct: `telemac_do_sag` (`sim_duration_s` door=CONSTANT
- "time to reach the steady-state sag" is the same "long enough" shape),
`artemis_harbor_agitation` (no duration at all - an elliptic boundary-value solve
has no simulation clock), `telemac_river_dye` (SCENARIO is right: the window IS a
scenario choice about how long you watch a pulse). `telemac_rain_on_grid` is
pre-migration and rides the migration.

## Consequences

- A coastal run publishes FOUR layers where it published three, and its headline
  raster now shows 15 cm of flooding rather than 6.6 m of bay. That is a visible
  change to what the product means, and it is the point.
- The ARTEMIS canary no longer meshes a breakwater, because nothing supplies one.
  Its Kd numbers therefore MOVED, and that is correct rather than a regression:
  the run that fetched a structure was answering a question the caller had not
  asked. The sheltered/exposed pair is now what the canary's open-water domain
  actually produces, and the layer says so.
- Every engine gets the resolution label for the cost of one declaration line.
- The worker image was rebuilt once, with absolute `-f`/context paths, and its
  contents were provenance-checked in the image before any live run.

## What this does NOT do

The worker-purity audit enumerated far more than this wave migrated. The coastal
leg's unreachable knobs are now reachable (friction law and coefficient, wind
speed and direction were `CoastalConfig` fields the deck writer never filled), and
four narrate-on-adjust violations now echo. The remaining migrate-to-manifest
inventory - four different auto-spacing divisors, four grid floors duplicated
against the params' own bounds, the TOMAWAC spectral discretisation, the
TELEMAC-3D turbulence constants, the six hardcoded fetch endpoints - is recorded
in `docs/IDEAS.md` and rides the in-worker-fetch migration, which is the other
half of the same end state.
