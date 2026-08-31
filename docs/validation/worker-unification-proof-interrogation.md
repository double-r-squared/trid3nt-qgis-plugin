# Worker-unification proof interrogation

Ten live runs, ten mechanical packets, one adversarial pass over every render
before any of it is handed on. Packets live under the stage scratchpad
(`.../scratchpad/stage6/packets/<template>/coarse/`); `docs/proof/` is frozen and
untouched.

## What was proven live

Every run is on `code_sha c5591c2c` (HEAD), through the TELEMAC image, and every
render postdates the run it claims to show.

| family | packet dir | run | frames | correct end | packet |
|---|---|---|---|---|---|
| river_dye tracer | `telemac_river_dye` | 01M1CGTRE1MV0QQBWX7MH1XYFQ | 6 | yes | PASS |
| do_sag (WAQTEL) | `telemac_do_sag` | 01M1CH3NFCXA8Y3NVF52ZGG7GE | 7 | yes | PASS |
| river_dye OIL (user fortran) | `telemac_river_dye_oil` | 01M1CH6J14DWTYHGV8KHY4Y7XW | 6 | yes | PASS |
| sediment / GAIA (coupled) | `telemac_river_dye_sediment` | 01M1CHA7HV1EZV9ZGMZNE2HXX3 | 6 | yes | PASS |
| rain_on_grid (design storm) | `telemac_rain_on_grid` | 01M1CHEWX0BF2V01SRYMCKWZR9 | 13 | yes | PASS |
| agitation (ARTEMIS) | `artemis_harbor_agitation` | 01M1CHJPTNH0036D21QRW6HR3V | 1 (exempt) | yes | PASS |
| stratified (TELEMAC3D) | `telemac3d_stratified_flow` | 01M1CHMKD12BEGBFRM976DZ0GB | 11 | yes | PASS |
| split A straight | `split_run_A_straight` | 01M1CHVSR933PSNHAXS8BF6GM4 | 12 | yes | PASS |
| split B1 first half | `split_run_B1_first_half` | 01M1CHRC9FJR5JXNF26GGGMY8T | 6 | yes | PASS |
| split B2 continued | `split_run_B2_continued` | 01M1CHZ6RK5JEFN5GD2PA9B2DS | 6 | yes | PASS |

Every GIF: all frames distinct, the field moves, the legend crop byte-identical
across frames. Every engine still and animation carries the mesh wireframe.

Suite, unchanged by this stage: `tests/test_[s-z]*.py` 1397 passed, 6 skipped;
`contracts/tests` 789 passed.

## Reentrancy, drawn

`split_run_sidebyside.png`: A straight-through, B1's handover state, B2 continued
from B1's restart, and `|A - B2|` on one row with the numbers on the panel.

