# ADR 0172 - HEC-RAS reference-fixture seed: real public decks in hand for both fronts; the missing-plan-HDF wall generalizes and survives

Date: 2026-08-07
Status: accepted

## Context

ADR 0170 (1D steady / RasSteady front) and ADR 0171 (2D structure-authoring
front) both STOPped on the SAME diagnosis: no public HEC-RAS example project
in the repo carries the reference artifact each front needs to diff against
(a GUI-computed steady-typed plan HDF; a GUI-computed SA/2D or 2D-2D
`Type="Connection"` structure HDF). Both ADRs converged on the identical
recipe: fetch HEC's public "Example Projects" and seed them as fixtures.
This job is that single unblock for both fronts, plus the pump-station
board rows both ADRs also gated on a reference that did not exist.

## What was fetched

- HEC's official "HEC-RAS 7.0 Example Projects" bundle -- linked directly
  from the HEC-RAS download page
  (`https://www.hec.usace.army.mil/software/hec-ras/download.aspx`, section
  "HEC-RAS 7.0 Example Projects": *"This file contains all of the HEC-RAS
  example projects."*, 408 MB).
- Direct URL (HEC's own distribution host -- the `hec-downloads` GitHub org
  is where HEC has hosted installers/examples since the USACE site stopped
  serving large binaries directly; this is the SAME hosting pattern already
  relied on for the `muncie_smoke` fixture's `Linux_RAS_v66.zip` and the
  `hecras2025` worker's beta zip):
  `https://github.com/HydrologicEngineeringCenter/hec-downloads/releases/download/1.0.45/Example_Projects_7_0.zip`
- Fetched with `curl -L -A "Mozilla/5.0 ... TRID3NT-fixture-fetch/1.0
  (contact: natealmanza3@gmail.com)"` -- no access controls encountered, no
  scraping-around anything; the URL was read directly off the official
  download page's HTML.
- Downloaded size 427,304,097 bytes (matches the server's `Content-Length`
  exactly). SHA-256 `fac04f071e624c841b20e943e3b68f351b4531f565a0c24ab7d885cf9e38d523`
  (verified post-download).
- Public domain: HEC-RAS + its example projects are U.S. Federal Government
  work, freely redistributable (HEC's own terms, already relied on for
  `muncie_smoke`).
- The zip's top-level categories: `1D Sediment Transport`, `1D Steady Flow
  Hydraulics`, `1D Unsteady Flow Hydraulics`, `2D Sediment Transport`,
  `2D Unsteady Flow Hydraulics`, `Applications Guide`, `Pipes`.

## What was seeded (`services/workers/hecras/fixtures/`, one subdir each)

| fixture | source folder | seeded size | ADR front |
| --- | --- | --- | --- |
| `beaver_creek_steady/` | `Applications Guide/Example 2 - Beaver Creek/` | 596 KB | 0170 (steady) |
| `critical_creek_steady/` | `Applications Guide/Example 1 - Critical Creek/` | 342 KB | 0170 (steady) |
| `baldeagle_connection/` | `2D Unsteady Flow Hydraulics/BaldEagleCrkMulti2D/` (8.4 MB of a ~350 MB source folder -- see that fixture's `PROVENANCE.md` for the full trim table) | 8.4 MB | 0171 (2D connection) |
| `pump_station/` | `1D Unsteady Flow Hydraulics/Pumping Station/` + `.../Pumping Station with Rules/` | 1.0 MB | 0171 (pump) |

Total seeded: ~10.6 MB of a 427 MB zip. Each fixture directory carries its
own `PROVENANCE.md` (source URL, SHA-256, license, exact trim rationale)
and `schema_notes.md` (the VERIFY results + HDF/ASCII schema dump below).
The zip itself was NOT committed (staged under a scratch dir outside the
repo, deleted after extraction -- mirrors how `Linux_RAS_v66.zip` was
handled for `muncie_smoke`).

## VERIFY: the empirical gate (docker run against `trid3nt-local/hecras:latest`, image id `5d4ac7cfbc8c`)

Every seeded fixture was run headless through the SAME image the existing
`muncie_riverine_flood`/`muncie_levee_breach` archetypes use, on scratch
copies (fixtures verified pristine afterward via `git status`). The single,
consistent, and load-bearing finding across ALL FOUR fixtures:

### The wall: `RasGeomPreprocess`'s modern CLI requires a `File Type="HEC-RAS Results"` (plan-level) HDF; a bare geometry HDF or no HDF at all is silently rejected into a legacy fallback

```
$ RasGeomPreprocess <any seeded fixture's geometry file or a nonexistent-path stand-in> <suffix>

forrtl: No such file or directory
forrtl: severe (29): file not found, unit 5, file /data/io.x
  htabopen_ (Htabopen.for:107) <- MAIN__ (Htab.for:33)
```

This is IDENTICAL to the program's behavior with **zero** command-line
arguments -- confirmed by running `RasGeomPreprocess` bare. Root-caused
precisely (new this job, not established by ADR 0170/0171): `h5py` on
Muncie's WORKING plan HDF shows root attribute `File Type = "HEC-RAS
Results"` (top-level groups `Plan Data` / `Event Conditions` / `Geometry`);
every seeded fixture's `.g0N.hdf` (Beaver Creek, BaldEagle) shows `File
Type = "HEC-RAS Geometry"` (a single top-level `Geometry` group, no `Plan
Data`, no `Event Conditions`). Given a file lacking the `Plan Data` shape,
`RasGeomPreprocess` does not error cleanly against the geometry it WAS
given -- it discards argv entirely and falls into a different, legacy
Fortran code path (`Htabopen.for`) that expects an interactive/batch
control file (`io.x`) neither we nor HEC's own Linux run scripts ever
provide. This reproduces on THREE independent, structurally different
example projects (1D steady Beaver Creek, 2D-connection BaldEagle, pump
stations with no HDF at all) -- it is a property of what HEC's public
Windows-GUI example distribution ships (processed GEOMETRY, never a
processed PLAN), not a quirk of any one project.

**Exhaustive check across the entire 1D steady category:** every `.hdf` in
`1D Steady Flow Hydraulics/` (Baxter, Chapter 4, ConSpan Culvert, Mixed Flow
Regime Channel, Beaver Creek -- Wailupe GeoRAS ships none) was opened and
its root `File Type` attribute checked. **All are `"HEC-RAS Geometry"`; none
is `"HEC-RAS Results"`.** No public HEC-RAS 1D steady example anywhere in
this 408 MB bundle ships a computed steady plan HDF. This closes out ADR
0170's step-1 recipe with a definitive negative: the recipe's premise (a
public download would carry the needed artifact) does not hold.

### `RasSteady`: two distinct, now-separated failure modes

- Against an EXISTING but wrong-shaped HDF (`BEAVCREK.g01.hdf`, geometry-
  only): reproduces ADR 0170's exact abort, same routine chain, same line --
  `read_siz_is_post_` (`Read_siz.for:349`), `end-of-file during read, unit
  15`. Now confirmed on a SECOND, independent input (not just Muncie's
  unsteady-typed plan HDF) -- strengthens ADR 0170's conclusion that this is
  a missing-artifact-class problem, not an input-specific quirk.
- Against a NONEXISTENT path (`critical_creek_steady`, which ships no HDF
  at all): a clean `File: ... Not Found`, exit 0, no crash. A genuinely new,
  useful distinction: RasSteady's error behavior differs between "you never
  staged a plan HDF" and "the plan HDF you staged is not steady-shaped".

### The one alternate `RasSteady` invocation tested: NOT a general steady solve

HEC's own `Linux_RAS_v66/Muncie/run_steady.sh` + the bundled
`RAS_v.6.6_Linux.pdf` slide deck document `RasSteady Muncie.r04` (a ONE-arg
call on a `.rNN` RESTART file, not the two-arg `<plan_hdf> <geom_suffix>`
CLI). Run end-to-end on a scratch Muncie copy (RasGeomPreprocess ->
RasUnsteady -> `RasSteady Muncie.r04`): it EXECUTES successfully (exit 0,
produces `Muncie.O04`, 14 MB) but the sentinel it prints is **`Finished Post
Processing`**, not ADR 0170's target `Finished Steady Flow Simulation`. Per
the doc, this leg "Requires the RAS Unsteady Flow computed first" -- it is a
restart-snapshot POST-PROCESSOR (likely a rating-curve/max-WS export at each
saved timestep), not an independent 1D steady-network solve. It does not
apply to a from-scratch steady deck (Beaver Creek/Critical Creek have no
`.rNN` file -- that artifact is only produced BY an unsteady run). Documented
for completeness; does not unblock the target board rows
(`mixed_regime_multi_profile_solve`, `steady_floodway_encroachment`), which
need genuine multi-profile `.fNN` steady decks solved independently.

### `baldeagle_connection`: the schema is real; the cell-face pairing question is narrowed, not resolved

`Geometry/Structures/Attributes` in both `g01.hdf` and `g11.hdf` carries
genuine `Type=b'Connection'` rows (the class ADR 0171 found completely
absent -- Muncie's only structure is `Type=b'Lateral'`). Highlights (full
tables in `baldeagle_connection/schema_notes.md`):

- `g01`: 4 connections on ONE 2D Flow Area (`BaldEagleCr`) + ONE Storage
  Area (`Reservoir Pool`) -- a genuine SA-to-2D dam connection (`Weir
  Shape=Ogee`, `Weir Coef=3.82`, **`Use 2D for Overflow=1`**) plus 3
  internal-levee connections. Every structure here has overflow ROUTED into
  the 2D mesh -- unlike Muncie's one inert weir (ADR 0171 finding 4, `Use 2D
  for Overflow=0`), so a `Weir Coef` A/B on this fixture is not pre-doomed
  to be byte-identical.
- `g11`: independently confirmed via `Geometry/2D Flow Areas` enumeration to
  carry TWO distinct named 2D Flow Areas (`BaldEagleCr`, `Upper 2D Area`)
  joined by ONE connection -- the genuine 2D-mesh-to-2D-mesh topology ADR
  0171 called "the frontier". Same Ogee/3.82/1-gate-group config as g01's
  dam connection -- a clean SA-2D vs. 2D-2D diff pair (same physical
  structure, two different upstream-area types).
- Searched exhaustively: no static HW/TW cell-face index table exists
  anywhere in either HDF (unlike Muncie's LATERAL structure, which has a
  station-based `User Defined Weir Connectivity` table). The underlying
  mesh face geometry to intersect a connection's centerline against IS
  present and real (`Faces Cell Indexes`, `FacePoints Coordinate`, etc.).
  Whether the pairing is Linux-computable by `RasGeomPreprocess` could NOT
  be tested -- both files hit the SAME missing-plan-HDF wall as the steady
  fixtures (this project ships no `p01.hdf`/`p02.hdf`/`p18.hdf` either).
- Breach parameters for these connection-type structures live in the PLAN
  TEXT file (`Breach Loc=`/`Breach Geom=`/`Breach Start=`/`Breach
  Progression=` blocks in `BaldEagleDamBrk.p01`), not the boundary/unsteady
  file the way Muncie's lateral-structure breach does (`deck_edit.
  set_breach_enabled` targets Muncie's `.bNN`) -- a structurally different
  authoring surface, flagged for whoever builds the row-2 authoring front.

### `pump_station`: real ASCII schema, both static and rule-driven

No HDF ships with either pump project (pre-HDF vintage). Full ASCII
transcripts in `pump_station/schema_notes.md`:

- `Pumps.g01`: a static `Pump Station=`/`Pump Station Group Pump=<name>,
  <WSEL On>,<WSEL Off>` schema + a real head-discharge `Pump Station Group
  HQ=` curve (6 points, monotonic 300 cfs -> 0 cfs).
- `PumpRule.u02`: a genuine `Rule Operation=`/`Rule Expression=` trigger
  script -- Pump #1 gated on a companion inline gate's open/close state,
  dynamically resetting Pump #2/#3's WSEL-On elevations -- the real grammar
  the `gate_pump_rules` / `pump_station_trigger_and_ramp_control` rows need.

## Decision

**Fixtures seeded, provenance + schema documented, VERIFY run honestly to
its conclusion. No registry/template/entrypoint/image change** (mirrors ADR
0170/0171: characterization work, not a landing). `entrypoint.py`'s
`_BAKED_DECKS` and `_KNOWN_MANIFEST_FIELDS` are untouched; registry and
`EXPECTED_TEMPLATES` counts are unaffected by this job.

The mission's central empirical question -- "does a genuine steady project's
plan preprocess into a valid steady plan HDF on our Linux image" -- has a
definitive answer: **untestable with any public HEC-RAS example project**,
because none ships the GUI-computed plan-level HDF `RasGeomPreprocess`
requires to do ANYTHING (steady or otherwise) with a fresh geometry. This
reframes both fronts' remaining blocker one level deeper than ADR
0170/0171 stated it:

- NOT "no steady-typed reference exists" (ADR 0170) -- refined to: no
  PLAN-level HDF of any type exists for ANY public 1D steady project, so
  `RasGeomPreprocess` cannot even be pointed at one.
- NOT "no SA/2D-connection HDF reference exists" (ADR 0171) -- refined to:
  the reference NOW EXISTS (this job's `baldeagle_connection` fixture, a
  real `Type="Connection"` schema with active overflow), but it is
  similarly gated on the SAME missing-plan-HDF problem for any headless
  compute/verification step.

## The recipe, corrected

The unblock is no longer "find a public reference deck" (done, this job).
It is: **construct a minimal `File Type="HEC-RAS Results"` plan-HDF
skeleton** (top-level `Plan Data` + `Event Conditions` + a `Geometry` group
populated from the seeded `.g0N`/`.g0N.hdf` reference) that
`RasGeomPreprocess` will recognize and operate on. Two paths, neither
attempted here (out of this job's characterization-only scope):

1. **Reverse-engineer the `Plan Data`/`Event Conditions` schema** from
   Muncie's WORKING plan HDF (already fully mapped, `Geometry/Structures`
   schema durable in ADR 0171) as the template, transplanting a seeded
   fixture's real `Geometry` subtree in place of Muncie's. This is
   mechanical HDF surgery (h5py group copy), not physics authoring -- a
   meaningfully SMALLER lift than ADR 0170's original "multi-day, comparable
   to the 2D-authoring arc" estimate, because the `Geometry` payload no
   longer needs to be synthesized (it is real, GUI-computed, seeded by this
   job) -- only the thin `Plan Data`/`Event Conditions` wrapper does.
2. **A genuine Windows HEC-RAS GUI compute session** (out of scope --
   no Windows/GUI available in this environment) to produce ONE authentic
   plan HDF (steady, and separately a fresh-connection unsteady run) that
   becomes the wrapper template for (1), removing even the schema-guessing
   risk. Flagged as a NATE-only step (mirrors the `muncie_smoke` fixture's
   original GUI-seeded provenance) if (1) proves intractable blind.

## Consequences

- Coded-tools metric: 0 tools added, 0 LOC in `services/workers/hecras/*.py`
  changed (fixture seed + docs only). Registry/`EXPECTED_TEMPLATES`
  unaffected.
- 4 new fixture directories (~10.6 MB total), each with `PROVENANCE.md` +
  `schema_notes.md`; no zip committed.
- `entrypoint.py` untouched; `_BAKED_DECKS` does not yet reference any new
  fixture (none can compute yet -- see Decision above). A future job wiring
  a `write_plan_hdf_skeleton(...)` helper would be the natural next step,
  not scoped here.
- Regression: `test_hecras_landing.py` + `test_deck_edit.py` run green,
  foreground, `env -u TRID3NT_CACHE_BUCKET` (see report for the exact
  count) -- proves this job did not disturb the existing Muncie-backed
  archetypes.
- Both ADR 0170 and ADR 0171's boards remain STOPped -- this job does not
  reopen either row -- but both now have (a) real public reference decks in
  hand, (b) a single, precisely-characterized, SHARED remaining blocker
  (the plan-HDF skeleton), and (c) a corrected, smaller-scope recipe (HDF
  surgery on a known-working template, not blind schema reconstruction)
  for whoever picks up either front next.
