# ADR 0171 - HEC-RAS 2D structure-authoring front: HDF schema mapped, STOP on the SA/2D-connection face-pairing frontier + the inert Muncie weir

Date: 2026-08-07
Status: accepted

## Context

The M/L sign-off shortlist (`docs/validation/ml-signoff-shortlist.md`) ranks the
**HEC-RAS structure authoring** front #16, nominally unblocking five board rows:
`multi_opening_flow_split`, `advanced_inline_multi_component`, `gate_pump_rules`,
`1d2d_pump_station`, `breach_param_ensemble`. This job scoped the four
structure-authoring rows the mission named: SA/2D-connection authoring on a FRESH
composed deck (weir-type embankment - centerline / crest profile / weir
coefficient), `simple_breach_geometry_setup` (progressive breach on that authored
connection), `pump_station_trigger_and_ramp_control`, and
`weir_discharge_coefficient_tuning`.

ADR 0157 established the ground truth this front must overcome:
`compose_pure2d_deck` STRIPS `["Structures", "Reference Lines", "2D Flow Area
Break Lines"]` from the copied Muncie plan HDF (`_STRIP_2D_COUPLING`) - so a
freshly-composed deck has NO structures. The only shipped structure capability is
`deck_edit.set_breach_enabled` toggling the SHIPPED Muncie lateral-structure
breach (ADR 0125). This front's charter: author structures on FRESH decks by
reverse-engineering the Muncie HDF structure groups (the diff-driven method that
carried the whole beta arc), STOPping with recipes where the schema resists.

Per the triage-first law, the structure schema was mapped from the real HDF and
probed with real 6.6-engine A/B solves BEFORE any build.

## Schema findings (empirical, against the shipped Muncie plan HDF + `trid3nt-local/hecras:latest`)

### 1. The Muncie structure schema is LATERAL (1D->2D), not a SA/2D CONNECTION

`Geometry/Structures` in `Muncie.p04.tmp.hdf` carries TWO structures, both
`Type = "Lateral"`, `Mode = "Weir/Gate/Culverts"`, keyed to the 1D White River
reach (`River="White" Reach="Muncie" RS=13214 / RS=7300`) with `DS SA/2D = "2D
Interior Area"`. The full HDF layout is now mapped and durable in this ADR:

- `Attributes` (compound, 90 fields): the structure header - `Type`, `Mode`,
  `River`/`Reach`/`RS` (US 1D tie), `US SA/2D`/`DS SA/2D` (2D tie),
  `Weir Width`=20.0, `Weir Coef`=2.0, `Weir Shape`="Broad Crested",
  `Weir Min Elevation`=nan, `Use 2D for Overflow`=**0**, `Culvert Groups`=0,
  `Gate Groups`=0, `LW HW Position`/`LW TW Position`, the Hagers/bridge/pier
  block (all zero), `HTAB *`, `Cell Spacing Near/Far`.
- `Centerline Info` (n,4) + `Centerline Parts` (n,2) + `Centerline Points`
  (70,2 float64): the weir alignment polyline in the model CRS.
- `Profile Data` (8,2 float32): the crest station-elevation profile per
  structure (struct 0: sta 0 el 952.2 -> sta 1010.26 el 952.0).
- `Table Info` (compound, 28 fields): index/count offsets into the profile /
  Manning / rating-curve pools (`Centerline Profile (Index/Count)`, the XS/BR
  profile pools, `RC (Index/Count)`).
- `User Defined Weir Connectivity` (158, compound `SID/HW-TW/RS-FP/Station`):
  the STATION-based HW/TW pairing tying each weir station to a 1D reach station
  (HW) and a floodplain station (TW).

This is a complete, solver-proven schema **for a lateral weir off a real 1D
reach**. It is NOT a schema for a SA/2D CONNECTION between two 2D areas (or a 2D
area and a storage area): a connection is `Type="Connection"` with `US SA/2D` and
`DS SA/2D` both naming 2D/storage areas, and its HW/TW pairing is a CELL-FACE
pairing along the weir line on BOTH meshes - not the station-based
`User Defined Weir Connectivity` a lateral weir carries.

