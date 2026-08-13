# ADR 0136 -- HEC-RAS: the FRESH-TOPOLOGY solve probe -- a repo-authored 2D tessellation is ACCEPTED and SOLVED by the production 6.6 engines (ADR 0135 fused c1+c2 discharged); wetting the fresh BC line is the precise remaining STOP

Status: accepted (2026-08-05)
Follows: ADR 0135 (the BC-lines writer + pure-2D `.bNN` discharge; the fused
c1+c2 -- "a fresh 2D topology solving end-to-end through the writer + production
6.x engines" -- ledgered as the ONE untested risk, with the RECOMMENDED FIRST
PROBE being pure-Python topology surgery on a Muncie sub-rectangle), ADR 0134
(the pure-2D deck architecture + the `x09`/`b06` references), ADR 0133 (the
geometry WRITER, solver-validated on re-serialized Muncie topology, dWSE 0.0),
ADR 0132 (the Muncie transplant -- VALIDATED UNLOCK), ADR 0109 (the Muncie
baseline: maxWSE 951.93 ft, ~4881 wet cells, vol err 0.0058%).

This wave runs ADR 0135's recommended first probe and DISCHARGES the fused
c1+c2's core risk: a genuinely-fresh 2D tessellation -- authored by this repo,
with a different perimeter + cell layout than anything HEC shipped -- is accepted
by the production 6.6 `RasGeomPreprocess` AND solved by `RasUnsteady` (the 2D
Diffusion-Wave solver engages over the fresh mesh, the run completes, volume
accounting closes at 0.0021%). It then STOPS precisely at the ONE remaining link:
directing an inflow onto the fresh 2D BC line, for which no combined-1D/2D `.bNN`
reference exists.

Experiment-only-plus: NO server / tool / contract / registry change (registry
byte-identical by construction). New durable code under
`services/workers/hecras2025/subst/crux/freshtopo/`; transcripts + renders under
`scratchpad/freshtopo_proofs/`.

## The carve (fresh topology from solver-proven ingredients)

`carve_muncie.py` extracts a spatial sub-rectangle of Muncie's shipped 2D flow
area (the NW quadrant: `x < 408600, y > 1803025` -- the low ground incl. the
channel thalweg at 925 ft + the protected floodplain edge) and RE-INDEXES it from
zero into a fresh, smaller mesh:

| metric | full Muncie | carved NW |
| --- | --- | --- |
| real cells | 5391 | **2068** |
| faces | 11164 | **4247** |
| facepoints | 5774 | **2180** |
| ghost cells | 374 | **171** |
| perimeter points | 170 (RASMapper polygon) | **171** (walked from the cut) |
| **fresh cut faces** (interior in Muncie, now external) | 0 | **66** |

Every topology array is rebuilt in HEC's exact 2D conventions (all decoded +
validated against the full Muncie mesh -- see `test_freshtopo.py`):

1. `Faces Cell Indexes [col0, col1]`: the `NormalUnitVector` points col0 -> col1;
   external faces put the REAL cell in col0, a fresh ghost in col1 (normal
   outward). Held for 100% of Muncie's 374 external faces.
2. `Faces FacePoint Indexes [A, B]`: ordered so `rot_-90(B-A) == normal`, where
   `rot_-90((dx,dy)) = (dy,-dx)`. Held for 11164/11164 Muncie faces.
3. Ghost cell: center = the external-face midpoint; surface area 0; min elev NaN;
   no volume-elevation curve.
4. `Cells Face and Orientation`: `+1` if the cell is col0 of the face, else `-1`.
5. `FacePoints Cell / Face adjacency`: CCW-angular (`atan2(center-fp)` /
   `atan2(otherfp-fp)`), the face-orientation `+1` iff the fp is the face's B
   endpoint. Both verified CCW-up-to-rotation on 500 sampled Muncie facepoints.

