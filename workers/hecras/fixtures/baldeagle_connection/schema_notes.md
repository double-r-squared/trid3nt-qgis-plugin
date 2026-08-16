# baldeagle_connection -- schema dump + VERIFY results (ADR 0171 front)

## The headline finding: real `Type="Connection"` structures, in hand, for
## the first time

Neither `g01.hdf` nor `g11.hdf` is synthetic or reverse-engineered -- both
are GUI-computed HEC-RAS geometry HDFs (root attr `File Type="HEC-RAS
Geometry"`) shipped as-is in HEC's own public example set. `Geometry/
Structures/Attributes` in each carries genuine `Type=b'Connection'` rows --
the exact structure class ADR 0171 found completely absent from the repo
(the only prior reference, Muncie, is `Type=b'Lateral'`).

### g01 -- FOUR connections: one Storage-Area-to-2D dam, three internal
### levee connections (single 2D Flow Area `BaldEagleCr` + storage area
### `Reservoir Pool`)

| idx | Groupname | US SA/2D | US Type | DS SA/2D | DS Type | Weir Width | Weir Coef | Shape | Use 2D for Overflow | Gate Groups |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 0 | Reservoir Pool to BaldEagleCr, Dam | Reservoir Pool | -- | BaldEagleCr | 2D | 100.0 | 3.82 | Ogee | **1** | 1 |
| 1 | BaldEagleCr, Lower Levee | BaldEagleCr | 2D | BaldEagleCr | 2D | 20.0 | 2.0 | Broad Crested | **1** | 0 |
| 2 | BaldEagleCr, Middle Levee | BaldEagleCr | 2D | BaldEagleCr | 2D | 20.0 | 2.0 | Broad Crested | **1** | 0 |
| 3 | BaldEagleCr, Upper Levee | BaldEagleCr | 2D | BaldEagleCr | 2D | 40.0 | 2.0 | Broad Crested | **1** | 0 |

`US Type="--"` on struct 0 correlates with its `US SA/2D` naming the project's
one Storage Area (`Reservoir Pool`, confirmed present in `Geometry/Storage
Areas` with an elevation-volume curve, 53 points) -- i.e. struct 0 IS the
SA-to-2D connection ADR 0171 row 1 needs (dam breach forcing water from a
lumped reservoir into the 2D floodplain mesh). Structs 1-3 are `2D`/`2D` on
BOTH sides but with `US SA/2D == DS SA/2D == "BaldEagleCr"` (the SAME single
2D Flow Area on both sides) -- these are INTERNAL levee/weir lines splitting
one mesh into leveed compartments, a genuine `Type=Connection` variant
distinct from a true two-mesh connection.

**`Use 2D for Overflow = 1` on every structure here** -- this is the row-4
unblock: Muncie's one shipped weir (ADR 0171 finding 4) has `Use 2D for
Overflow=0` and is provably inert (byte-identical A/B on a 4x coefficient
change). Every BaldEagle connection has overflow ROUTED into the 2D mesh,
so a `Weir Coef` A/B on this fixture is NOT pre-doomed to be inert the way
Muncie's was -- this is the fixture `weir_discharge_coefficient_tuning`
needs, once a compute path exists (see VERIFY below -- one does not yet).

### g11 -- ONE connection, TWO DISTINCT named 2D Flow Areas: the genuine
### 2D-mesh-to-2D-mesh case ADR 0171 called "the frontier"

`Geometry/2D Flow Areas` in `g11.hdf` independently enumerates **two**
areas: `BaldEagleCr` and `Upper 2D Area` (verified directly, not inferred
from the Attributes text). The one connection:

| idx | Groupname | US SA/2D | DS SA/2D | Weir Width | Weir Coef | Shape | Use 2D for Overflow | Gate Groups |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 0 | Upper 2D Area to BaldEagleCr, Dam | Upper 2D Area | BaldEagleCr | 25.0 | 3.82 | Ogee | 1 | 1 |

Nearly identical structure config to g01's struct 0 (same Ogee shape, same
weir coefficient 3.82, one gate group) -- this is the SAME logical "Dam"
connection modeled two ways: g01 with the upstream side as a lumped Storage
Area (plan title "SA to Detailed 2D Breach"), g11 with the upstream side as
a full 2D mesh (plan title "2D to 2D Run"). A clean, minimal, real-world
diff pair for SA-2D vs. 2D-2D connection schema differences -- both driven
by the identical downstream mesh (`BaldEagleCr`).

### The cell-face HW/TW pairing table: CONFIRMED not a static input dataset
### anywhere in this HDF (narrows, does not resolve, the ADR 0171 wall)

Searched exhaustively in both files: `Structures/*` carries no per-face
index table (unlike Muncie's LATERAL weir, which has `User Defined Weir
Connectivity` -- a STATION-based table; that dataset is absent here because
these are `Connection`-type, not `Lateral`-type, structures).
`2D Flow Areas/BaldEagleCr` carries the full mesh face geometry (`Faces
Cell Indexes`, `Faces FacePoint Indexes`, `Faces NormalUnitVector and
Length`, `FacePoints Coordinate`, ...) but nothing that names WHICH faces
belong to a given `Structures` centerline. So the pairing is either (a)
computed at preprocess time by intersecting `Structures/Centerline Points`
against the mesh face geometry (both of which ARE present and real), or
(b) computed at solve time and never persisted as a named HDF dataset. This
job could not distinguish (a) from (b) empirically -- see VERIFY below; the
wall is now BETTER characterized (the mesh geometry to intersect against is
real and in hand) but not removed.

### Other structure sub-tables present (real, GUI-computed)

- `Centerline Info/Parts/Points`: g01's dam+levee centerlines, 328 points
  total; g11's single dam centerline, 18 points.
- `Profile Data` (crest station-elevation): g01 325 rows (4 structures'
  crests concatenated per `Table Info` index/count), g11 6 rows.
- `Gate Groups` (both files): `Attributes` + `Openings` sub-datasets -- a
  real GATE schema (the dam connections both carry 1 gate group), a
  reference this repo also lacked before (Muncie's weir has `Gate
  Groups=0`).
- `Bridge Coefficient Attributes`, `Pier Attributes`/`Pier Data`: present
  but empty/unused on these connection-type structures (bridge fields ride
  along in the shared `Attributes` compound regardless of structure type).

## VERIFY: RasGeomPreprocess against `trid3nt-local/hecras:latest`

Same wall as the steady fixtures, reproduced on BOTH kept geometries:

```
$ RasGeomPreprocess BaldEagleDamBrk.g01.hdf g01
$ RasGeomPreprocess BaldEagleDamBrk.g11.hdf g11

forrtl: severe (29): file not found, unit 5, file /data/io.x
  htabopen_ (Htabopen.for:107) <- MAIN__ (Htab.for:33)
```

Root cause identical to `beaver_creek_steady`/`critical_creek_steady`:
`g01.hdf`/`g11.hdf` are `File Type="HEC-RAS Geometry"` (geometry-only), not
`File Type="HEC-RAS Results"` (plan-level) -- `RasGeomPreprocess`'s modern
CLI does not recognize them and falls into the legacy `io.x` path. This
project ships NO plan-level HDF for any plan (`p01.hdf`, `p02.hdf`,
`p18.hdf` do not exist in the source zip) -- confirmed by directory listing
before extraction, not just by this probe.

**No RasGeomPreprocess run, therefore no in-container proof of whether the
cell-face pairing is Linux-computable.** This is the SAME missing-plan-HDF
gap ADR 0170 hit, now confirmed on a THIRD independent example project
(steady 1D, and now 2D connections) -- it is a general property of HEC's
public example distribution (which ships GUI geometry, not GUI-computed
plans), not a Muncie-specific or steady-specific quirk.

## What this unblocks for ADR 0171

- Row 1 (SA/2D connection authoring) + row 4 (weir-coefficient tuning): a
  real, solver-shaped `Type="Connection"` HDF schema is now in hand to
  diff against, INCLUDING a variant with `Use 2D for Overflow=1` (so a
  future headless authoring attempt has a non-inert reference, unlike
  Muncie). `hecras_geometry_writer.write_sa2d_connection(...)` (the ADR
  0171 recipe) can now target a concrete `Attributes` field layout instead
  of a blind guess.
- Row 2 (breach on a fresh connection): p01/p02 carry REAL, GUI-authored
  breach parameter blocks -- but in the **PLAN TEXT FILE** (`BaldEagleDamBrk.
  p01`), not the unsteady/boundary file. Three breach locations are defined
  (`Dam`, `Upper Levee`, and a station on the `Bald Eagle Cr.` 1D reach),
  each a `Breach Loc=` / `Breach Method=` / `Breach Geom=` / `Breach Start=`
  / `Breach Progression=` / `Simplified Physical Breach Downcutting=` /
  `...Widening=` / `Breach Use User Defined Growth Ratio=` block, e.g. for
  the Dam: `Breach Geom=5700,200,595,0.5,0.5,True,0.5,630,3.2,2.6` (invert
  elev/width/etc.), `Breach Start=True,676,...` (trigger time). This is a
  STRUCTURALLY DIFFERENT authoring surface than the existing
  `deck_edit.set_breach_enabled` (which edits Muncie's `.bNN` BOUNDARY file
  for a LATERAL structure's breach) -- a fresh-connection breach editor
  would need to target the PLAN file's `Breach *` block set instead. Noted
  for whoever builds the row-2 authoring front; not implemented here.
- Row 3 (pump): NOT served by this fixture (no pump group in either
  geometry) -- see `pump_station/`.
- The cell-face pairing frontier: NARROWED (the mesh face geometry to
  intersect against is now a real, in-hand reference) but NOT crossed --
  still gated on the same missing-plan-HDF problem as the steady front.
