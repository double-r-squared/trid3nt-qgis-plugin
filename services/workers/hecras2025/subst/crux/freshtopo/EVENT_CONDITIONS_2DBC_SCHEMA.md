# The plan-HDF `/Event Conditions` 2D-BC-line schema (OI-FT1, decoded)

Our own decoding of the last forcing link in the ADR 0136/0137 chain: the
HEC-RAS 6.6 engine's `read_un_q2d_bc_` (Read_UN_Q2D_BC.for) reads each 2D-BC-line
flow hydrograph / normal depth from the plan HDF's `/Event Conditions/Unsteady/
Boundary Conditions`. This group -- NOT the `.bNN` fake-reach header, NOT the
geometry BC line -- is what directs moving water onto a 2D flow area. Authoring
it blind segfaults (ADR 0137); these are the exact schema facts so it can be
authored correctly and re-authored entirely by our own code (`hecras_event_
conditions.py`) against our own carved geometry.

Schema facts only (a file-format decode). Nothing is vendored: the decode was
read off shipped public-domain HEC example plan HDFs to learn the layout, then
our writer reproduces it from our own mesh.

## Group layout

```
/Event Conditions                               @Completed Successfully = "True"
                                                @Date Processed = "M/D/YYYY h:mm:ss AM"
  /Unsteady
    /Boundary Conditions
      /Flow Hydrographs
        "2D: <area> BCLine: <name>"   dataset (N, 2) float32   -- [time, flow] interleaved
      /Normal Depths
        "2D: <area> BCLine: <name>"   dataset (1,)  float32   -- friction slope
      /Precipitation Hydrographs                              -- (rain-on-grid; not needed for BC-line inflow)
        "2D: <area>"                  dataset (N, 2) float32
    /Initial Conditions                         @Startup Mode = "Computed"
```

The dataset NAME is the literal `2D: <2D-area-name> BCLine: <bc-line-name>`.

## Flow Hydrographs dataset

- data: `(N, 2) float32`, interleaved `[time_i, flow_i]`; time in the `Interval`
  units, flow in cfs.
- attributes:
  - `2D Flow Area`  (S)  -- the 2D area name (== geometry area name)
  - `BC Line`       (S)  -- the BC line name (== geometry BC-line name)
  - `Check TW Stage`(S)  -- `"False"`
  - `Data Type`     (S)  -- `"INST-VAL"`
  - `EG Slope For Distributing Flow` (f4) -- energy-grade slope used to spread the
    total line flow across the member faces (0.001 in the references)
  - `Start Date` / `End Date` (S) -- `"DDMonYYYY HHMM"` (e.g. `"01Jan1900 2400"`)
  - `Interval`      (S)  -- e.g. `"Days"`
  - `Node Index`    (i4) -- `1`
  - `Face Indexes`      (i4[k]) -- the member perimeter face indices (see below)
  - `Face Point Indexes`(i4[k+1]) -- the ordered facepoints spanning those faces
  - `Face Fraction`     (f4[k]) -- per-face fraction of the face covered by the line

## Normal Depths dataset

- data: `(1,) float32` -- the friction slope (e.g. `0.001`).
- attributes: as above minus the hydrograph-only ones (`Data Type`, `EG Slope`,
  `Start/End Date`, `Interval`), PLUS `BC Line WS = "Multiple"`.

## The face keying is DERIVED from geometry (the key insight)

A BC line's `Face Indexes` / `Face Point Indexes` / `Face Fraction` are NOT
independent data -- they are the geometry's `Geometry/Boundary Condition Lines/
External Faces` rows for that line, CLIPPED to the line's `[0, Length]` station
span (`Length` from `.../Attributes`):

- keep a face iff `[Station Start, Station End]` overlaps `[0, Length]`;
- `Face Fraction` = `(min(SEnd,Length) - max(SStart,0)) / (SEnd - SStart)`
  (partial at the ends where the drawn polyline starts/stops mid-face);
- `Face Point Indexes` = `[FP Start of first kept face] + [FP End of each kept face]`.

This derivation was verified to reproduce the shipped enumeration **byte-exact**
for every BC line (flow AND normal-depth) across multiple reference decks
(transcripts under `scratchpad/oift1_probe_proofs/verify_clip.py`). Our own
geometry writer lays the BC-line polyline exactly on the face endpoints, so every
`Face Fraction` is `1.0`; the clip is nonetheless implemented generally.

## Where the forcing VALUE lives (fake reach vs Event Conditions)

In a pure-2D deck the `.bNN` still carries `Upstream Flow Hydrograph - River:
Fake River  Reach: Fake Reach  RS: 100` + `Downstream Normal Depth`. That fake
reach is an **inert placeholder** satisfying the engine's >=1-reach requirement:
the reference decks hold a flat constant there while the REAL hydrograph lives in
the Event Conditions 2D-BC dataset. The engine wets the 2D area from Event
Conditions, not from the fake reach -- which is exactly why ADR 0137's fake-reach
deck (correct geometry + fake reach, empty Event Conditions) solved but stayed
DRY.
