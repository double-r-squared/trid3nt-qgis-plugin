# ADR 0170 - HEC-RAS 1D steady / RasSteady front: machinery characterized, STOP on the missing steady reference deck

Date: 2026-08-07
Status: accepted

## Context

The M/L sign-off shortlist (`docs/validation/ml-signoff-shortlist.md`) ranks the
**HEC-RAS 1D-network / RasSteady deck author** as machinery front #17, unblocking
four board rows: `mixed_regime_multi_profile_solve`,
`steady_floodway_encroachment`, `storage_area_network_flow_reversal`,
`modified_puls` (plus the M-effort `steady_hwm_calibration`). ADR 0157 corrected a
prior-board error ("1D steady signed" was WRONG) and STOPped rows 1/2 with the
finding: **`RasSteady` is baked in the 6.6 solver image but NOTHING invokes it** -
every workflow runs `RasGeomPreprocess` + `RasUnsteady`; there is no 1D steady
authoring, no `.fNN` steady-flow writer, and 1D cross-section decks exist only as
the shipped Muncie fixtures.

This job attempted the front: author a minimal single-reach 1D steady deck
(synthetic prismatic channel), wire `RasSteady` for the first time, parse the WSE
profiles, and land `mixed_regime_multi_profile_solve` (Belanger conjugate-depth
V&V) + `steady_floodway_encroachment`. Per the triage-first law, the machinery was
probed empirically against the real 6.6 Linux engines BEFORE any build.

## Machinery findings (empirical, in-container against `trid3nt-local/hecras:latest`)

Two findings advance the surface beyond ADR 0157; one is the wall.