### 2. No SA/2D-connection HDF reference exists anywhere in the repo

The pure-2D Bald Eagle reference `pure2d_reference/BaldEagleDamBrk.x09` carries a
`Storage Area Connection Data` (`Conn 6 "Sayers Dam"`, weir coef 3.1 / width 20),
a `Gate Data` block, and an EMPTY `Pump Station Data` section - but ONLY as ASCII
`.xNN` (a 6.2 combined 1D/2D deck). The committed HDF schema for that geometry
(`pure2d_reference/g09_hdf_schema.json`) captures ONLY
`Geometry/Boundary Condition Lines` + `Geometry/2D Flow Areas` - it does NOT
capture a `Structures` group, and no Bald Eagle `.g09.hdf` is in the repo. So the
2D-2D connection's HDF face-pairing tables (the `SA/2D Connection` cell-face
geometry the engine reads) have NO reference to diff against.

### 3. The SA/2D-connection cell-face pairing IS the RASMapper M3 frontier

Authoring a connection between two 2D meshes requires computing which mesh faces
the weir centerline crosses on each side and writing the paired HW/TW cell-face
tables - the same RASMapper-authored (Windows-DLL) geometry that blocks headless
terrain-subgrid authoring (the ADR 0100/0125 M3 STOP). No shipped reference in
this distribution exposes it. Diff-driven authoring has nothing to diff.

### 4. The Muncie lateral weir is EMPTY of overflow physics - `Use 2D for Overflow`=0

Even setting the connection frontier aside, the ONE working weir we have (the
Muncie lateral structure) cannot demonstrate weir-coefficient discharge tuning.
Three real-engine A/B solves (`trid3nt-local/hecras:latest`, image id
5d4ac7cfbc8c, breach ON, geompre + unsteady, 2026-08-07):

| run | edit | Boundary Flux In (ac-ft) | Volume Ending (ac-ft) | 2D max WSE (ft) |
|---|---|---|---|---|
| baseline | `Weir Coef`=2.0 | 36674.184 | 3498.749 | 951.9266 |
| A | weir-line field-1 (`2` -> `8`, 4x) | 36674.184 | 3498.750 | 951.9266 |
| B | weir-line field-2 (`.98` -> `.40`) | 36674.184 | 3498.750 | 951.9266 |

Both candidate coefficient fields are BYTE-IDENTICAL in flux and 2D max WSE. The
`Attributes` field `Use 2D for Overflow`=0 explains it: the lateral weir's
overflow is NOT routed into the 2D area - the ONLY 1D->2D coupling is the BREACH
opening (which bypasses the crest). So on the shipped Muncie deck the weir
coefficient is INERT: breach ON = flow through the breach hole (coef irrelevant);
breach OFF = 0 wet cells / protected side dry (ADR 0125 - no overtopping into 2D
at all). There is no scenario on this fixture where the weir coefficient
measurably moves water.

## Decision

**STOP all four structure-authoring rows, with recipes.** No structure capability
can be authored on a fresh deck or proven on the shipped deck within the current
surface without crossing the RASMapper face-pairing frontier or shipping a knob
that fails the 0143 must-measurably-move-water rule.

- **`SA/2D connection authoring` (row 1) = STOP.** Recipe: bake a shipped HEC 2D
  project that HAS a SA/2D Area Connection between two 2D areas (the Bald Eagle
  `dam-breach-analysis-with-2d-areas` tutorial is the anchor), extract its
  `Geometry/Structures` group AND its per-connection cell-face pairing tables as
  the HDF reference, then teach `hecras_geometry_writer` a
  `write_sa2d_connection(...)` that emits Attributes (`Type="Connection"`), the
  centerline polyline, the crest `Profile Data`, and - the wall - the HW/TW
  cell-face pairing computed by intersecting the centerline with BOTH meshes'
  faces. The pairing is the RASMapper-authored geometry (M3 STOP); it needs the
  reference project OR a headless face-intersection derivation validated against
  a shipped connection before any authoring is trustworthy.

