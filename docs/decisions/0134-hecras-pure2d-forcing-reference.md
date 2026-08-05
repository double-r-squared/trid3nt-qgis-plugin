# ADR 0134 -- HEC-RAS: the pure-2D forcing reference obtained (OI-A discharged) + the precise remaining chain

Status: accepted (2026-08-05)
Follows: ADR 0133 (the 2D geometry WRITER lands at dWSE 0.00000 + the deck-skeleton
triage that walled precisely on "the forcing stanza" -- OI-A/OI-B ledgered QUEUED,
gated on obtaining a shipped PURE-2D reference deck), ADR 0132 (the Muncie transplant
VALIDATED UNLOCK -- 2025 authors an arbitrary mesh, computes subgrid tables headless,
the production 6.x solver consumes externally-authored tables), ADR 0125 (the
archetype triage + the SHA-pinned `Example_Projects_6_6` distribution + the
rain-on-grid QUEUED row).

This wave discharges **OI-A** (the deck-skeleton forcing stanza -- the ONE link ADR
0133 had no evidence for) by OBTAINING and characterizing the shipped pure-2D
reference, then specifies **OI-B** + the `hecras_flood_2d` template's remaining
chain precisely and STOPS there per the charter's stop rule (no blind authoring, no
template that cannot run a new AOI end-to-end).

Research + reference-artifact wave: NO server / worker / tool / contract / registry
change (registry byte-identical by construction). Durable artifacts under
`services/workers/hecras2025/subst/crux/pure2d_reference/`; investigation transcripts
under `scratchpad/realaoi2_proofs/`.

## What ADR 0133 walled on, and why it is now discharged

ADR 0133 built the geometry writer and proved its output solves (dWSE 0.0), then
triaged the deck skeleton and found ONE link without evidence: a PURE-2D deck's
forcing is a different stanza class than Muncie's combined-1D/2D case (Muncie's
inflow enters on a 1D reach + lateral structure), and "NEITHER [a 2D-BC-line flow
hydrograph nor a precipitation block] appears in any in-repo reference." Its unblock
was explicit: obtain a shipped pure-2D reference from `Example_Projects_6_6`.

**Obtained.** The zip re-downloaded + SHA-verified EXACT against the ADR 0125 pin
(`ea239b50...386887`, 432389121 B). Its two 2D projects are Muncie (combined 1D/2D,
already in-repo) and **BaldEagleCrkMulti2D**, which ships **pure-2D single-area
plans** -- AND, decisively, ships their **preprocessed Linux intermediates**:

- `BaldEagleDamBrk.b06` -- the Linux `.bNN` boundary file for plan 06 ("Gridded
  Precip - Infiltration", `Flow File=u03`, `Geom File=g09`).
- `BaldEagleDamBrk.x09` -- the `.xNN` geometry-preprocessor file for the pure-2D
  `g09` geometry.

ADR 0133's premise "the shipped Windows projects lack the Linux intermediates" was
true for the Muncie folder but FALSE for BaldEagle plan 06: HEC shipped the
preprocessed `.b06`/`.x09`. So the `.bNN` forcing reference did not have to be
authored blind and confirmed by empirical `RasUnsteady` iteration -- it is a shipped,
HEC-authored artifact, valid by construction. All six ASCII reference decks + the
`g09.hdf` schema are vendored (56 KB) under `pure2d_reference/` with a decoded
README.

## The forcing-reference story (which example, what the stanza is, precip vs flow)

### 2D-BC-line inflow hydrograph -- AUTHORABLE (the landing forcing)

The Windows `.u` declares a 2D BC line as `Boundary Location=` with field 6 = the 2D
area name and field 8 = the BC-line name (`u09`: area `Upstream2D`, line `USFlow`;
`u02`/`u03`: area `BaldEagleCr`, line `Upstream Inflow`), followed by
`Interval=<step>` + `Flow Hydrograph= <n>` with flow-only ordinates.

The Linux `.b06` expands that, inside a `Hydrograph Data` section, to a **BARE**
`Upstream Flow Hydrograph` header (NO `River:/Reach:/RS:` suffix -- that suffix is
precisely what marks a 1D-reach inflow; its absence marks a 2D-BC-line inflow) + an
explicit `(time, flow)` pair list in HEC's 8-char fixed fields, then a bare
`Downstream Normal Depth` + slope. The `.u`->`.bNN` transform is identical to the
Muncie `u01`->`b04` pair characterized in ADR 0132/0133. Mapping to a specific BC
line is **positional** against the geometry's BC-line list (the `g09.hdf`
`/Geometry/Boundary Condition Lines/Attributes` rows: `Upstream Inflow`,
`DSNormalDepth`, `DS2NormalD`, all on `BaldEagleCr`). The in-repo
`deck_edit.scale_flow_hydrograph` already matches bare `Flow Hydrograph` headers, so
the existing flow scaler drives this stanza with no change.

