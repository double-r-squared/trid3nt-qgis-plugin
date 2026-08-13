# ADR 0174 - HEC-RAS SA/2D connection gate CRACKED: the g09 mesh seeded, the Sayers Dam connection solves with nonzero weir flow, and the weir coefficient moves water

Date: 2026-08-07
Status: accepted

## Context

ADR 0173 partitioned the stalled HEC-RAS 2D-connection front (ADR 0171) to a
single, precisely-named data gap: the shipped preprocessor deck
`hecras2025/subst/crux/pure2d_reference/BaldEagleDamBrk.x09` carries a complete
`Section - Storage Area Connection Data` for the Sayers Dam weir (weir coef 3.1,
HW/TW cell-face pairing arrays), but had **no matching `g09.hdf` mesh** to number
against, and the seeded `g01.hdf` had no matching `.xNN`. ADR 0173 named the fix
(path a): obtain the 11 MB `g09.hdf` from the same public example the `x09` came
from, build a plan-HDF skeleton around it, and the shipped `x09` pairing + a
recognized plan HDF form a consistent deck `RasUnsteady` can solve. This job
executed that path end to end against the real 6.6 Linux engines
(`trid3nt-local/hecras:latest`, image id `5d4ac7cfbc8c`). **Both halves of the
gate fell.**

## What was fetched (the missing mesh) and how the numbering was verified

`BaldEagleDamBrk.g09.hdf` (11,690,739 B) + its GUI text `BaldEagleDamBrk.g09` were
pulled from **`Example_Projects_6_6.zip`** (release tag `1.0.33`, whole-zip
432,389,121 B / sha `ea239b50...`) -- the SAME distribution the `x09`/`b06`/`p06`
came from (ADR 0132/0133), NOT the `7_0` zip the `g01`/`g11` fixtures came from (a
7.0 mesh could renumber cells/faces vs the 6.2-authored `x09`). The pull was a
**partial HTTP-Range extraction** (zip central directory -> the two members'
local headers + deflate streams), each member verified against the zip's own
**CRC-32** manifest entry -- no 432 MB download (the tmpfs/disk-headroom
directive). Seeded into `services/workers/hecras/fixtures/baldeagle_connection/`
with `PROVENANCE.md` updated.

Numbering-match evidence (ADR 0174 GATE-1):

- **Provenance:** same zip, same `BaldEagleDamBrk` project, same geometry index 09
  -- `x09` is by construction `g09`'s preprocessor output.
- **Structural identity:** `g09.hdf` `Geometry/Structures/Attributes` has ONE
  structure, `Type="Connection"` `Connection="Sayers Dam"` `Weir Coef=3.1`
  `Weir Width=80` `Gate Groups=1` `Use 2D for Overflow=1` -- byte-for-byte the
  `x09` `Conn 6 Sayers Dam` block.
- **Range:** every `x09` HW/TW face index (max 17779) and facepoint index
  (max ~19340) falls inside `g09`'s 37594 faces / 19529 facepoints.
- The `.xNN` preprocessor enumerates faces differently from the `.gNN.hdf` display
  face-array rows, so pairing self-consistency is proven by the SOLVE, not by row
  identity (below).

## What was proven CONSTRUCTIVELY: the connection solves with nonzero weir flow

Deck assembly (`build_sayers_connection_deck.py`, a host-side reference builder):
`build_skeleton` (ADR 0173) wraps g09's geometry -- Structures KEPT, unlike the
pure-2D fresh decks that strip them -- in a Results-typed plan HDF;
`hecras_event_conditions` (ADR 0138) authors the 2D-BC-line forcing (an inflow
hydrograph on the real `Upstream Inflow` BC line, normal-depth outflows on
`DSNormalDepth`/`DS2NormalD`) that wets the mesh; `patch_chippewa_bnn(initial_stage)`
seeds the 2D area's initial water surface at 688 ft so the reservoir impounds above
the 683 ft Sayers Dam crest and overtops the connection from t=0 (a dry-startup
pool needs 125,000 acre-ft / 30+ h even at 50k cfs to reach the crest -- infeasible
in a capped window, so the impounded IC is the physically-correct Sayers scenario).
The shipped `x09` is used verbatim; `b09` is the Chippewa-form inert fake-reach
placeholder (the 6.6-correct `.bNN`, ADR 0137).

Driven through `trid3nt-local/hecras:latest`:

| leg | result |
| --- | --- |
| `RasGeomPreprocess p09.hdf x09` | exit 0, `Finished Processing Geometry` |
| `RasUnsteady p09.hdf x09` | exit 0, `Finished Unsteady Flow Simulation` |
| Volume accounting | Error Percent **0.0006%** (2.03M acre-ft impounded pool drains to 0.13M) |

The connection output group `Results/.../2D Flow Areas/BaldEagleCr/2D Hyd Conn/
Sayers Dam/Structure Variables` (cols: Total Flow, Weir Flow, Stage HW, Stage TW,
Total Gate Flow) carries **nonzero weir flow through the Sayers Dam connection**:
peak ~299,845 cfs, mean ~11,800 cfs, min ~400 cfs -- ALL of it Weir Flow (Total
Gate Flow = 0, gate un-operated), HW 660-688 ft over the 683 ft crest draining to
TW 589-688 ft. **This is the first SA/2D `Type="Connection"` structure ever solved
in the repo with flow across it.** The x09 HW/TW face pairing + the g09 mesh are
self-consistent by the solve: had they numbered different meshes, the preprocessor
would have rejected the connection or the solve would have diverged; it finished
with 0.0006% mass balance.