- **`simple_breach_geometry_setup` (row 2) = STOP (depends on row 1).** A fresh
  progressive breach (crest elevation / bottom width+elevation / formation time)
  is a `Breach Data` block ON an authored SA/2D connection - which does not exist
  until row 1 lands. The breach-RUNS-TO-COMPLETION bring-up INTENT is already
  served GREEN by `hecras_levee_breach` (the shipped Muncie lateral-structure
  breach, ADR 0125). The residual is fresh-connection breach authoring, shared
  with the QUEUED Sayers/Bald Eagle PMF case.

- **`pump_station_trigger_and_ramp_control` (row 3) = STOP.** No pump HDF
  reference exists: Muncie has no pump group (`Gate Groups`=0, no pump in
  `Structures`), and the x09 `Pump Station Data` section is EMPTY. The pump HDF
  schema (on/off trigger elevations, startup ramp, wet-well node) did not yield
  to Muncie-diff or string-dump because there is nothing to dump. Recipe: bake a
  shipped HEC interior-drainage pump project (SA/2D pump-station tutorial),
  extract its pump-group HDF datasets, then author `write_pump_group(...)` with
  the 0143-class rule (the pump must MEASURABLY move water and respect the ramp,
  or error loudly). Needs the reference project first.

- **`weir_discharge_coefficient_tuning` (row 4) = STOP.** Its premise ("cheap
  once authoring exists") is void: authoring does not exist (row 1), and the one
  shipped weir (Muncie lateral) has `Use 2D for Overflow`=0, so its coefficient
  is empirically inert (finding 4). Shipping it would be a knob that cannot move
  water - a 0143 violation. Recipe: it becomes cheap and provable the moment a
  fresh SA/2D connection with weir overflow into 2D is authored (row 1), where a
  per-structure `Weir Coef` A/B measurably changes the flow split.

## Consequences

- **No landing.** No new registered tool, no new template, no corpus/categories
  change: registry stays **226**, `EXPECTED_TEMPLATES` stays **68**. No image
  rebuild (both HEC-RAS images UNCHANGED - the A/B probes ran the shipped
  `trid3nt-local/hecras:latest` unmodified; no executed code touched, so ADR
  0148/0158 image law does not fire). No repo file modified (all A/B ran on
  scratch copies; `Muncie.x04`/`.b04`/plan HDF fixtures pristine - grep + git
  diff verified).
- **Offline slice green (33 + 17 passed):** `test_deck_edit`,
  freshtopo `test_hecras_deck2d`, `test_event_conditions`;
  `test_catalog_surfacing` (registry 226), `test_door_dissolution`
  (EXPECTED_TEMPLATES 68).
- **Baseline canary reproduced:** the Muncie levee-breach solve reproduces ADR
  0125 EXACTLY (vol err 0.005835%, 5765-cell 2D area, 2 breaches active).
- **What this does for the QUEUED rows:** the Sayers/Bald Eagle PMF case and the
  `2d_diffusion_wave_vs_full_swe_regression` M-row both depend on the SAME two
  unbuilt capabilities this front confirms are blocked: (a) fresh SA/2D
  connection authoring (the face-pairing frontier) and (b) fresh breach on that
  connection. Both are gated on a shipped 2D-structure reference project to diff
  against - the identical "reference-fixture gap, not an engine gap" framing ADR
  0170 reached for HEC-RAS 1D steady. The HEC-RAS structure and 1D-network fronts
  are now BOTH characterized as reference-fixture-gated, not machinery-gated.

The HEC-RAS front's remaining board value reduces to ONE unblock: a public,
HEC-shipped 2D project carrying a SA/2D Area Connection (and, separately, an
interior-drainage pump), from which the connection/pump HDF groups + cell-face
pairing become a diffable reference. After that, the diff-driven authoring
machinery applies exactly as `deck_edit.py` reparameterizes the Muncie fixture.