The 66 cut faces + the 171-facepoint walked perimeter are topology THIS REPO
authored -- they exist in no HEC-shipped geometry. Every carried subgrid curve
(cell volume-elevation, face area-elevation-WP-Manning, min elevations) is a
Muncie solver-proven value at a fresh index. The carved mesh is HEC-clean: max 5
faces/cell (Muncie's max is 7), no degenerate faces (min face length 17.1 ft).

## The `.xNN` author (link c4 -- the durable addition)

The ADR 0135 charter asked for a durable, tested `.xNN` author (SA + reach + Arrays
Sizes). Two paths were built + characterized:

- **`patch_xnn` (from the shipped `x09`)** -- parses + fully processes through
  `RasGeomPreprocess`, BUT the shipped pure-2D `x09` fake reach is entangled with
  its Sayers-Dam SA-connection: removing the dam (required -- a single-SA deck
  has no valid dam) leaves the reach header unreadable (`error reading header
  information for reach`) because the fake reach's node numbering + Arrays-Sizes
  node counts assumed the dam's nodes. Reducing x09 to a clean pure-2D reach needs
  a node-topology rebuild, not a text patch. **This is a sharp finding: the
  shipped pure-2D reference is NOT trivially reducible to a dam-free deck.**
- **`remove_lateral_weirs` (from Muncie's proven `x04`) -- THE WORKING PATH.**
  Muncie's `x04` fully processes on the 6.6 engine (it is the M3 gate), but it is
  a COMBINED 1D/2D deck: its White River reach carries two `NODE` type-6 lateral
  weirs (RS 13214 + RS 7300) whose `DS SA/2D` is `2D Interior Area`. Left in, they
  crash `RasUnsteady` in `jobinit_lw_q2d` (the weir cannot couple to a carved
  boundary that no longer lies on the weir line). The transform removes both
  type-6 NODE blocks, decrements the Arrays-Sizes node counts + the Reach-Boundary
  downstream node, and patches the SA perimeter count -- yielding a standalone
  reach + a bare 2D flow area that solves.

Both are durable + unit-tested; the geometry-HDF group is authored by the ADR
0133/0135 `write_2d_flow_area` + `write_boundary_condition_lines` writers, unchanged.

## The iteration log (each named solver error -> its fix)

The probe is the ADR 0133 method -- iterate on named errors:

| # | engine error | root cause | fix |
| --- | --- | --- | --- |
| 1 | `end-of-file during read ... x01` (`readin`/`htabreal`) | hand-rolled Arrays Sizes mis-set the 1D htab counts | author `.xNN` by patching a proven reference, never by hand |
| 2 | `input conversion error ... b01` (`read_un_beg`) | hand-rolled `.bNN` number formats | patch the shipped `b06`/`b04`, keep byte-faithful fields |
| 3 | `ERROR with Connection ... Sayers Dam` (`RasUnsteady` 2D init) | x09's dam SA-connection references a structure the pure-2D deck lacks | drop the dam -> exposes finding: x09 reach is dam-coupled (`error reading header for reach`) |
| 4 | `jobinit_lw_q2d` crash | Muncie's two lateral weirs orphaned by a swapped 2D mesh | `remove_lateral_weirs` |
| 5 | `Error with Lateral/Hydraulic Facepoints ... 7300` | keeping a weir: it cannot couple to the carved boundary | remove both weirs (native-weir forcing is off the table for a sub-carve) |
| 6 | `integer divide by zero` (`hdf_set_compression`, 1D output block) | `HYDROGRAPH LOCATIONS = 0` | point it at one valid 1D node |
| 7 | **SOLVE COMPLETES** | -- | -- |

## The solve verdict (the fused c1+c2 core risk DISCHARGED)

The fresh-topology deck solves end-to-end through the PRODUCTION 6.6
`RasGeomPreprocess` + `RasUnsteady`:

| run (flow scale) | vol err % | boundary flux in | flux out | 2D max WSE | 2D wet cells |
| --- | --- | --- | --- | --- | --- |
| **1.0** | **0.002150** | 36674 | 35305 | 946.93 | 0 (dry) |
| **1.5** | 0.002933 | **55011** (= 36674 x 1.5) | 52977 | 946.93 | 0 (dry) |

`RasGeomPreprocess` reports `Finished Processing Geometry` on the fresh mesh;
`RasUnsteady` reads the 2D area, prints `2D Unsteady Diffusion Wave Equation Set` +
`2D number of Solver Cores: 6`, builds + solves the implicit diffusion-wave system
over the fresh tessellation, and prints `Finished Unsteady Flow Simulation` with a
closing volume balance of 0.0021%. **The ADR 0135 "ONE untested risk" -- whether a
fresh mesh's face normals / cell-face orientation / facepoint winding / rebuilt
perimeter satisfy the solver's consistency checks -- is DISCHARGED: they do. The
solver accepts and solves a repo-authored fresh topology.** The x1.5 forcing scale
moves the solve deterministically (flux in scales exactly 1.5x; vol err tracks) --
the acceptance-style delta, confirming the completing run genuinely consumes the
deck.

## The precise STOP: wetting the fresh 2D BC line

The fresh 2D flow area is DRY at both scales: the White River 1D reach carries the
forcing, and it is (correctly) decoupled from the 2D area (the weirs were removed).
Directing an inflow onto the fresh 2D BC line walls precisely, and the wall is a
missing REFERENCE, not the topology:

- A **bare `Upstream Flow Hydrograph`** (the shipped pure-2D `b06` form) in this
  combined deck maps POSITIONALLY to the 1D reach's upstream boundary, not to the
  2D BC line (verified: the solve is byte-identical to the 1D-only run).
- A **distinct 2D-BC-line flow stanza** appended to the `.bNN` triggers `input
  conversion error` in `read_hydro` -- there is no shipped reference for a
  2D-BC-line flow hydrograph in a COMBINED 1D/2D boundary file (b06 is pure-2D;
  Muncie's b04 is 1D-reach + lateral-weir; neither shows the combined form).
- The **native lateral-weir** path (let White River spill into the 2D area) needs
  the carve boundary to lie ON the weir line + a rebuild of the weir->2D cell
  connectivity (`Structures/User Defined Weir Connectivity`); a sub-rectangle
  whose interior contains the weir cannot couple.
- The Windows `.u` `Initial Storage Elev=<area>,<wse>` 2D initial-condition
  directive is NOT accepted in the Linux `.bNN` (breaks the friction/breach read).

This is the same charter discipline: a completing solve is the gate, and the fresh
topology PASSES it; wetting the fresh faces with MOVING water needs either a
shipped combined-deck 2D-BC-line `.bNN` reference OR a pure-2D deck whose sole
flow boundary is the 2D BC line (blocked by the x09 dam-coupled-reach finding).
No template lands on a dry-2D solve.

## Consequences

- No server / tool / contract / registry change; registry byte-identical (git:
  this ADR + the `freshtopo/` durable code + test). Coded-tools delta: **0** (all
  new code is worker-local authoring components, like the ADR 0133/0135 writers).
  No template pin / category / corpus change -- none is warranted until the 2D BC
  line can be wetted end-to-end.
- New durable code under `services/workers/hecras2025/subst/crux/freshtopo/`:
  `carve_muncie.py` (the fresh-topology carve + `--validate`), `hecras_pure2d_deck.py`
  (the `.xNN`/`.bNN` authors incl. `remove_lateral_weirs` + `patch_muncie_bnn`),
  `build_freshtopo_deck.py` + `solve_freshtopo.py` (the build + in-container solve
  harness), `test_freshtopo.py` (5 offline gates, green). No `flood.py` / SFINCS /
  `publish_layer` / registry reference (grep-verified). No server import-graph change.
- Offline suite: all new files are under `services/workers/` and are NOT collected
  by `server/tests/`; server baseline delta is **zero by construction**.
- Image hygiene: no image built; throwaway `--rm` containers on the pre-existing
  `trid3nt-local/hecras:latest` (2.2 GB). Proofs + renders under
  `scratchpad/freshtopo_proofs/` (deletable); durable footprint ~30 KB.

## Open issues / ledger

- **ADR 0135 c1+c2 (this ADR): the CORE RISK is DISCHARGED.** A repo-authored fresh
  2D tessellation solves through the production 6.6 engines; the solver's mesh
  consistency checks pass. What remains is DIRECTING FORCING onto the fresh 2D BC
  line, not the topology's solve-validity.
- **OI-FT1 (the 2D-BC-line forcing -- QUEUED, the precise STOP).** Obtain (or author
  + empirically confirm) a 2D-BC-line flow-hydrograph `.bNN` stanza -- either the
  COMBINED-deck form (a shipped 1D+2D example that forces a 2D BC line) or a clean
  PURE-2D deck whose only flow boundary is the 2D BC line. The latter needs a
  dam-free reach, which the shipped `x09` does not reduce to (this ADR's finding);
  a node-topology reach rebuild or a different shipped pure-2D reference unblocks it.
  Wetting gives the ADR 0135 sanity (wet cells where the carved terrain is low) +
  the x1.5 delta ON the 2D area.
- **OI-FT2 (the template -- QUEUED).** `hecras_flood_2d` + its archetype + a
  formalized authoring worker stage land TOGETHER once OI-FT1 wets the fresh 2D
  area end-to-end, with both acceptances (a Muncie self-check + a genuinely-new
  small US AOI). Unchanged from ADR 0135; the geometry + fresh-topology halves are
  now proven, the forcing half remains.
- Carries ADR 0134 OI-D (precipitation -- Meteorology+DSS residual) and ADR 0132
  OI-3 (the 2025 `ras` build is `-dev`/schema-unstable) + OI-4 (virtual-cell
  SanityCheck NotSupportedException).