## What RESOLVES ADR 0171's inert-weir epilogue: the weir coefficient MOVES water

ADR 0171 found Muncie's one shipped weir provably inert (byte-identical A/B on a 4x
coefficient change) because it has `Use 2D for Overflow=0`. The Sayers Dam
connection has `Use 2D for Overflow=1`. A weir-discharge A/B on the LIVE connection
(coef patched in BOTH the `x09` connection block and the plan-HDF `Weir Coef`
attribute, all else identical, same 34-day window):

| weir coef Cw | full-window mean flow (cfs) | early high-head weir-controlled mean (cfs) |
| --- | --- | --- |
| 2.0 | 11,256 | 262 |
| 3.1 (default) | 11,798 | 406 |
| 4.0 | 11,567 | 524 |

The connection is emphatically NOT inert -- ~300k cfs peak / ~11.8k cfs sustained
crosses it (vs Muncie's byte-identical A/B). The full-window mean is drainage-limited
(all three empty the same finite 2.03M-acre-ft pool over 34 days, so it is NOT
monotonic in Cw), but the **early high-head phase, where flow is weir-equation-
controlled, scales cleanly and monotonically with Cw: 262 / 406 / 524 cfs for
Cw 2.0 / 3.1 / 4.0 -- +100% for a 2x coefficient (textbook weir Q proportional to
Cw)**. The peak (~299,845 cfs) is identical across Cw: it is the t=0 impounded-
release transient (flux-limited, not weir-controlled), so it is NOT the A/B signal.
**Verdict: on a `Use 2D for Overflow=1` connection the weir coefficient moves water
-- the ADR 0171 inert verdict was a property of Muncie's overflow=0 weir, not of
connections in general. The 0143 must-move-water rule is satisfied.**

## Decision

**GATE 2 (ADR 0173) is discharged: the connection solves.** The durable landing is
a reference builder + solver + the seeded mesh + offline tests -- the same
non-registered form the pure-2D reference decks (`build_freshtopo_deck.py`,
`build_chippewa_fakereach_deck.py`) and ADR 0173's skeleton builder take. No
agent-facing tool or template is registered: wiring the connection deck as a baked
`_BAKED_DECKS` archetype in `entrypoint.py` (+ a workflow template + corpus queries
+ an image rebuild under the ADR 0148/0158 law) is a distinct integration lift and
is the clear next job now that the solve is proven. Per the honesty floor, registry
stays **226** and `EXPECTED_TEMPLATES` stays **68**; no `entrypoint.py`/`_BAKED_DECKS`/
manifest change, so no worker code executes the builder and the ADR 0148 image law
does not fire. No corpus/categories change.

## Consequences

- Coded-tools metric: **0 registered tools, 0 templates** added; registry
  226 -> 226, `EXPECTED_TEMPLATES` 68 -> 68. New durable artifacts: the seeded
  `g09.hdf`/`g09` fixture (+ `PROVENANCE.md`), `build_sayers_connection_deck.py`
  (~150 LOC reference builder), `solve_sayers_connection.py` (in-container solver),
  `test_sayers_connection_deck.py` (offline).
- Evidence (in-container, image id `5d4ac7cfbc8c`): both engines `Finished`; vol
  error 0.0006%; connection Weir Flow nonzero (peak ~300k, mean ~11.8k cfs, gate
  flow 0); the weir-coef A/B (Cw 2.0/3.1/4.0) monotonic in the weir-controlled
  drainage phase.
- Proofs (`docs/proof/templates/`): `hecras_sayers_dam_connection.png` (max
  inundation depth over Esri, the red Sayers Dam weir line + white 2D-area
  perimeter, real PA-North georeference), `..._mesh.png` (the g09 37594-face
  wireframe, separate), `..._hydrograph.png` (the connection weir-flow hydrograph),
  `hecras_sayers_dam_weir_coef_ab_chart.png` (the Cw A/B overlay + delta).
- Offline slice green (`env -u TRID3NT_CACHE_BUCKET ... --timeout=300 -q`):
  `test_sayers_connection_deck` (new, 3), + hecras + freshtopo + fixtures
  `test_plan_hdf_skeleton` + the pin tests (`test_catalog_surfacing` registry 226,
  `test_door_dissolution` EXPECTED_TEMPLATES 68).
- The HEC-RAS connection front is no longer blocked. What remains for a future job:
  (a) the agent-facing template wiring (baked deck + archetype + image rebuild);
  (b) the plan-TEXT breach-on-connection authoring surface (ADR 0173: p01's `Breach
  Geom=`/`Breach Start=` block set, distinct from `deck_edit.set_breach_enabled`'s
  `.bNN` lateral breach) -- not attempted here; (c) tightening the run window (the
  Chippewa `b09` `Computational Time Window` -- 34 days -- currently governs over
  the plan-HDF window; a `b09` window patch would trim solve time).
