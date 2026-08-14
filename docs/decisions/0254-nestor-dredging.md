# ADR 0254 - NESTOR channel-maintenance dredging (dig/dump on the GAIA erodible bed)

Date: 2026-08-13
Status: accepted
Continues: ADR 0253 (NESTOR STOP-recipe - deck-authoring was the sole gap),
ADR 0240/0216 (GAIA v3/v2 sediment), ADR 0158 (strict parser gate).

## Context

ADR 0253 resolved that NESTOR's compiled libraries (`libnestor4{api,telemac2d,
telemac3d}.so`) and keyword family are ALREADY baked in `trid3nt-local/telemac:
latest` (v9.0 gaia/telemac2d dicos), so the blocker was purely deck-authoring:
the worker authored GAIA sediment but ZERO NESTOR action/polygon/coupling
emitters. Four board rows waited on it: `channel_maintenance_dredging`,
`dredge_spoil_disposal_placement`, `critical_elevation_triggered_dig_dump`, and
(considered) `reservoir_siltation_flushing_1d`.

The risk was the SnapWave precedent (ADR 0238/0243): an unverifiable file grammar
defeats blind authoring. Here it did NOT - the full NESTOR fortran source ships
in-image (`/opt/conda/opentelemac/sources/nestor/`), so the action-file grammar
was pinned against the COMPILED PARSER the baked `.so` builds from, stronger than
any example deck.

## Grammar pinned in-image (make-or-break)

Read from the in-image source (authoritative, not guessed):

- `readdigactions.f` - action file: `ACTION`..`ENDACTION` blocks, `/` comments,
  `ENDFILE` terminator, top-level `RESTART`; `KeyWord = value` lines
  (`ParseSteerLine` splits on `=`). Keywords: `ActionType`
  (Dig_by_time/Dump_by_time/Dig_by_criterion/Reset_bottom/Save_water_level/
  Backfill_to_level), `FieldDig`/`FieldDump`, `TimeStart`/`TimeEnd`/`TimeRepeat`,
  `DigVolume`/`DigRate`/`DigDepth`/`CritDepth`/`MinVolume`/`MinVolumeRadius`,
  `DumpRate`/`DumpVolume`, `GrainClass`, `ReferenceLevel`.
- `isactioncompletelydefined.f` - per-ActionType required fields.
- `readpolygons.f` - polygon file: `NAME <id>_name` + `x y` vertex lines,
  bare `ENDFILE` terminator.
- `set_by_profiles_values_for.f` - surface reference file: `>= 2` profiles
  `x1 y1 z1 x2 y2 z2 km`, `END` terminator; interpolates refZ + km per node.
- `datestringtoseconds.f` - dates `yyyy.mm.dd-hh:mm:ss` (exactly 19 chars),
  seconds since `ORIGINAL DATE/HOUR OF TIME`.
- `threedigitsnumeral.f`, `nestor_interface.f` (gaia), `dig_by_criterion.f`,
  `dig_by_time.f`, `initialisenestor.f` - semantics + coupling.

Five format subtleties an IN-IMAGE DIRECT SOLVE exposed one at a time (each a
crash a guessed format would have hit):

1. field/polygon names need a 3-numeral prefix whose FIRST digit is 1-9
   (`ThreeDigitsNumeral` rejects a leading 0) -> ids `101_channel`/`102_spoil`.
2. `RESTART` is read as a Fortran LOGICAL -> `F`, never DAMOCLES `NO`.
3. the polygon file MUST end with a bare `ENDFILE` (no trailing blanks).
4. the NESTOR SURFACE REFERENCE FILE is MANDATORY in BOTH modes, not just
   criterion: `Write_Node_Info` logs each dug/dumped node's km chainage via
   `Set_by_Profiles_Values_for`, which hard-errors on its absence. The profiles
   must bracket EVERY field (dig AND dump) -> a reach-spanning profile fence.
5. criterion mode uses `ReferenceLevel = SECTIONS` (the profile-interpolated
   design grade), never `GRID` (which demands a gridded ZRL NESTOR lacks here).

NESTOR is enabled at the GAIA level (`NESTOR : YES` + `NESTOR ACTION/POLYGON/
SURFACE REFERENCE FILE` in the gaia.cas), NOT via `COUPLING WITH` (whose dico
choice list excludes NESTOR). It requires a real erodible bed stock (it digs ZF
through the GAIA active layer) and non-cohesive sand (NSAND==NSICLA) -> dredging
FORCES the GAIA v2 erodible-bed path + the sediment class.

## Decision

**All 3 dredging rows LANDED as ONE knob on `telemac_river_dye`** (0 new coded
tools, registry stays 254, EXPECTED_TEMPLATES unchanged - the ADR 0240 knob-fold
precedent). Both rows collapse into modes of one template because they are the
same question class (an engineered dig/dump rule on the morphodynamic bed):

- Worker (`telemac_river_dye_build.py`): `write_nestor_action_file` +
  `write_nestor_polygon_file` + `write_nestor_surface_ref_file` +
  `write_nestor_decks` (zone resolver: explicit UTM polygons or a channel-
  spanning box built from the centerline). `write_gaia_deck` gains the NESTOR
  keyword block on the erodible branch; `author_deck` stamps a deterministic
  `ORIGINAL DATE OF TIME` so action-file absolute dates map to sim seconds.
  Modes: `scheduled` (Dig_by_time, +optional Dump_by_time disposal) and
  `criterion` (Dig_by_criterion to the design grade).
- Composer (`river_dye.py`): `dredging`/`dredge_mode`/`dredge_volume_m3`/
  `dredge_disposal`/`dredge_crit_depth_m`/`dredge_dig_depth_m` knobs, auto-armed
  from DREDGE_KEYWORDS (dredge / maintenance dredging / spoil / shoaling /
  silting), forcing erodible_bed=True (-> sediment class via the single-source
  gate). Zone geometry + volumes/rates = labeled-default engineering (input-
  review gate); the worker builds a mid-reach channel box by default.

**`reservoir_siltation_flushing_1d` - NOT folded (stays CAND).** Flushing is
HYDRAULIC remobilization via a reservoir drawdown boundary condition, not a
mechanical dig/dump; different machinery (multi-year GAIA accumulation + a
stage-drawdown outflow BC).

## Verification (offline build session)

- Grammar pinned against in-image compiled fortran (above). Parser bump
  `telemac-reach-8` -> `telemac-reach-9` (strict unknown-field gate, ADR 0158).
- Worker image REBUILT (absolute `-f`/context from repo root); baked provenance
  confirmed `_PARSER_VERSION = telemac-reach-9` + NESTOR authors present (not
  the mounted copy).
- **Discriminating pair proven THROUGH the rebuilt image** (no code mount), real-
  CRS synthetic navigable reach, direct GAIA+NESTOR solve:
  - `scheduled` (channel_maintenance_dredging): OFF zone mean bed change
    **-0.001 m** (0 digs) vs ON **-0.529 m** (74 XdigX; the 4000 m3 dig), both
    CORRECT END.
  - `criterion` (critical_elevation_triggered_dig_dump): 53 digs, zone
    **-0.74 m** to the design grade, CORRECT END.
  - `scheduled+disposal` (dredge_spoil_disposal_placement): 74 digs + 71 XdumX
    dumps, zone **-0.40 m**, spoil placed **+0.43 m** in the disposal zone,
    CORRECT END.
- Worker unit tests `test_nestor_dredging.py` (7, grammar pins) +
  `test_gaia_erodible.py` (8): green.
- Server: AST + import clean; `test_run_river_dye_scenario.py` 29 passed / 2
  network-gated baseline failures (the river_dye [p-r] baseline, NWM streamflow
  offline -> TELEMAC_DISCHARGE_INPUT_REQUIRED); `test_catalog_surfacing.py` +
  `test_door_dissolution.py` 17 passed (registry 254, EXPECTED_TEMPLATES
  unchanged). Corpus queries added.
- Proof: `docs/proof/templates/telemac_river_dye_nestor_dredging_proof.png`
  (dredge ON vs OFF bed-evolution, mesh wireframe + dredge-zone overlay).

## Consequences / honesty floor

- Registry 254, EXPECTED_TEMPLATES unchanged; dredging is a knob like scour /
  gradation / wind, not a new template.
- REAL-REACH LIVE E2E through the composer is BLOCKED OFFLINE on this box: the
  carrier-discharge NWM streamflow fetch returns TELEMAC_DISCHARGE_INPUT_REQUIRED
  without network (identical to the river_dye offline baseline). The FORMAT,
  physics, coupling, and dredge-ON/OFF discriminating pair are proven in-image on
  a real-CRS synthetic reach; a real navigable-reach live drive (Mississippi /
  Atchafalaya) is queued for an online session (explicit discharge_m3s bypasses
  the block if NWM coverage is thin).
- Planning-grade, not calibrated: MORFAC amplifies morphology; the dredge zone /
  volume / rate are labeled demo defaults (no dredging-record fetcher); the
  design grade auto-resolves from the mean bed over the dig zone.
- The retrieval index could not be rebuilt offline (no embedding backend;
  `retrieve_visible_tools` fails open to the full registry), so ranked
  surfacing of the dredging corpus is queued for an online check.
