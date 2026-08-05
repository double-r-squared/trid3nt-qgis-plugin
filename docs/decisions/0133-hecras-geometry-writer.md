# ADR 0133 -- HEC-RAS: the 2D geometry WRITER lands (OI-2) + the deck-skeleton triage

Status: accepted (2026-08-04)
Follows: ADR 0132 (the Muncie transplant -- VALIDATED UNLOCK: the 2025 path authors
an arbitrary real-AOI mesh (Q1), computes subgrid tables headless on Linux
(ADR 0130), those VALUES match the 6.x GUI (Q2, cell-vol corr 0.99988), and the
production 6.x solver consumes externally-authored 2D tables to the baseline (Q3);
its ONE net-new build left was OI-2, "a general HDF geometry-writer for a
genuinely NEW AOI"), ADR 0109 (the Muncie riverine-flood baseline: maxWSE 951.93 ft,
~4881 wet cells, vol err 0.0058%), ADR 0100 (the M3 STOP -- the write-BLOCK this ADR
supersedes), ADR 0125 (the archetype triage + the rain-on-grid QUEUED row).

This wave builds OI-2 (the geometry writer) and validates it end-to-end through the
PRODUCT PATH, then TRIAGES the remaining links to a genuinely-new-AOI pure-2D deck
and STOPS precisely at the one that walls (the deck skeleton's forcing stanzas).

## What landed: the geometry WRITER (the net-new OI-2 component)

`services/workers/hecras2025/hecras_geometry_writer.py` (+ `test_hecras_geometry_writer.py`)
serializes a 2025-authored mesh (`Mesh2D` -- the topology arrays a
`Geospatial.Vectors.Mesh` carries: Cells / Faces / FacePoints / CellCenters /
Perimeter) plus `MeshPropertyTables.ComputeFrom` subgrid curves (`SubgridTables`)
into the exact `/Geometry/2D Flow Areas/<name>/` schema the production 6.x solver
reads -- the classic HEC ragged `Info(start, count)` + flat `Values` layout. The
schema (25 topology + subgrid datasets, per-dataset Column/Row/Units attrs, the
group `Attributes` compound, the parent `2D Flow Areas/Attributes` row, the root
`Projection`) was EXTRACTED from HEC's shipped Muncie geometry, never guessed.

The writer's genuine logic is the SCHEMA ASSEMBLY: ragged Info/Values splitting
from per-cell/face curve lists, dtype casting to HEC's on-disk types, the two
Attributes compounds, the attr stamping. The topology arrays + subgrid curves are
produced upstream by the 2025 `Mesh` + `ComputeFrom` (ADR 0130/0132), which the
writer does not recompute.

### Validation (two gates, both green)

- **Offline round-trip (no docker, `test_hecras_geometry_writer.py`, 2 tests).**
  Read Muncie's 25 geometry datasets, rebuild the writer's `Mesh2D`+`SubgridTables`
  inputs from the on-disk ragged layout, serialize through `write_2d_flow_area`
  into a fresh HDF, assert every dataset reconstructs VALUE-IDENTICALLY (shape +
  dtype + values, equal_nan) and both ragged tables are Info/Values-consistent
  (`sum(count) == len(Values)`, contiguous non-overlapping starts). PASS.

- **Muncie product-path self-check (live 6.x solve, `scratchpad/realaoi_proofs/`).**
  Author a FRESH `/Geometry/2D Flow Areas/2D Interior Area` group via the writer,
  splice it into a copy of the Muncie plan HDF (replacing the RASMapper-authored
  group), run the PRODUCTION `RasGeomPreprocess` + `RasUnsteady` in
  `trid3nt-local/hecras:latest`, compare to the on-machine ADR 0109 baseline:

  | run | maxWSE ft | wet cells | vol err % | flux out |
  | --- | --- | --- | --- | --- |
  | baseline (this machine, ADR 0109) | 951.9266 | ~4881 | 0.005836 | 33467.46 |
  | **writer-authored geometry (product path)** | **951.9266** | 4896 | 0.005834 | 33467.46 |

  **dWSE = 0.00000 ft; vol err + flux essentially identical.** A writer-authored
  geometry HDF group is consumed by the production 6.x solver and reproduces the
  baseline BIT-IDENTICALLY. OI-2's core risk -- does a from-a-writer geometry group
  solve? -- is discharged. Depth+mesh proof render:
  `scratchpad/realaoi_proofs/writer_authored_depth_mesh.png` (peak ~20.2 ft in the
  NW protected floodplain, mesh wireframe overlaid).

This SUPERSEDES the ADR-0100 write-BLOCK: that block rested on "nothing on the
Linux stack computes the subgrid property tables"; ADR 0130 removed that premise
(ComputeFrom runs headless on Linux) and this ADR proves the writer's output
solves. `server/src/trid3nt_server/agent/mesh/hecras_geometry.py`'s stale
write-STOPPED docstring is corrected to point at the writer.

## The deck-skeleton triage (per-file verdicts -- the archetype decision)

The transplant proven in ADR 0132 wrote 2D tables into Muncie's EXISTING deck. A
genuinely-NEW pure-2D AOI needs a whole deck authored from scratch. Per-file
verdict, from the in-repo Muncie combined-1D/2D reference
(`Muncie.p04.tmp.hdf` + `.b04` + `.x04`):

| deck file | role | authorability | verdict |
| --- | --- | --- | --- |
| geometry HDF `/Geometry/2D Flow Areas/<area>` | the 2D mesh + subgrid tables | schema fully extracted; the WRITER lands it | **BUILT + solver-validated (this ADR)** |
| `.pNN.tmp.hdf` `/Event Conditions` + `/Plan Data` | plan skeleton (BC timeseries, run control) | HDF, schema readable from Muncie's; authorable via h5py | AUTHORABLE (structure known) |
| `.xNN` (geometry preprocessor / 1D + SA decl) | for pure-2D: the 2D-area DECLARATION, no cross-sections | ASCII, characterized (Arrays Sizes + Job Control + Storage Area Data) | AUTHORABLE with care |
| `.bNN` (unsteady boundary/flow) | Job Control (reusable verbatim) + the FORCING stanza | Job Control YES; the FORCING stanza is the gap | **PARTIAL -- forcing stanza walls** |

The WALL is precise: the Muncie reference is a COMBINED 1D+2D case whose inflow
enters on a 1D reach (`Upstream Flow Hydrograph - River: White ...`) and couples to
the 2D area via a lateral structure; its `/Event Conditions` Boundary Conditions
are 1D (a Flow Hydrograph + a Normal Depth keyed by river/reach/RS). A PURE-2D
deck's forcing is a DIFFERENT stanza class -- a flow hydrograph attached to a named
2D BC LINE on the area perimeter, OR a precipitation block -- and NEITHER appears in
any in-repo reference. Authoring them blind and getting the engine to solve is the
one link without evidence.

## The forcing verdict (precip vs flow)

The signed rain-on-grid row PREFERS precipitation. The honest ranking on current
evidence:

- **Precipitation on the 2D area (the signed, preferred forcing): HIGHER RISK.**
  The Muncie deck has NO precipitation boundary (ADR 0125 finding 4); HEC-RAS 6.x
  DOES support precip-on-2D, but its Meteorology/Precipitation HDF+ASCII structure
  has NO in-repo reference at all. Authoring it needs a shipped rain-on-grid
  reference deck first.
- **Flow hydrograph on a 2D BC line (still real-AOI): MEDIUM RISK.** The structure
  is inferable (Job Control reused verbatim; the hydrograph timeseries mirrors the
  1D one but keys a 2D BC-line name; `/Event Conditions` carries a 2D Flow
  Hydrograph BC), but the exact 2D-BC-line stanza is likewise absent from the
  combined-1D/2D Muncie reference.

**Verdict: neither forcing can be VALIDATED this wave without a PURE-2D reference
deck.** Both walls are the same missing artifact (a shipped pure-2D example, incl.
a rain-on-grid one), which is obtainable from the SHA-pinned `Example_Projects_6_6`
distribution (ADR 0125 pin `ea239b50...386887`, the same zip ADR 0132 used for the
terrain). Per the charter's stop rule, this ADR does NOT force a blind authoring;
it lands the maximum honest subset (the writer + the product-path self-check) and
ledgers the deck-skeleton link with its precise unblock.

## The authoring-worker formalization (designed, not built this wave)

`services/workers/hecras2025/` already carries the pieces of the authoring worker
(Dockerfile from the ADR 0129 substitution recipe, `entrypoint.sh`,
`probe_ras_cli.py`, the `subst/crux/transplant/` C# harness -- `ApiProbe`,
`MeshGen`, `ComputeMuncie` -- proven to reflect the API, regenerate an arbitrary
mesh, and run `ComputeFrom` headless). The remaining formalization to a real
authoring worker: a stage entrypoint that runs
`fetch_dem -> ras createterrain -> perimeter+seeds -> TryCreateMesh -> ComputeFrom
-> dump-all-topology-arrays` and hands the arrays to `hecras_geometry_writer`, then
emits the geometry HDF. That stage is gated on the deck-skeleton unblock (a geometry
HDF with no solvable deck around it is not yet a product), so it is ledgered with
the forcing link rather than half-built.

## Consequences

- Registry UNCHANGED (no template/tool/contract landed -- the writer is an internal
  worker component, and a registered `hecras_flood_2d` template is correctly GATED
  on the deck-skeleton forcing link; landing a template that cannot run a new AOI
  end-to-end would be dead/dishonest). Coded tools delta: 0. New durable in-repo
  code: `hecras_geometry_writer.py` (~330 LOC) + `test_hecras_geometry_writer.py`
  (~180 LOC). Edited: `mesh/hecras_geometry.py` (docstring only -- the ADR-0100
  block corrected).
- No flood.py / SFINCS seam touched (grep-verified). No server import-graph change
  (the writer lives under `services/workers/`, imported only by its worker-local
  test + the future authoring stage). Offline suite baseline preserved (below).
- Image hygiene: no new images built; throwaway `--rm` containers on the
  pre-existing `trid3nt-local/hecras:latest` (2.2 GB) for both solves. Proofs +
  scratch scripts under `scratchpad/realaoi_proofs/` (deletable); durable footprint
  ~0.5 MB (writer + test).

## Open issues / ledger

- **OI-2 (this ADR): DISCHARGED for the geometry link.** The writer is built and
  its output is solver-validated (dWSE 0.0). What remains is the deck SKELETON, not
  the geometry.
- **OI-A (the deck-skeleton forcing stanza -- ledgered QUEUED).** Obtain a shipped
  PURE-2D reference deck (incl. a rain-on-grid one) from `Example_Projects_6_6`
  (SHA-pinned), characterize the 2D-BC-line flow-hydrograph stanza AND the
  precipitation/Meteorology block in `.bNN` + `/Event Conditions`, then author a
  from-scratch pure-2D deck around a writer-authored geometry and solve a small real
  US AOI end-to-end. This is the gate on BOTH acceptance (a) (a new US AOI) and a
  registered `hecras_flood_2d` template. Preferred forcing: precipitation
  (rain-on-grid, the signed row); fallback: 2D-BC-line inflow.
- **OI-B (the authoring-worker stage -- ledgered QUEUED).** Formalize
  `services/workers/hecras2025/` into an authoring worker (fetch_dem -> createterrain
  -> TryCreateMesh -> ComputeFrom -> dump arrays -> `hecras_geometry_writer`),
  gated on OI-A (a geometry HDF is not a product without a solvable deck).
- Carries ADR 0132 OI-3 (the 2025 `ras` build is `-dev`/schema-unstable;
  re-characterize the API + schema every version bump) and OI-4 (virtual-cell
  SanityCheck NotSupportedException).