* RE-ENTRY (B1 restart record vs B2's first results record at t = 600.2 s):
  worst field `DYE` max|d| 1.120e-07 against a 3.603 mg/L span, rel 3.1e-08.
* CLOSURE (A restart record vs B2 restart record at t = 1200.384 s): **exactly
  0.0 on every variable** - velocity U/V, depth, bottom friction, increment of H,
  dye. A's and B2's dye maxima agree to six figures (0.73382 mg/L).

The continuation is bit-exact. The claim holds.

## Findings

Filed, not re-rendered away. Numbered for reference; each one names the
measurement that produced it.

### 1. The unified metrics dropped `result_slf`, and the packet assembler went blind

`completion.json` for every case-family run (reach, do_sag, oil, sediment, rog)
carries no `result_slf` and no `ntimestep` - the unified worker metrics is
`{status, correct_end, run_id, module, family, wall_s, **echo}` and neither key
is in the echo. `scripts/assemble_proof_packet.py` reads
`completion["result_slf"]` to measure frames and to render the declared
animation, so on the first attempt every case-family packet REFUSED with "the
time-stepped decision is UNMEASURABLE". The two legacy in-worker builders
(`artemis_build.py`, `telemac3d_build.py`) still emit it, so agitation and
stratified were unaffected - the loss is scoped exactly to the families the wave
rewrote. The plan's verification line ("completion.json consumer keys unchanged")
listed `solve.py`, diagnostics and the products readers; the packet assembler is
a consumer that was not on the list.

The packets above were produced with a SCRATCHPAD-ONLY shim in the drive script
(`drive_house_proofs.py::_shim_result_slf`) that recovers the name from the
completion's own `output_uris`. No product code was touched.

DESIGN-STOP: restoring `result_slf` is a choice of channel - worker echo, a
server-known manifest fact, or the assembler deriving it from `output_uris` - and
whichever wins decides whether `ntimestep` comes back with it.

### 2. `output_interval_min` is accepted and silently ignored on the reach path

Declared on both `river_dye` and `do_sag` (`Param("output_interval_min", ...,
bounds=(0.1, 1440.0))`), threaded into `PHYSICS`, carried into the deck dict at
`steps/deck.py:658` - and never converted to a step count. The author's
`_DEFAULTS["graphic_period"] = 200` is what reaches the deck. Measured: a run
asking 0.333 min (20 s) got `GRAPHIC PRINTOUT PERIOD = 200` = 104.2 s frames, six
frames over a 600 s window. `steps/rain_on_grid.py:471` does the minutes-to-steps
conversion; the reach seam has no equivalent. The two refined canaries
(`telemac_do_sag_refined`, `telemac_river_dye_refined`) ask for 30 frames on this
parameter and have been getting 6.

DESIGN-STOP: the conversion's home (deck vs author) and whether the parameter
survives at all are both design calls.

### 3. do_sag: the BOD never enters, and the "sag" is the inflow boundary

Measured on run 01M1CH3NFCXA8Y3NVF52ZGG7GE:

* `ORGANIC LOAD` (the CBOD tracer) is identically **0.0 over the whole domain at
  every frame**, and the published chart draws CBOD as a flat zero line across
  the whole reach. The declared `discharge_bod_mgl = 20.0` never reaches the
  water.
* `DISSOLVED O2` at the final frame is 0.000 at **exactly the ten nodes of the
  inflow boundary group** (LIHBOR=4, the prescribed-flowrate end; centroid
  406848.8, 4482931.3) and 9.022 at all eleven outflow-boundary nodes. The
  low-oxygen region IS the inflow boundary, not a plume.
* None of the low-oxygen nodes are dry (depth 0.71-0.73 m there), so this is not
  a wetting artifact - it is an imposed boundary value.
* Consequently `do_min_mgl = 0.2534`, `do_violates_standard = true` and the whole
  sag curve are artifacts. A Streeter-Phelps sag has its minimum DOWNSTREAM of
  the outfall and recovers; this curve's minimum is at distance 0 and only
  recovers.
* Independently: k1 = 0.3/day over a 600 s window can deplete ~0.2 percent of the
  BOD. A DO sag is not observable at this canary's duration even with the
  boundary fixed.

The WAQTEL steering file itself is correct (k1, k2, Cs, temperature all as
declared). The failure is in the T2D tracer boundary block
(`PRESCRIBED TRACERS VALUES = 0.0;0.0;0.0;0.0;0.0;9.022;20;0.0`) and how TELEMAC
maps those eight slots onto two boundaries and four tracers.

DESIGN-STOP: the slot ordering convention (boundary-major vs tracer-major), where
the outfall load should enter (boundary vs point source - the deck authors no
`SOURCES FILE` for do_sag at all), and the canary's window are three separate
semantic calls.

### 4. `spill_fraction` runs backwards along the reach

Declared "0=upstream..1=downstream". Measured on the dye run with
`spill_fraction = 0.25`: the derived release sits **915.6 m from the inflow
boundary and 330.8 m from the outflow** on a 1204 m reach - about 76 percent
downstream. `LineString(centerline_utm).interpolate(0.25, normalized=True)` is
walking a centerline whose vertex order runs downstream-to-upstream, which the
deck's own header corroborates ("Measured liquid-boundary order: ['outflow',
'inflow']" - the contour walk meets the outflow first). Consequence: the plume
has a quarter of the reach to travel instead of three quarters, and
`plume_reach_m = 237.3` is bounded by the domain rather than by the physics.

The stage-5 remedy did land - the release is now inside the meshed domain by
construction, where it used to be 350 m outside. Only its orientation is wrong.

DESIGN-STOP: fixing this means declaring which end of the acquired centerline is
upstream and enforcing it, which is a convention the whole reach chain reads.

### 5. The reach mesh is not on the water

`telemac_river_dye` panel 6 (the published mesh layer over ESRI World Imagery):
the meshed domain is a strip lying on the gravel bar and forested terrace
SOUTH-EAST of the Eel's visible wetted channel. The open water in the imagery is
almost entirely outside the modelled domain; only the south-west tip touches it.
Every reach family in this wave (dye, oil, sediment, do_sag, and all three split
legs) solves that same domain. The mesh follows the NHD centerline / NHDArea
polygon, and on a braided, migrating gravel-bed river those disagree with current
imagery by a channel width.