1. **`RasGeomPreprocess` reads the `.x0N` ASCII geometry and computes 1D
   cross-section conveyance headless - no 2D-subgrid wall.** Proven by
   perturbation: raising the first Muncie cross-section's station-elevation in the
   `Muncie.x04` TEXT file by 5 ft and rerunning `RasGeomPreprocess Muncie.p04.tmp.hdf
   x04` changed the computed `Geometry/Cross Sections/Property Tables/XSEC Value`
   table (max |diff| = 38015) while the plan HDF's `Station Elevation Values`
   dataset was untouched (max |diff| = 0.0). So the `.x0N` text is the authoritative
   1D geometry SOURCE and the engine builds the conveyance/property tables from it.
   The M3 STOP (RASMapper's Windows-DLL 2D subgrid tables) does NOT apply to 1D:
   1D property tables are computable on the Linux stack. The `.x0N` format (the
   Muncie `Section - Arrays Sizes` / `Section - River Reach Data` / `NODE`
   station-elevation blocks) is a faithful, authorable 1D-geometry reference.

2. **`RasSteady` EXECUTES on Linux (first-ever invocation) - refining ADR 0157's
   "never invoked".** `RasSteady <plan_hdf> <geom_suffix>` mirrors the `RasUnsteady`
   CLI. It loads, opens the plan HDF, and enters the steady network reader
   (`Snet.for` MAIN -> `Snetopen.for` -> `Read_siz.for`). Success sentinels in the
   binary: `Finished Steady Flow Simulation` / `Steady Finished Successfully`.
   The binary confirms first-class support for the target rows' physics:
   `Computing supercritical profile` / `~ Recomputing to subcritical profile` /
   `Critical depth was used instead` (mixed flow regime) and a full
   `Overbank Encroachment Method` / `Encroachment_L_R` / `Encroached WSE ...` block
   (floodway encroachment). The engine is capable; nothing in the binary is the
   blocker.

3. **THE WALL: `RasSteady` needs a steady-TYPED plan HDF that does not exist
   anywhere.** On the (unsteady) Muncie plan HDF, `RasSteady` aborts at
   `read_siz_is_post_` (`Read_siz.for:349`) with `forrtl: severe (24):
   end-of-file during read, unit 15, file Muncie.p04.tmp.hdf`. This reproduces
   identically whether or not a preceding Linux `RasGeomPreprocess` is run and
   whether or not a classic text `Muncie.f04` steady-flow file is staged beside the
   plan - the abort is in the geometry/network SIZE read, BEFORE any flow read. The
   Muncie plan HDF is unsteady (`Plan Data/Plan Information/Flow Filename =
   Muncie.u01`; `Event Conditions` has only an `Unsteady` subgroup) and lacks the
   steady network-sizes structure the steady engine expects. Crucially, unlike the
   unsteady path - whose inflow forcing rides an ASCII `.bNN` file that the engine
   reads by naming convention (the `deck_edit.py` lesson) - a string dump of
   `RasSteady` surfaces **no named HDF dataset paths** for steady flow / profiles /
   steady event-conditions (searched: `Steady Flow`, `Profile Names`,
   `Event Conditions/Steady`, `Number of Profiles`, etc. - zero hits beyond the
   OUTPUT paths `Steady Profiles` / `Critical Water Surface` / `Profile Names`). So
   the steady plan HDF's INPUT schema is opaque and must be reverse-engineered from
   the closed Fortran `Read_siz`/`Snetopen` readers with **no reference deck to
   diff against**.

## Decision

**All five rows: honest STOP.** The blocker is not engine capability (both engines
run headless; steady physics is in the binary) and not 1D geometry authoring (the
`.x0N` text path is tractable). The blocker is a **missing steady reference deck**:
a valid steady-typed plan HDF whose network-sizes / steady-event-conditions
structure `read_siz_is_post` consumes. That structure is unshipped, absent from the
HEC `Linux_RAS_v66.zip` distribution and the repo, and does not surface as named
datasets in the binary - so it cannot be authored blind within a bounded budget.
Reverse-engineering it from the Fortran reader with no reference is a multi-day leg
comparable to the entire 0127-0140 2D-authoring arc, NOT the "~2-4 h front" the
shortlist assumed (that estimate wrongly presumed the unsteady machinery
generalized to steady; the steady plan HDF is disjoint from the unsteady one).

**No registry, template, corpus, categories, entrypoint, or image change.** Wiring
a `RasSteady` entrypoint leg that always aborts at `read_siz` would violate the
honesty floor (a template that cannot solve must not land) and the worker-image law
(no behavior-proving smoke to gate a rebuild). Registry stays 226;
`EXPECTED_TEMPLATES` stays 68. This is a pure characterization + recipe.

## Recipe (the constructive unblock)

The cheapest unblock mirrors exactly how the unsteady path was seeded: **obtain a
1D steady REFERENCE deck as a fixture**, then reparameterize it (the engines
already run headless).

1. **Seed a steady reference fixture (a one-time NATE / GUI step, mirrors the
   Muncie unsteady fixture's provenance).** HEC ships 1D steady example projects
   with HEC-RAS (the "Steady Examples" set - Beaver Creek, Critical Creek, single
   reaches) that are public-domain and carry a GUI-computed `.p0N.hdf` + `.g0N`
   (`.x0N`) + `.f0N`. Drop a minimal single-reach steady project into
   `services/workers/hecras/fixtures/<name>_steady/` exactly as `muncie_smoke/`
   holds the unsteady deck. This is a FIXTURE-acquisition step, not an engine
   build - the interactive/GUI authoring stays NATE's, as the Muncie deck was.
   (Alternatively, once ONE GUI-authored steady `.p0N.hdf` exists, its HDF group
   structure becomes the diff reference that makes headless synthetic-geometry
   authoring - step 4 - tractable.)

2. **Confirm the steady solve through the rebuilt image.** With a real steady plan
   HDF staged, run `RasGeomPreprocess <plan> <geom>` then `RasSteady <plan> <geom>`
   and gate on the `Finished Steady Flow Simulation` sentinel + a `/Results/Steady`
   group (the steady analogue of the unsteady `_run_engine` Finished gate +
   `/Results` check in `entrypoint.py`). Extend `_KNOWN_MANIFEST_FIELDS` (ADR 0158
   strict parser) with the steady knobs (`analysis="steady"`, `profiles`,
   `flow_regime`, `downstream_bc`) and bump `_PARSER_VERSION`. Rebuild
   `trid3nt-local/hecras:latest` with absolute paths, docker-history provenance
   check (must not reference `/home/nate/Documents/GRACE-2`), behavior-proving smoke
   through the rebuilt image (ADR 0148).

3. **Steady flow authoring is TEXT (the `.f0N` analogue of `deck_edit.py`'s
   `.bNN`).** Author a `scale_steady_flow` / `set_profiles` deck editor over the
   classic `.f0N` ASCII format (`Number of Profiles=`, `Profile Names=`,
   `River Rch & RM=`, the fixed-field discharge line, `Boundary for River Rch &
   Prof#=` / `Up Type` / `Dn Type` / `Dn Slope`). This is the multi-profile sweep
   the App Guide's 6-profile framing needs and the encroachment method block the
   FEMA-floodway row needs (`Section - Encroachment Data` in the `.x0N` +
   `Encroachment_L_R` in the engine).

4. **Synthetic prismatic geometry (mechanism fixture) becomes tractable AFTER
   step 1.** Author a single-reach `.x0N` (prismatic trapezoidal station-elevation,
   uniform slope) using the Muncie `.x0N` format, plus a matching plan HDF geometry
   group DIFFED against the step-1 reference (Cross Sections `Attributes` /
   `Station Elevation Info+Values` / `Manning's n` / `Polyline`; `River Centerlines`;
   `GeomPreprocess` seed). Then `mixed_regime_multi_profile_solve` gets its
   closed-form gate: a prismatic-channel hydraulic jump has the Belanger conjugate-
   depth relation y2/y1 = 0.5(sqrt(1+8*Fr1^2) - 1) - use it as the V&V, rendered as
   a dock-interpreter chart (WSE profile with the jump + Belanger reference in one
   figure, delta in the caption strip, neutral background = synthetic mechanism).

5. **`storage_area_network_flow_reversal` + `modified_puls` remain a SEPARATE,
   larger leg** (1D UNSTEADY network authoring: junctions / storage areas / lateral
   weirs / the Diamond River synthetic fixture). The steady machinery above does NOT
   generalize to them - they need multi-reach junction topology + storage-area
   routing in the plan HDF. STOP; build behind the steady front once a network
   reference deck (again, GUI-seeded) exists.

## Consequences

- Coded-tools metric: 0 tools added, 0 LOC landed (characterization only). Registry
  226 -> 226; `EXPECTED_TEMPLATES` 68 -> 68; no corpus/categories/entrypoint/image
  change.
- Evidence (in-container, `trid3nt-local/hecras:latest`, image id 5d4ac7cfbc8c):
  - `RasGeomPreprocess` text-geometry read proven by the `Muncie.x04` +5 ft
    perturbation -> `XSEC Value` table max |diff| 38015, HDF `Station Elevation
    Values` max |diff| 0.0.
  - `RasSteady` launches and aborts at `Read_siz.for:349` (`forrtl severe (24)`
    EOF, unit 15) on the unsteady Muncie plan HDF - identical with/without a
    preceding Linux geompre and with/without a staged text `Muncie.f04`.
  - Binary confirms mixed-regime (`Computing supercritical profile` /
    `Recomputing to subcritical profile`) + encroachment (`Overbank Encroachment
    Method`) support and the `Finished Steady Flow Simulation` sentinel.
- Offline slice green (72 passed): `test_hecras_landing`,
  `test_hecras_flood2d_template`, `test_deck_edit`, `test_entrypoint`,
  freshtopo `test_freshtopo`, `test_catalog_surfacing` (registry 226),
  `test_door_dissolution` (EXPECTED_TEMPLATES 68).
- The corrected framing for the board: HEC-RAS 1D steady is NOT an engine gap (the
  engines run headless) - it is a **reference-fixture gap**. The unblock is a
  GUI-seeded steady example project (public-domain, HEC-shipped), after which the
  headless reparameterization machinery (proven here) applies, exactly as
  `deck_edit.py` reparameterizes the Muncie unsteady fixture. This is the next real
  build front once the fixture lands.
