# ADR 0173 - HEC-RAS plan-HDF skeleton: the h5py surgery is proven and reduced to practice, but the shared blocker is TWO artifacts not one; RasSteady walls a level deeper

Date: 2026-08-07
Status: accepted

## Context

ADR 0172 identified the single shared blocker behind BOTH stalled HEC-RAS fronts
(0170 1D steady, 0171 2D structures) and prescribed the unblock: **construct a
minimal `File Type="HEC-RAS Results"` plan-HDF skeleton** around the seeded real
geometry so `RasGeomPreprocess` recognizes it, characterized as "mechanical HDF
surgery (h5py group copy), not physics authoring." This job executed that surgery
against the real 6.6 Linux engines (`trid3nt-local/hecras:latest`, image id
`5d4ac7cfbc8c`) and drove both fronts' gates as far as the evidence allows.

Two outcomes: the surgery **works and is reduced to practice** (the plan-HDF
skeleton is engine-recognized for both fronts), and the empirical probing
**corrects ADR 0172's central premise** -- the plan HDF is necessary but NOT
sufficient. A co-equal second artifact (a Muncie-format `.xNN` preprocessor
geometry) is required, and beyond it the steady engine has a further, deeper wall.

## What was proven CONSTRUCTIVELY (the plan-HDF skeleton, reduced to practice)

`services/workers/hecras/fixtures/build_plan_hdf_skeleton.py` performs the
Muncie-diff transplant: copy HEC's shipped Muncie plan HDF (the only in-repo
`File Type="HEC-RAS Results"` reference), delete its `/Geometry`, h5py-copy the
seeded fixture's real `/Geometry` subtree in, stamp `File Type="HEC-RAS Results"`,
repoint `Plan Information`. Built for both fronts and driven through the image:

- **Beaver Creek steady skeleton** (`BEAVCREK.g01.hdf` 1D cross-sections
  transplanted): root `File Type="HEC-RAS Results"`, top-level `Plan Data` +
  `Event Conditions` + `Geometry`; `Geometry` children `Cross Sections` /
  `River Centerlines` / `Structures` (a Bridge) / `Land Cover`.
- **Bald Eagle connection skeleton** (`BaldEagleDamBrk.g01.hdf` transplanted):
  same Results wrapper; `Geometry` carries the real `2D Flow Areas/BaldEagleCr`,
  `Storage Areas`, and `Structures` with `Type="Connection"` -- the exact class
  ADR 0171 found absent, now inside a recognized plan HDF.

**The recognition A/B (decisive):**

| input to `RasGeomPreprocess <hdf> g01` | result |
| --- | --- |
| raw seeded `BEAVCREK.g01.hdf` (`File Type="HEC-RAS Geometry"`) | `forrtl severe (29)` -> `htabopen_ (Htabopen.for:107)` -- the ADR 0172 `io.x` fallback |
| **this job's Results skeleton** (same geometry, transplanted) | advances PAST `io.x` into `READ_SIZ` (`An error occurred while reading FORMAT 50 in READ_SIZ`) |
| **connection Results skeleton** | identical -- advances into `READ_SIZ` |

The skeleton surgery flips `RasGeomPreprocess` from the legacy `io.x` reject into
the real geometry reader. **ADR 0172's recipe half (h5py surgery on the Muncie
template) is validated and reduced to a reusable builder** for both fronts.

## What CORRECTS ADR 0172: the `.xNN` is a co-equal missing artifact

ADR 0172 root-caused the `io.x` fallback solely to the HDF's `File Type` (correct
for its invocation, which failed at that gate first). Empirical probing this job
shows the requirement is a CONJUNCTION -- `RasGeomPreprocess` needs BOTH a
Results-typed plan HDF AND a Muncie-format `.xNN` preprocessor geometry at
`<project>.<geom_suffix>`:

- **Muncie's WORKING plan HDF with `Muncie.x04` DELETED** -> the identical `io.x`
  fallback (`Htabopen.for:107`). So a missing `.xNN` reproduces the fallback even
  with a valid Results HDF -- the fallback is not HDF-type-exclusive.
- **Muncie's WORKING plan HDF + a GUI-format `.gNN` staged at the `.x04` name**
  -> advances into `READ_SIZ` and fails at `FORMAT 50` (the `Section - Arrays
  Sizes` record). So the `.xNN` must be in the PREPROCESSOR format
  (`Muncie.x04`: `Section - Arrays Sizes` / `Section - River Reach Data` /
  fixed-field `FORMAT 50`), NOT the GUI `.gNN` text.
