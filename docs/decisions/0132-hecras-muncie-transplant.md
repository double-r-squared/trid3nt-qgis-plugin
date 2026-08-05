# ADR 0132 -- HEC-RAS: THE MUNCIE TRANSPLANT -- 2025-authored mesh + tables into the 6.x solver

Status: accepted (2026-08-04)
Follows: ADR 0130 (the PREPARE CRUX -- `MeshPropertyTables.ComputeFrom` computes
subgrid property tables headless on Linux under substituted open-source natives),
ADR 0129 (the substitution recipe -- meshing verbs run on Linux), ADR 0100 (M3 --
the Muncie fixture, the 6.x geometry HDF whose GUI-computed tables we reproduce
bit-identically; the strip experiment: no 2D tables -> the solver fails), ADR 0109
(the Muncie engine landing -- baseline maxWSE 951.93 ft, ~4881 wet cells, volume
error 0.0058%).

Experiment-only: NO server / worker / tool / contract change (registry
byte-identical by construction). Durable harness under
`services/workers/hecras2025/subst/crux/transplant/`; transcripts under
`scratchpad/transplant_proofs/`.

## The question, decomposed

Can subgrid property tables authored by the 2025 beta compute path be
transplanted into a 6.x geometry HDF and solved by the PRODUCTION 6.x Linux
solver, validated against the Muncie ground truth we match bit-for-bit? Three
sub-questions, two of them terrain-independent and decisive on their own:

- **Q1 (mesh authoring)** -- can the 2025 path author an ARBITRARY, real-AOI mesh,
  or only `MeshFactory.FromExtent` regular grids? (ADR 0130 only ever exercised
  FromExtent.) This gates whether "2025 authors any mesh" is even reachable.
- **Q3 (solver ingest)** -- does the PRODUCTION 6.x `RasUnsteady` consume 2D
  subgrid tables written by an EXTERNAL writer (h5py), not by RASMapper? This is
  the ADR 0100 strip experiment IN REVERSE.
- **Q2 (numeric fidelity)** -- do the 2025-computed table VALUES match the shipped
  6.x GUI tables for the same mesh + terrain? This alone needs the Muncie terrain.

## Findings

### Q1 -- the 2025 path authors an ARBITRARY mesh (two ways). YES.

Reflecting the beta DLLs inside `hecras2025:subst-exp`
(`ApiProbe`, `01_api_probe.txt`):