The oil slick makes the same point in a second way: the three slick polygons in
panel 7 are drawn over forest and bar, not water.

DESIGN-STOP: whether the reach substrate should be conditioned against a wetted-
channel source, or whether the canary reach should move, is a substrate judgment.

### 6. The canvas view is basin-scale; the answer is a dot

`telemac_river_dye` panel 8: the canvas view spans roughly 80 km of the lower Eel
basin because the published CONTEXT layers (OSM waterways, NHDPlus NLDI, NHD area
water - 23 features, 6808 vertices) are basin-extent, while the model is 1 km.
The per-layer panels say it outright: "FRAMED ON THIS LAYER (4195x closer than
the canvas extent)". A user opening this case sees the basin and cannot see the
result without zooming four thousand times.

DESIGN-STOP: this is the layer-extent policy for context inputs, not a render
defect.

### 7. Substance is a label, not a physics fork, on two of the three legs

Discrimination test, dye vs oil vs sediment at the final frame on identical
meshes:

* dye vs OIL: velocity U, velocity V, depth, free surface and bottom are
  **bit-identical** (max|d| = 0). Only the tracer differs, by 0.137 mg/L against
  a 4.92 mg/L span (2.8 percent). The oil leg's distinct products (drogues,
  particles, slick) are real; its FIELD is 97 percent the dye run's.
* dye vs SEDIMENT: every variable differs, bottom by 0.078 m - the GAIA coupling
  feeds back into the hydrodynamics. Good discrimination.