- **Exhaustive:** no seeded fixture (Beaver Creek, Critical Creek, Bald Eagle,
  Pump Station) ships a `Section - Arrays Sizes` `.xNN` -- all ship GUI `.gNN`
  (+ geometry `.gNN.hdf`). The `.xNN` is a GUI-preprocessor output HEC ships only
  for its Linux-verification project (Muncie), never for the Windows example set.

`RasGeomPreprocess` reads the network geometry FROM the `.xNN` and WRITES the
computed property tables INTO the plan HDF (proven: deleting the plan HDF's
`Geometry/GeomPreprocess` group and rerunning REGENERATES it from `Muncie.x04`).
So the plan-HDF skeleton is the WRITE target; the `.xNN` is the geometry SOURCE.
The h5py surgery produces the former; it cannot produce the latter. **The shared
blocker is two artifacts, and this job discharges one and isolates the other.**

## What SHARPENS ADR 0170: RasSteady walls a level below the geometry read

Even with both artifacts, the steady front has a further wall, now precisely
located. `RasSteady <plan_hdf> <geom_suffix>` aborts BEFORE any geometry-suffix
matters:

```
forrtl: severe (24): end-of-file during read, unit 15, file /data/Muncie.p04.tmp.hdf
  read_siz_is_post_ (Read_siz.for:349) <- snetopen_ (Snetopen.for:189) <- MAIN__ (Snet.for:88)
```

- **Unit 15 IS the plan HDF** (`/data/Muncie.p04.tmp.hdf`) -- captured in full,
  new this job. `read_siz_is_post_` Fortran-reads the plan HDF as a legacy
  SEQUENTIAL file and hits EOF. The abort is BYTE-IDENTICAL whether the geometry
  suffix is `x04` (valid `.xNN` present) or `f04` (steady-flow name) -- it fires
  before the geometry/flow read, so no `.xNN` or `.fNN` authoring can reach it.