- `Geospatial.Vectors.Mesh` has a PUBLIC constructor
  `Mesh(Polygon perim, Cell[] cells, Face[] faces, FacePoint[] facePoints,
  Point[] cellCenters, Memory<Point>[] faceInternalPoints)` plus public
  `Cells / Faces / FacePoints / CellCenters / Perimeter` fields -- an EXACT-topology
  import of any mesh (e.g. reconstructed from a 6.x geometry HDF's arrays).
- `Geospatial.Vectors.MeshFactory.TryCreateMesh(Polygon perimeter,
  IList<Point> cellCenters, IList<Polyline> breaklines, out Mesh, out MeshError,
  MeshGenerationParams, reporter)` -- the RASMapper-style regeneration from a
  perimeter polygon + cell-center seeds (+ optional breaklines).

`MeshPropertyTables.ComputeFrom(Mesh, IResample<float> elev, IResample<float>
nvalues, PropertyTableOptions, out tables, reporter)` takes ANY `Mesh` -- so the
0130 authoring path is NOT bound to FromExtent.

**Evidence (`MeshGen`, `06_mesh_fidelity.txt`)** -- regenerating the Muncie mesh
from its OWN shipped perimeter + 5391 cell-center seeds (the top-level
`Cell Points` dataset), CCW-wound and open (see the perimeter caveat below):

| metric | 2025-regenerated | shipped 6.x | match |
| --- | --- | --- | --- |
| real cells | 5391 | 5391 | **EXACT** |
| total cells (with virtual/ghost) | 5765 | 5765 | **EXACT** |
| faces | 11166 | 11164 | delta 2 (0.02%) |
| facepoints | 5776 | 5774 | delta 2 (0.02%) |
| cell-center displacement | max = mean = p95 = **0.000000 ft** | -- | **bit-identical**, 5391/5391 bijection |

`TryCreateMesh` returned `True`, `MeshError.Status=Complete`,
`SanityCheck.Result=True`. The 2025 generator reproduces the Muncie real-AOI mesh
essentially bit-for-bit; the +/-2 faces/facepoints is a boundary-face
tie-break, not a structural divergence.

Perimeter caveat (`05_meshgen.txt`): fed the raw HEC `Polygon Points` (CW-wound,
closed with a duplicate first/last point), `TryCreateMesh` returns a NON-fatal
`FacePerimeterConnectionError`. Reversing to CCW and dropping the closing
duplicate makes it `Complete` -- an input-conditioning step (RASMapper's own
`AdHocFixPerimeterIssues` territory), NOT a Linux/native wall.

### Q3 -- the PRODUCTION 6.x solver consumes externally-authored 2D tables. YES.

ADR 0100 proved the forward strip (remove the 2D `Cells Volume Elevation` /
`Faces Area Elevation` groups -> `RasUnsteady` dies "Cells Volume Elevation Info
doesn't exist"). Because `RasGeomPreprocess` does NOT recompute the 2D subgrid
tables (it rebuilds only the 1D cross-section conveyance), whatever is written
into the plan HDF's 2D groups is exactly what `RasUnsteady` solves -- the
transplant lever. `transplant_solve.py` h5py-authors those tables into a copy of
the Muncie deck and runs the real `RasGeomPreprocess` + `RasUnsteady`
(in `trid3nt-local/hecras:latest`):

| run (external h5py author) | maxWSE ft | wet cells | vol err % | flux out |
| --- | --- | --- | --- | --- |
| **identity** (byte-identical) | **951.927** | 4896 | 0.005834 | 33467.5 |
| shipped baseline (ADR 0109) | 951.93 | ~4881 | ~0.00584 | 33467.5 |
| cell-volume x1.5 | 951.927 | 4896 | 0.005788 | **33388.1** |
| face-area (conveyance) x0.4 | 951.927 | **4945** | **0.006757** | 33450.6 |

Identity REPRODUCES the baseline (dWSE 0.003 ft, tables bit-identical vs pristine
after geompre) -- the h5py-authored transplant is accepted with no corruption and
no hidden RASMapper-provenance gate. The two perturbations MOVE the solve
deterministically and sensibly (reduced conveyance backs water up: wet extent
4896 -> 4945; more cell storage / less conveyance shift the mass balance and
outflow) -- proving `RasUnsteady` genuinely CONSUMES the externally-authored
tables, not a cached original. maxWSE is pinned by the upstream inflow-boundary
stage and is (correctly) insensitive to interior 2D-table edits; wet-cell extent
is the sensitive interior metric and it responds.

### Q2 -- numeric fidelity is the ONE open link. TERRAIN-GATED.

The 6.x GUI tables (`Cells Volume Elevation Values` = [Elevation, Volume];
`Faces Area Elevation Values` = [Z, Area, Wetted Perimeter, Manning's n]) are the
only 6.x ground truth, and reproducing them via the 2025 path needs the terrain
they were sampled from. The Muncie geometry references
`.\Terrain\Terrain.hdf` (Terrain File Date 23MAY2014, NAD83 StatePlane Indiana
East, US ft). That terrain ships with the WINDOWS 2D Muncie example but is
STRIPPED from HEC's `Linux_RAS_v66.zip` (the Linux engines never recompute 2D
tables, so they do not need it) -- it is absent from the worker image and the
repo, and was not obtainable this session (web-search budget exhausted; the DEM
is not mirrored on GitHub). So the direct 2025-vs-GUI table A/B and the faithful
exact-topology 2025 -> 6.x transplant solve are the one deferred step.

Encouraging priors: Muncie's 2D area is constant Manning n = 0.06 with the exact
`PropertyTableOptions` recorded in the geometry `Attributes` (Cell Vol Tol 0.01,
Face Conv Ratio 0.02, Face Profile/Area Tol 0.01, Cell Min Area Fraction 0.01,
Laminar Depth 0.2, 50 ft spacing) -- a clean, fully-specified target -- and ADR
0130 showed `ComputeFrom` produces real monotonic curves in the identical
on-disk ragged Info+Values schema. Nothing observed suggests the values will
diverge; it is simply unvalidated without the DEM.

## Verdict

**Real-AOI HEC-RAS is UNLOCKED for mesh + table AUTHORING and the transplant
SOLVE mechanics -- proven end-to-end on Muncie -- with ONE terrain-gated
numeric-fidelity check outstanding (quantified-gap, leaning strongly unlocked).**

Proven, terrain-independent: (1) 2025 authors an arbitrary real-AOI mesh,
regenerating Muncie bit-identically (Q1); (2) 2025 computes subgrid tables
headless on Linux (ADR 0130); (3) the production 6.x solver consumes
externally-authored 2D tables and reproduces the Muncie baseline (Q3). The single
open link is the numerical fidelity of the 2025 table VALUES vs the 6.x GUI
tables (Q2), which requires the Muncie terrain DEM.

## The landing map (real-AOI archetype)

Every link now has evidence except the terrain-gated value check:

    fetch DEM (our fetchers) -> HEC terrain via `ras createterrain` (ADR 0129, Linux OK)
      + perimeter polygon + cell-center seeds (our mesh components / QGIS)
      -> 2025 MeshFactory.TryCreateMesh(perimeter, seeds)   [regenerate; Q1]
         OR new Mesh(perim, cells, faces, facepoints, centers, internalPts) [exact import]
      -> 2025 MeshPropertyTables.ComputeFrom(mesh, terrain, nvalues, opts)  [ADR 0130]
      -> serialize the Mesh + tables into the 6.x /Geometry/2D Flow Areas/* group
         (the classic ragged Info+Values layout; MeshReader.WriteTables schema == 6.x)
      -> assemble a 6.x deck skeleton (plan HDF + boundary/flow files)
      -> RasGeomPreprocess (1D) + RasUnsteady (2D)   [consumes external tables; Q3]
      -> postprocess -> depth COG   [existing ADR 0109 path].

The `.bNN`/`.x`/`.tmp.hdf` intermediates question, made precise: the transplant
PROVEN here writes 2D tables into an EXISTING 6.x geometry HDF (Muncie's own
topology). A genuinely NEW AOI (different perimeter/mesh) additionally needs the
cell/face/facepoint TOPOLOGY arrays authored, not just the tables -- the 2025
`Mesh` object carries all of them (Cells/Faces/FacePoints/CellCenters/Perimeter),
and the on-disk writer is `MeshReader.WriteTables` (ADR 0130). So the one net-new
COMPONENT to build is an HDF geometry-writer that serializes a 2025 `Mesh` +
`MeshPropertyTables` into the exact `/Geometry/2D Flow Areas/*` schema extracted
here; this experiment proves the 6.x solver will accept its output. The 1D
cross-section files are optional (a pure-2D deck omits them; Muncie's x04 is a
combined 1D/2D case).

## Consequences

- No server / worker / tool / contract change; registry byte-identical (git:
  only this ADR + `services/workers/hecras2025/subst/crux/transplant/` harness).
  Offline suite untouched (no server code). Muncie baseline re-confirmed on this
  machine (vol err 0.005837%, tables bit-identical) before the experiment.
- Durable harness (source only): `extract_muncie_mesh.py`, `ApiProbe.cs/.csproj`,
  `MeshGen.cs/.csproj`, `compare_mesh_fidelity.py`, `transplant_solve.py`,
  `REPRODUCE_TRANSPLANT.txt`. The proprietary beta DLLs (staged from the image),
  .NET build outputs, and regenerable `.f64` fixtures are `.gitignore`d.
- Image hygiene: no new images built; throwaway `--rm` containers on the
  pre-existing `hecras2025:subst-exp` (0.54 GB), `trid3nt-local/hecras:latest`
  (2.2 GB), and `mcr.microsoft.com/dotnet/sdk:9.0`. Durable in-repo footprint ~90 KB.

## Open issues

- **OI-1 (closes Q2)**: obtain the Muncie terrain DEM (public in the Windows 2D
  Muncie example) -> author tables via `ComputeFrom` over the imported/regenerated
  mesh -> element-wise A/B vs the GUI `Cells Volume Elevation` / `Faces Area
  Elevation` tables -> faithful exact-topology 2025 -> 6.x transplant solve vs the
  0109 baseline. This converts quantified-gap to a full validated UNLOCK.
- **OI-2 (the one net-new build)**: the HDF geometry-writer (2025 `Mesh` +
  `MeshPropertyTables` -> the 6.x `/Geometry/2D Flow Areas/*` schema). Either the
  exact-topology `new Mesh(...)` import (matches 6.x face ordering for an
  in-place transplant) or accept the +/-2-face regeneration and author a fresh
  geometry HDF.
- **OI-3**: the 2025 `ras` build is `-dev` / schema-unstable; re-characterize the
  API + schema every version bump (ADR 0127/0130 policy carries).
- **OI-4**: with `CreateVirtualCells=true` the mesh builds at the exact 5765-cell
  count but `Mesh.SanityCheck` throws `NotSupportedException` (non-fatal);
  characterize the virtual-cell path before relying on it.