* All three decks author `NAMES OF TRACERS = 'DYE MG/L'` and all three publish
  the same `telemac_dye_peak.tif` under three different names ("Peak dye / oil /
  sediment concentration") with the same `continuous_dye_concentration` style
  row. `proof_animations` declares one animation for the tool, painting `DYE`,
  for all three.

Nothing here is wrong physics - a passive tracer is a passive tracer - but the
NAME on the product asserts more than the field carries.

DESIGN-STOP: whether the substance legs deserve their own quantity rows and
animation declarations is a declaration-surface call.

### 8. Two published results are declared `role="input"`

`Oil slick track` and `Bed evolution / scour` both carry `role: "input"` in the
emitted layer record; they are results. The oil slick additionally carries
`style_preset: "nhdplus_flowlines"` - a river-line preset on a slick polygon.

DESIGN-STOP: the correct role and the correct style row are declaration choices.

### 9. sediment: the deposition scalar contradicts its own field

`CUMUL BED EVOL` spans -0.047 to +0.078 m and the metrics report
`max_deposition_mm = 78.35` / `max_scour_mm = 47.05`, which agree. But the same
metrics report `deposited_mass_kg = -0.0` and `deposit_fraction = -0.0`. A field
with 78 mm of deposition and zero deposited mass is one of the two numbers being
wrong. The evolution is also concentrated in a single blob at the inflow end with
the rest of the reach at zero, which reads as boundary-driven rather than as
transport.

DESIGN-STOP: which of the two the reader should believe is a semantics call.

### 10. rain_on_grid: the peak is the truncation, and the headline depth is a DEM pit

* The outlet hydrograph rises monotonically to the last sample. `peak_discharge_m3s
  = 75.49` at `peak_discharge_time_s = 7200.0` - the final instant. The storm
  ended at 1 h; the catchment's response has not peaked by 2 h. `runoff_volume_m3`
  and `runoff_coefficient = 0.045` are integrals over a window that closes before
  the answer does.
* `max_depth_peak_m = 11.3186` is a single node whose bed (933.3 m) sits 10 to 35
  m BELOW its immediate neighbours (944-970 m) - an unfilled DEM pit ponding to
  its rim. Peak-depth median over the catchment is 0.113 m, p95 0.83 m, and only
  176 of 4998 nodes exceed 1 m. The published "Max water depth" raster is scaled
  by the pit.
* The packet note for `(telemac_rain_on_grid, coarse)` says "the depths are
  millimetre-scale"; this run's are centimetre-to-decimetre. The note describes a
  different storm than the one that ran.
* Legibility: the declared `depth > 0.0` mask punches star-shaped holes through
  the field wherever a node generated no runoff at all (zoom crop
  `view/ZOOM_rog_channel.png`). "No runoff here" renders as "no data", which
  reads as a rendering defect rather than as a result.

DESIGN-STOP: the window, the bed conditioning, and the mask floor are three
separate physics/declaration choices.

### 11. ARTEMIS: the "harbour agitation" canary models no harbour

The run's own metrics are honest - `structure_note`: "NO structure was supplied,
and the solve confirms it meshed none: the domain was solved as OPEN WATER and
every Kd here is the unsheltered response." The render agrees: the mesh is an
offshore rectangle whose landward edge stops short of Marquette Lower Harbor, the
marina and breakwater sit outside it, and the field is an open-water shoaling and
interference pattern (Kd max 2.764, Hs max 5.53 m from a 2.0 m incident wave).
But `kd_sheltered = 0.098`, `kd_exposed = 0.524` and the family's
`sheltering_ratio` vocabulary are still reported over a domain with nothing to
shelter, and the canary is named for a question it does not ask.

Minor, same run: the still is captioned "PEAK FRAME t = 8 s" while the animation
declaration exempts ARTEMIS precisely because it "has no simulation clock at all"
- the caption is reading the wave period as a time.

DESIGN-STOP: renaming the canary, or giving it the breakwater the question
implies, is NATE's call.

### 12. Coupled runs are not reentrant, by construction

`steps/deck.py` authors `restart=None if coupled_with else _RESTART`, so the
WAQTEL (do_sag) and GAIA (sediment) legs write no `restart_river.slf` and cannot
be continued. Verified in the run objects: the dye and oil runs carry one, the
sediment run does not. This is a deliberate, visible code decision rather than a
regression, but "reentrant by default" now holds for the pure-t2d classes only,
and the wave close should say so where the ruling is recorded.

### 13. TELEMAC3D: no basemap, and the domain includes land

The stratified still renders over a black background - the ESRI imagery did not
come through at this extent, so the framing cannot be checked against anything.
The structured grid covers the whole declared bbox including the shoreline
visible in the one green sliver at the lower left, i.e. land cells are solved as
water. Surface temperature ends at 23.6 degC against a declared warm layer of
25.0 with zero wind and no heat flux.

## Per-family verdict

| family | packet | physics as declared | verdict |
|---|---|---|---|
| river_dye tracer | PASS | plume advects (peak moves 275 m in 417 s, mean wet speed 0.42 m/s) and dilutes 37.3 -> 4.9 mg/L | proven, with findings 2/4/5/6 |
| river_dye OIL | PASS | user fortran + oil spill steering ran; drogues, particles, slick published | proven as plumbing; field 97 pct the tracer run's (finding 7) |
| sediment / GAIA | PASS | coupling is real - bed moves, hydrodynamics respond | proven as coupling; finding 9 on the scalar |
| do_sag WAQTEL | PASS | launcher-deviation path ran, WAQTEL tracers present | plumbing proven, PHYSICS NOT PROVEN (finding 3) |
| rain_on_grid | PASS | delineate -> mesh -> infiltrate -> solve -> hydrograph chain intact, drainage network legible | proven as mechanism (finding 10) |
| agitation | PASS | ARTEMIS solved, one steady field, exemption declared | proven as solved; not the question its name asks (finding 11) |
| stratified | PASS | 13 planes, thermocline persists under calm | proven, framing unverifiable (finding 13) |
| split-run pair | PASS x3 | closure exactly 0.0 on every variable | proven, cleanly |

## What was checked and found clean

* Frame counts measured off each run's own SELAFIN, cross-checked against the
  GIF: all ten agree.
* Legend stability across every animation frame: byte-identical, no per-frame
  rescale anywhere.
* Panel counts: published layers + canvas view, exact on all ten packets; no
  second panel generation in any folder.
* Animation extent vs the run's AOI: every animation overlaps its run's bbox; no
  frames at the UTM false origin.
* Run-vs-code freshness: all ten on `c5591c2c`, all renders newer than their
  runs, no deliverable older than the evidence beside it. The recorded
  `code_dirty = true` is `docs/IDEAS.md` and nothing else.
* Boundary role conformance: the measured-numliq deck puts the flowrate on the
  inflow and the elevation on the outflow on every reach run
  (`PRESCRIBED FLOWRATES = 0.0;2.2` with the measured order `['outflow',
  'inflow']`), and the `.cli` codes match (LIHBOR 4/5 split, one contiguous run
  each).
* Wireframe: present on every engine still and every animation frame.