- **The RasSteady binary carries only OUTPUT steady HDF paths** (grep of the
  43 MB engine: `/Output Blocks/.../Cross Sections`, `/Sediment Time Series`,
  `/Unsteady/Geometry Info` -- zero steady INPUT paths). It has no notion of
  reading steady INPUT from HDF; the two-argument HDF form is not the supported
  Linux steady invocation. This confirms + hardens ADR 0170: the steady engine
  predates the HDF-plan workflow. HEC's own `run_steady.sh` uses `RasSteady
  Muncie.r04` (a `.rNN` restart POST-processor, `Finished Post Processing`, ADR
  0172), not an independent steady-network solve.

The sharpened diagnosis: 1D steady on this Linux 6.6 build is **not headlessly
drivable via the HDF-plan workflow at all** -- not "missing a steady reference
deck" (0170) nor "missing a plan HDF" (0172), but an engine whose steady
network-sizes reader consumes a legacy compiled artifact no headless tool in this
distribution produces. Reverse-engineering it remains the multi-day leg ADR 0170
estimated; this job did not attempt it, because computing 1D property tables
headlessly (the reachable `.xNN` half) unblocks NO board row while `read_siz_is_post`
stands. **GATE 1 = STOP.** The `mixed_regime_multi_profile_solve` Belanger V&V
stays blocked: it needs a computed steady WSE profile, and no steady solve
completes to produce one.

## What RESOLVES an open ADR 0171 question: the connection cell-face pairing is IN the repo

ADR 0171 named the SA/2D-connection HW/TW cell-face pairing "the RASMapper M3
frontier" with "nothing to diff against"; ADR 0172 narrowed it (the mesh face
geometry is real) but could not test whether the pairing is Linux-computable.
**The pairing already exists in the repo, in preprocessor `.xNN` form:**
`services/workers/hecras2025/subst/crux/pure2d_reference/BaldEagleDamBrk.x09`
carries a complete `Section - Storage Area Connection Data` for the Sayers Dam
connection (`Conn 6`, weir coef 3.1) INCLUDING the HW-side and TW-side face-index
arrays (`17727 17729 ... / 17726 17728 ...`) -- the exact paired cell-face tables
ADR 0171 called authorable only by RASMapper. The frontier is not missing; it is
a shipped, HEC-authored reference.

The connection blocker is therefore NOT the pairing -- it is a **mesh-matching
gap**: the `x09` `.xNN` (pairing + connection) has NO vendored matching
`g09.hdf` mesh (only `g09_hdf_schema.json`, an 18066-cell schema without data),
and the seeded `g01.hdf` (real mesh + connection in HDF) has NO matching `.xNN`.
A consistent connection deck cannot be assembled from the two halves because the
`x09` face indices number the `g09` mesh, not `g01`'s. **GATE 2 = STOP**, narrowed
to a data-availability gap, not a capability gap: `RasUnsteady` is proven (Muncie
+ the beta-arc pure-2D decks), the plan-HDF skeleton is proven (above), and the
face-pairing schema is in hand -- what is missing is a single `.xNN`/mesh pair
that share cell numbering. The `weir_discharge_coefficient_tuning` A/B (ADR 0171
row 4) is ready the moment such a pair exists: `g01`'s connections all carry
`Use 2D for Overflow=1` (non-inert, unlike Muncie's weir).

## Decision

**Both gates STOP; the plan-HDF skeleton surgery lands as a reference builder, no
registry/template change.** The skeleton alone cannot solve either gate (steady:
`read_siz_is_post`; connection: mesh-matching), so per the honesty floor nothing
is registered and no template/row is added. Registry stays **226**;
`EXPECTED_TEMPLATES` stays **68**. `entrypoint.py`, `_BAKED_DECKS`,
`_KNOWN_MANIFEST_FIELDS` untouched -- no worker code executes the builder, so no
image rebuild (ADR 0148/0158 image law does not fire). No corpus/categories change.

The one durable code artifact -- `build_plan_hdf_skeleton.py` (+ its offline test)
-- is a non-registered reference utility, alongside the fixtures and the
`pure2d_reference` decks it complements. It discharges ADR 0172's h5py-surgery
recipe so the next front author builds the `.xNN` beside a proven, engine-recognized
plan HDF rather than reconstructing both blind.

## The recipe, corrected again (for whoever picks up either front)

- **1D steady (GATE 1):** blocked below the geometry read. The path is NOT more
  HDF/`.xNN` authoring -- it is either (a) a genuine steady solve produced by a
  Windows HEC-RAS GUI session (NATE-only, as Muncie's `.xNN` + plan HDF were
  GUI-seeded), yielding the `read_siz_is_post` reference structure to diff, or (b)
  abandoning the HDF-plan steady path for the legacy `.rNN`/`.ONN` steady
  toolchain if a headless compile exists. Until then, `mixed_regime` /
  `steady_floodway_encroachment` stay STOPped; the Belanger V&V has no computed
  profile to check.
- **2D connection (GATE 2):** the tractable front. Two paths to a matched
  `.xNN`/mesh pair: (a) obtain the `g09.hdf` mesh (11 MB, from the same public
  BaldEagle example the `x09` came from) so the shipped `x09` pairing + a plan-HDF
  skeleton built from `g09.hdf` form a consistent deck -- then `RasUnsteady` with
  flow through the Sayers Dam connection is a direct test; or (b) author a `.xNN`
  for the seeded `g01` mesh by computing the face pairing (intersect the
  `Structures/Centerline Points` against `2D Flow Areas/BaldEagleCr` face
  geometry), validated against `x09`'s pairing FORMAT as the schema reference.
  Path (a) is the smaller lift and reuses this job's proven skeleton builder
  directly.

## Consequences

- Coded-tools metric: **0 registered tools, 0 templates** added; registry
  226 -> 226, `EXPECTED_TEMPLATES` 68 -> 68. One non-registered reference builder
  (`build_plan_hdf_skeleton.py`, ~90 LOC) + its offline test.
- Evidence (in-container, `trid3nt-local/hecras:latest`, image id `5d4ac7cfbc8c`):
  Muncie baseline `RasGeomPreprocess` green (`Finished Processing Geometry`);
  `.xNN`-deleted -> `io.x` fallback; GUI-`.gNN`-staged -> `READ_SIZ FORMAT 50`;
  raw geometry-only HDF -> `io.x`; both Results skeletons -> `READ_SIZ`; RasSteady
  `read_siz_is_post` EOF on unit 15 = plan HDF (invariant across geom suffix);
  RasSteady binary carries OUTPUT-only steady HDF paths.
- Offline slice green (**65 passed**, `env -u TRID3NT_CACHE_BUCKET ... --timeout=300`):
  `test_plan_hdf_skeleton` (new), `test_deck_edit`, `test_entrypoint`,
  freshtopo `test_freshtopo` / `test_hecras_deck2d` / `test_event_conditions`,
  `test_catalog_surfacing` (registry 226), `test_door_dissolution`
  (EXPECTED_TEMPLATES 68).
- No solve-proof charts: no steady WSE / Belanger / connection-flow figure is
  rendered because no solve completed on either gate -- fabricating one would
  violate the honesty floor. The evidence is the raw engine transcripts above.
- The HEC-RAS fronts' shared blocker is now precisely partitioned: the plan-HDF
  skeleton (DONE, reusable), the `.xNN` preprocessor geometry (per-front lift,
  GUI-format-gated), the steady `read_siz_is_post` legacy reader (architectural
  wall, GUI-seed-or-abandon), and the connection mesh-matching gap (data
  availability). Three of the four are characterized to a next concrete step; the
  connection front is the tractable one.