### Precipitation (rain-on-grid) -- NOT `.bNN`-authorable (named residual, confirms 0133)

`u03` carries precipitation as a `Met BC=Precipitation|Mode=Gridded` block pointing
at a binary `precip.2018.09.dss` grid + a `Gridded DSS Pathname`. The Linux `.b06`
for that SAME precip plan contains **no precipitation stanza whatsoever** -- only the
2D BC-line inflow + normal depth. Precipitation forcing does not live in the `.bNN`;
it lives in the plan-HDF Meteorology group + a binary HEC-DSS grid. It is therefore
NOT authorable as a boundary-file edit; it needs a Meteorology-HDF + DSS-writer path
(or a uniform-hyetograph variant whose serialization still has no shipped reference
here). **Verdict: the flow-forced 2D BC line is the landing forcing; precipitation
stays a named residual** (the signed-but-higher-risk row), deferred to a
Meteorology+DSS wave that the Atlas-14 seam feeds once it exists.

## The pure-2D deck architecture, fully decoded

A genuinely-new pure-2D AOI deck the Linux engines solve needs:

1. **Geometry HDF** `/Geometry/2D Flow Areas/<name>/` -- the `hecras_geometry_writer`
   authors this today (solver-validated, ADR 0133) -- **PLUS**
   `/Geometry/Boundary Condition Lines/` (the writer does NOT yet author it). Schema
   captured in `g09_hdf_schema.json`: `Attributes[Name S32, SA-2D S16, Type S8,
   Length f4]`, `External Faces[BC Line ID i4, Face Index i4, FP Start i4, FP End i4,
   Station Start f4, Station End f4]` (each BC line -> its perimeter faces),
   `Polyline Info/Parts/Points`.
2. **`.xNN`** (reference `x09`): the 2D area declared as a **Storage Area** (`SA 8
   ... BaldEagleCr`), a minimal **fake 1D reach** (`Fake River`/`Fake Reach` -- the
   engine requires >=1 reach), Arrays Sizes counts matching the mesh, and the
   PropertyTableOptions.
3. **`.bNN`** (reference `b06`): bare positional `Upstream Flow Hydrograph` +
   `Downstream Normal Depth`. DISCHARGED.
4. **Plan HDF Event Conditions** (authorable per ADR 0133); + Meteorology only for
   the precip residual.

## OI-B assessment (the authoring worker) -- tractable, not landable this wave

`ComputeMuncie.cs` already runs the full authoring chain for an ARBITRARY AOI --
`new Terrain(dem)` -> `MeshFactory.TryCreateMesh(perimeter, seeds)` ->
`MeshPropertyTables.ComputeFrom(mesh, terrain, nvalue, opts)` -- and dumps cell
centers, face midpoints, and the ragged subgrid tables. What it does NOT dump is the
**full mesh topology** the writer's `Mesh2D` needs (Cells FacePoint Indexes, Faces
Cell/FacePoint Indexes, FacePoints Coordinate, cell/face orientation arrays,
Perimeter) in HEC's exact index/orientation/winding conventions. The transplant
(ADR 0132) sidestepped this by reusing Muncie's OWN 6.x topology. Extending the C#
harness to serialize a FRESH mesh's full topology is real, non-trivial work, and --
critically -- a fresh `.NET` topology solving through a writer-authored geometry HDF
is UNVALIDATED (ADR 0133 validated the writer only on re-serialized Muncie topology
spliced into Muncie's plan, not on a fresh mesh). Landing the worker without that
solve validation would be a half-built stage; per the same discipline ADR 0133
applied to the template, it is specified here, not half-built.

## Why the template does NOT land this wave (the precise STOP)

`hecras_flood_2d` (a real-AOI 2D flood template) is correctly GATED. Its acceptance
(b) -- a genuinely new US AOI solved end-to-end -- depends on FOUR still-unbuilt /
unvalidated links, now each precisely specified:

- **(c1)** the `.NET` full-topology dump for a fresh mesh (OI-B; unbuilt C#);
- **(c2)** a fresh-topology writer-authored geometry HDF actually SOLVING (untested --
  the fresh mesh's face normals / cell-face orientation / facepoint winding must
  satisfy the solver's consistency checks, which only a Muncie-topology round-trip
  has exercised);
- **(c3)** the `hecras_geometry_writer` extension that authors
  `/Geometry/Boundary Condition Lines/` (schema now in hand) + the BC-line polyline
  on the mesh perimeter;
- **(c4)** the `.xNN` author (SA + fake reach + Arrays Sizes matching the fresh mesh)
  + the plan-HDF Event Conditions author.

Link (c5) the `.bNN` forcing -- the ONE ADR 0133 had zero evidence for -- is
DISCHARGED (reference `b06` + the existing flow scaler). Landing a template that
cannot yet run acceptance (b) would be dead/dishonest (the ADR 0133 doctrine).
Registry stays UNCHANGED; the template + contract remain GATED on (c1)-(c4).

## Consequences

- No server / worker / tool / contract / registry change; registry byte-identical
  (git: this ADR + the `pure2d_reference/` artifacts + README). Offline suite
  untouched (no importable code changed). Coded-tools delta: 0.
- Durable in-repo footprint ~60 KB (six HEC public-domain ASCII decks, CRLF
  stripped, + the `g09.hdf` schema JSON + the decoded README). The 432 MB zip, the
  11 MB `g09.hdf`, and all extractions were deleted after use.
- No flood.py / SFINCS seam touched. No import-graph change.

## Open issues / ledger

- **OI-A (this ADR): DISCHARGED.** The pure-2D forcing reference is obtained,
  vendored, and decoded; the 2D-BC-line flow `.bNN` stanza is known and authorable
  with the existing scaler; precipitation is confirmed a Meteorology+DSS residual,
  not a `.bNN` edit.
- **OI-B (the authoring worker -- QUEUED, tractable).** Extend `ComputeMuncie.cs` ->
  a general `AuthorMesh` that dumps a fresh mesh's FULL topology; formalize the
  Dockerfile stage entrypoint (terrain-in -> createterrain -> TryCreateMesh ->
  ComputeFrom -> dump-all-topology -> `hecras_geometry_writer`). Gate the LANDING on
  a fresh-topology geometry HDF SOLVING (link c2).
- **OI-C (the writer BC-line extension -- QUEUED).** Add
  `write_boundary_condition_lines()` (schema in `g09_hdf_schema.json`) + an offline
  round-trip test, and the `.xNN`/Event-Conditions authors. Then land
  `hecras_flood_2d` with BOTH acceptances (the Muncie self-check through the template
  + a genuinely new small US AOI end-to-end).
- **OI-D (precipitation -- named residual).** A plan-HDF Meteorology + HEC-DSS grid
  author (Atlas-14 seam preferred). No shipped `.bNN`/HDF precip reference in this
  distribution; needs its own reference before authoring.
- Carries ADR 0132 OI-3 (the 2025 `ras` build is `-dev`/schema-unstable;
  re-characterize per version bump) and OI-4 (virtual-cell SanityCheck
  NotSupportedException).
