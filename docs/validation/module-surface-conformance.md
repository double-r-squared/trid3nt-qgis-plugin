# The module surface, read against its spec

Fresh-eyes conformance for `docs/specs/module-surface.html` rev 1b, clause by
clause, produced by a verifier who did not build the wave. Every verdict below
was measured on this box at `b006cf3a`: the five suite slices, the catalog
re-extracted from `trid3nt-local/telemac:latest` and diffed byte for byte
against the committed JSON, twenty slots read back against the raw dico, seven
seeded breaks re-run, and eleven live runs driven through the daemon.

Deviations are REPORTED here and nowhere fixed.

## The clause table

| clause | verdict | evidence | re-verified 2026-09-06 |
| --- | --- | --- | --- |
| **1** The wrapper is the engine's catalog and nothing else that opines; variance lives in templates; every knob reachable at invocation | **DEVIATES** (the carrier wrappers hold no opinion; the two COUPLED wrappers do - see D1) | `modules/telemac2d.py:489`, `modules/artemis.py:146`, `modules/telemac3d.py:293`; against `modules/gaia.py:55,62,106,117` and `modules/waqtel.py:60-67` | **CLOSED**. Both coupled bodies take every constant as an argument (`modules/waqtel.py:36,48`, `modules/gaia.py:45,71,93`); the values are stated where a reader looks - `templates/do_sag/do_sag.py:98-105`, `templates/river_scour/river_scour.py:241-250`, `templates/river_sediment_plume/river_sediment_plume.py:215-218`. Seeded break re-run: `waqtel.o2` returning `BENTHIC_DEMAND=0.0` fails `test_a_coupled_body_states_only_what_its_caller_handed_it`. Live: do_sag 6.7457 mgO2/l at 1195.7 m, scour 5.2132 / 1.6364 mm, sediment_plume 0.6971 - the same numbers the coupled wrappers produced when they held the constants |
| **2** Every keyword is a SLOT with keyword/desc/type/choices/engine default/is_file/level/rubrique | **CONFORMS** | `modules/module.py:51` - the dataclass carries all eight fields; 1,311 slots over six catalogs | - |
| **2** A slot's value comes from exactly one of six sources and PROVENANCE is shown per slot | **DEVIATES** (four provenance values, not six - see D2) | `modules/sheet.py:181` writes `template` / `part <NAME>`; `sheet.py:148` writes `producer <name>`; `sheet.py:130` writes `fill`. No `derived`. Live: the rain-on-grid card's 33 set rows badge `template` (20) and `producer *` (13); zero rows carry no badge | **CLOSED**. `modules/sheet.py:140` badges a measured value `derived`. Live, the rain-on-grid card's 376 rows: `template` 17, `derived` 4 (TITLE, GRAPHIC PRINTOUT PERIOD, LISTING PRINTOUT PERIOD, DURATION), `producer *` 12, `engine default` 318, `open: the dictionary gives it no default` 25 - all six of the spec's sources readable on one card (`part <NAME>` is on the river cards; `test_a_composed_slot_says_which_part_asserted_it`) |
| **2** Resolution order engine default -> parts in order -> template -> fill; at most two opinion layers | **CONFORMS** | `modules/sheet.py:181-208` (parts then body), `sheet.py:130` (fill last); live: `LAW OF BOTTOM FRICTION` 3 (part RIVER) beaten to 4 by a raw fill, one line different in the deck | - |
| **2** A slot not in the catalog refuses by name at import (template) or fill (invocation) | **CONFORMS** | Seeded live: `STEERING asserts 'LAW_OF_BOTOM_FRICTION', which telemac2d has no keyword for. Did you mean LAW_OF_BOTTOM_FRICTION?` at import; on the wire `telemac2d has no keyword 'LAW OF BOTOM FRICTION'. Did you mean 'LAW OF BOTTOM FRICTION'?` | - |
| **2** A value outside the dico's choices refuses naming the choices | **CONFORMS** | Live: `LAW OF BOTTOM FRICTION does not take 9. The dictionary's choices are 0 (NO FRICTION), 1 (HAALAND), 2 (CHEZY), 3 (STRICKLER), 4 (MANNING), 5 (NIKURADSE).` | - |
| **3** One wrapper per module under `telemac/modules/`: telemac2d, telemac3d, artemis, waqtel, gaia | **CONFORMS** | five wrappers, 1,182 lines; `catalog/` carries six JSONs (tomawac extracted, not wrapped - stated in `catalog/README.md`) | - |
| **3** The catalog is dico-derived in-image, committed, drift-audited | **CONFORMS** | re-extracted here through `scripts/extract_telemac_catalog.py`: all six files byte-identical to the committed ones; `tests/test_telemac_catalog_drift.py` green in slice s-z with the image present | - |
| **3** Composites are expanders for repetitive keyword groups, "not a place for defaults" | **DEVIATES** (three choice literals on the carriers - see D3) | `modules/telemac2d.py:184` (`OPTION_FOR_WIND: 1`), `:207` (`PREVIOUS_COMPUTATION_FILE_FORMAT: "SERAFIND"`), `:374` (`RAINFALL_RUNOFF_MODEL: 1`); arming booleans at `:184,:233,:405` and `modules/gaia.py:140` | **CLOSED**. Zero choice literals survive: an independent AST sweep of all five wrappers finds only arming LOGICALs (`telemac2d.py:190,237,410`, `telemac3d.py:296`, `gaia.py:64,87,110,147`) and the shape a body IS (`gaia.py:83,86,105,108,112`). Seeded break fires: `OPTION_FOR_WIND: 1` inside `_wind` fails `test_a_composite_sets_only_what_its_value_s_presence_defines`. GAP FILED: the guard reads `ast.Constant`, so a NEGATIVE literal is invisible to it - seeded `COEFFICIENT_OF_WIND_INFLUENCE: -0.5` in the same composite and the test PASSED |
| **3** Outputs are bindings to the existing readers in `products/` | **CONFORMS** | `modules/telemac2d.py:495`, `gaia.py:161`, `artemis.py:148`, `telemac3d.py:295`; `tests/test_telemac_module_surface.py::test_every_wrapper_binds_its_own_outputs_and_claims_no_other_s` | - |
| **3** WAQTEL is a wrapper too; it binds no output of its own | **CONFORMS** | `modules/waqtel.py` registers none - it writes no result file | - |
| **3** A template names a coupling body whose slots serialize into the coupled steering file; COUPLING WITH lands on the carrier's sheet | **CONFORMS** | `modules/telemac2d.py:283` (`_coupling`), `authoring/serializer.py:59` (a coupled body is filled against its own catalog and joins the one driver call) | - |
| **4** A template package keeps its recipe file, `declarations.py`, `corpus.yaml`; the recipe carries a STEERING body of raw keywords under the image's own spaces-to-underscores map | **CONFORMS** | eight packages under `templates/`; `class STEERING(T2D/ART/T3D)` in each; `modules/module.py:396` `identify()` is the map | - |
| **4** A shared body is a PART a template lists, never a parent it extends | **CONFORMS** | `templates/shared/river.py` is listed as `parts = [RIVER]` by five templates; extending a body refuses naming the fix (`parts = [RIVER]`) - seeded break re-run | - |
| **4** It lives in `templates/shared/`, is named for what it is (never `reach`) | **CONFORMS** | `templates/shared/` holds only `river.py`; zero `reach` as a shared-body name (`helpers/reach.py` is the helper the spec's own section 8 layout lists) | - |
| **4** A shared body must have at least two users (suite-checked) | **CONFORMS** | RIVER: 5 users; seeded a one-user body and `test_every_shared_body_has_at_least_two_users` failed | - |
| **4** A keyword set by two parts REFUSES by name unless the template settles it | **CONFORMS** | seeded live: `BOTH lists A and B, which both set SOLVER; settle it on BOTH or drop one part.`; settling on the template gives provenance `template` | - |
| **4** Bodies are static and read no resolved value | **CONFORMS by construction** (no fireable break - see N1) | `modules/module.py` freezes `ASSERTED` at class creation; two fills of one body leave `ASSERTED` identical | - |
| **4** ONE TEMPLATE PER QUESTION - a structural fork is a template, never a switch on a param | **DEVIATES** (in the deck, yes; in `products/`, a substance switch survives - see D4) | eight templates, each its own STEERING body; against `products/products.py:139,148,454,461` | **CLOSED**. Zero `substance_class` spellings anywhere in the tree; the fork is four named readers each binding its own `SubstanceProduct` (`products/products.py:460,468,487,495`). Residual noted, not a re-opening: `_publish_sediment(erodible: bool)` (`products.py:510`) is one shared body two readers pass a flag to, and the flag gates one metric key |
| **4** Eight templates, routing picks the template | **CONFORMS** | all eight registered and callable: `telemac_river_dye`, `telemac_river_oil_spill`, `telemac_river_scour`, `telemac_river_sediment_plume`, `telemac_do_sag`, `telemac_rain_on_grid`, `artemis_harbor_agitation`, `telemac3d_stratified_flow` | - |
| **4** `Physics(...)` and `Forcing(...)` no longer exist; a forcing series is a file-slot producer | **CONFORMS** | zero live spellings anywhere in `trid3nt_server/`, `workers/`, `plugin/`, `contracts/` | - |
| **5** `fill` is repeatable, sets slots, evaluates producers in Ref order, refuses unknown keywords / bad choices by name | **CONFORMS** | `modules/sheet.py:114` (`fill`), `:222` (`_in_ref_order`, cycle refuses naming the names in it) | - |
| **5** `fill` accepts a validated `keywords={RAW: value}` floor on every wire | **CONFORMS** | `workflow.py:198`; live: `{"LAW OF BOTTOM FRICTION": 4}` and `{"releases": [...]}` both landed | - |
| **5** `fill` returns every filled slot with provenance and every open mandatory slot with desc, choices and engine default | **CONFORMS** | `modules/sheet.py:88` (`state()`); live card carries 376 rows - 33 set with badges, 343 advanced each with its engine default | - |
| **5** Execution is HELD until the user runs; `run` is explicit and on a complete sheet only | **CONFORMS** | `modules/sheet.py:296` refuses first, naming the required files: `telemac2d cannot run: GEOMETRY FILE (...); BOUNDARY CONDITIONS FILE (...)` | - |
| **5** A bare module run is the same sheet with nothing pre-filled | **CONFORMS** | measured: bare telemac2d fill = 0 filled, 29 open, 2 required (the OBLIG files), every other keyword the engine's own | - |
| **5** `describe_keywords` is a read-only lookup over the catalog | **CONFORMS** | `modules/describe.py`; registered in the tool registry | - |
| **6** One serializer function for every module; assign into telapy's `TelemacCas`, write, re-parse; `cas_validate`'s round trip is the choices gate | **CONFORMS** | `authoring/serializer.py` (80 lines), `authoring/cas_validate.py` | - |
| **6** Two measured caveats handled inside the serializer, nowhere else: file keywords through `cas.values[...]`, strings as a `str` subclass whose repr is the engine's form | **DEVIATES** (three caveats, not two - see D5) | `meshers/drivers/telemac_cas_driver.py:43` (`_EngineString`), `:51` (`_EngineLogical`), `:79` (`cas.values[...]`) | **CLOSED**. The spec's section 6 now names all three (`docs/specs/module-surface.html:158`, rev 1c): the file assignment, the `YES`/`NO` logical, the single-quote string |
| **6** Engine defaults are NOT written; the dictionary supplies them | **CONFORMS with two stated exceptions** (see N2) | measured on the live decks: river_dye 38 keywords, ZERO restate a dictionary default; artemis 8 keywords, ZERO; telemac3d 31 keywords, two are the LECDON-forced friction pair (`templates/stratified_flow/stratified_flow.py:134-143`), two are values a producer/param derived that coincide with the default | - |
| **6** There is no second serialization path | **CONFORMS** | seeded a keyword f-string in `products/products.py` and `test_the_serializer_is_the_only_module_that_writes_a_keyword_into_a_deck` failed; zero f-string keyword writers outside the driver on a clean tree | - |
| **7** The author family writers, `_Sheet`, `_DEFAULTS`, `_RECORD_ONLY`, `_consume` die | **CONFORMS** | `authoring/author.py` gone; `authoring/` 4,597 -> 1,500 | - |
| **7** The three JSON-config hops die; artemis/telemac3d become templates | **CONFORMS** | `authoring/{agitation,stratified,open_water}.py` gone | - |
| **7** `workers/telemac/{artemis,telemac3d}_build.py` die; the worker authors nothing | **CONFORMS** | `workers/telemac/` 3,839 -> 954: entrypoint and its test; zero `.cas` authoring in `workers/` | - |
| **7** `shared/physics_registry.py` dies | **CONFORMS** | file gone, 753 -> 0; zero live spellings | - |
| **7** `plan(ops)`, `FormGate`/`DrawGate` as steps, `_PROCESSES`, `_MESH_SHEET_PARAMS`, `_refuse_uncovered_fields` die | **DEVIATES** (dead in code; three stale comment references survive - see D6) | zero live spellings in `trid3nt_server/`, `workers/`, `contracts/`; against `plugin/ui/gate.py:163`, `plugin/ui/dock.py:2409`, `plugin/ui/cards.py:270` | **CLOSED**. Zero `FormGate` / `DrawGate` spellings in `plugin/`, `trid3nt_server/`, `workers/`, `contracts/`, `tests/`, `scripts/` - only frozen validation records name them |
| **7** Expected net of order -7,000 to -8,500 Python across server + worker | **DEVIATES - the promise is MISSED** (see clause 6 of the gate list below) | recomputed from git: **-4,267** over the seven areas the spec named; `docs/validation/module-surface-loc.md` states the same number and names both over-counts | **STILL OPEN, and further from the promise.** Recomputed at HEAD `42b9795e`: **-3,777** over the same seven areas (was -4,267 at the conformance read; the remedy and the lake level added +490 of surface). The spec's rev-1c corrected line still reads -4,267 and is now stale by 490 lines |
| **8** The layout of section 8 | **CONFORMS** | every directory in the spec's tree exists with the files it draws; `helpers/reach.py`, `forcing.py`, `substance.py`, `release_point.py`, `release_layer.py` all homed as shown | - |
| **8** `release_point`/`release_layer` move into the releases composite where they expand keywords | **DEVIATES** (they stayed in `helpers/`) | `helpers/release_point.py`, `helpers/release_layer.py` still exist; the spec's own section 8 tree ALSO lists them under `helpers/`, so the spec contradicts itself here and the tree follows the tree | **STILL OPEN, unchanged.** `helpers/release_point.py` and `helpers/release_layer.py` still exist and the spec's own section 8 tree still lists them there; the spec contradicts itself and nothing in the remedy touched it |
| **8** Every directory-map README rewrites in the same commit | **CONFORMS** | `catalog/README.md`, `modules/README.md`, `templates/README.md`, `shared/README.md`, `authoring/README.md`, `helpers/README.md`, `products/README.md`, `solving/README.md` all present and current | - |
| **9** A new `steering-surface` block set models Catalog, Slot, Module, Composite, Output, Sheet, Serializer with requirements and verify allocations | **CONFORMS** | `docs/model/steering-surface.sysml`: 13 blocks, 9 interface usages, 41 items, 16 requirements, 59 verifications, 0 findings; all six seams green | - |
| **9** Seeded breaks prove each rule fires | **CONFORMS with one exception** (see N1) | six of seven re-run here and fired; `BodiesAreStatic` has no fireable break | - |
| **10** Stage 0 proof matrix: six catalogs, drift audit equal; TelemacCas writes the deck the engine runs; the bare sheet renders | **CONFORMS** | drift equal (measured); river_dye solved to status=ok from the serializer's deck; bare sheet transcript above | - |
| **11** Stage 4: two dye releases fill -> run with full packet; a raw `LAW OF BOTTOM FRICTION=4` fill shown in the deck; a bare telemac2d fill; a bad choice refused naming the choices | **CONFORMS** | all four re-proved live; the two-release deck differs from the baseline in exactly the four source arrays, the raw-keyword deck in exactly one line | - |
| **12** Deck parity: the five canaries' authored decks vs today's, all five solve to status=ok | **NOT RE-VERIFIABLE / BROKEN today** | three of the five river canaries (`river_oil_spill`, `river_scour`, `river_sediment_plume`) refuse at the MESH step on this box - see D7 | **CLOSED.** The guard is deleted (`docs/DELETION_LEDGER.md:3370`); zero `MESH_BED_TWO_DATUMS` spellings in code. All three refusing canaries mesh and solve: `river_oil_spill` 90.7968 mg/L, `river_scour` 5.2132 / 1.6364 mm, `river_sediment_plume` 0.6971 deposited fraction |
| **12** The surface: two releases live; raw keyword live; bare-module sheet; unknown keyword and bad choice refused by name; provenance on every filled slot | **CONFORMS** | six for six, measured above | - |
| **12** Catalog: drift audit green; shared bodies have two users; no body reads a resolved value | **CONFORMS** | measured above | - |
| **12** Model + suite + packets: steering-surface green; seeded breaks fire; five slices zero; full interrogated packets on every acceptance run | **DEVIATES** (model and suite green; two acceptance runs have no packet because they do not solve - see D7, D8) | slices: 1638+4328+1747+1499+521 passed, **0 failed**; packets PASS for river_dye x3, rain_on_grid, agitation_om2d; none for agitation_supplied or stratified_om2d | **CLOSED.** NINE live runs to status=ok through the daemon at HEAD, NINE packets all `verdict: PASS`, `missing: []`, `code_staleness: null`, under `remedy/verify/packets/`. Six models 0 findings (steering-surface: 13 blocks, 19 requirements, 72 verifications). Five slices ZERO failures |
| **13** TOMAWAC: built only if free, else stated absent | **CONFORMS** | extracted, not wrapped, and the reason is written in `catalog/README.md` | - |
| **13** Form-card default view: level-0 set slots shown, everything else under advanced with engine defaults greyed | **CONFORMS** | live card: 33 set + 0 open-mandatory above the fold, 343 advanced each carrying its engine default and the badge `engine default` | - |

## The deviations

**D1. The coupled wrappers hold opinions.** `modules/gaia.py` and
`modules/waqtel.py` assert keyword VALUES from inside their coupled-body
classmethods, and those values are not the dictionary's:

| where | keyword | asserted | dico default |
| --- | --- | ---: | ---: |
| `waqtel.py:60` | WATER SALINITY | 0.0 | 35.0 |
| `waqtel.py:62` | CONSTANT OF NITRIFICATION KINETIC K4 | 0.0 | 0.35 |
| `waqtel.py:67` | BENTHIC DEMAND | 0.0 | 0.1 |
| `waqtel.py:67` | PHOTOSYNTHESIS P | 0.0 | 1.0 |
| `waqtel.py:67` | VEGETAL RESPIRATION R | 0.0 | 0.06 |
| `gaia.py:55` | VARIABLES FOR GRAPHIC PRINTOUTS | "B,E,D50" | 'U;V;H;S;B;R;E' |
| `gaia.py:62` | HIDING FACTOR FORMULA | 1 | 0 |
| `gaia.py:106` | SUSPENSION TRANSPORT FORMULA FOR ALL SANDS | 3 | 1 |
| `gaia.py:108` | SCHEME FOR ADVECTION OF SUSPENDED SEDIMENTS | [1] | [5, 5] |
| `gaia.py:117` | MASS-BALANCE | True | False |

These are physics and output choices, and the ruling puts them in templates.
The suite does not see them: `test_a_wrapper_asserts_nothing_and_has_no_hook_to`
reads `wrapper.ASSERTED`, which is built from a CLASS BODY, and a value returned
from a classmethod never enters it. `waqtel.py`'s own module docstring states the
rule the file breaks: "a body that restated a default would be an opinion wearing
a requirement's clothes".

**D2. The sheet implements four of the spec's six provenance sources.** The table
in section 2 names engine default, shared body, template, fill, producer and
derived. The sheet writes `template`, `part <NAME>`, `producer <name>` and
`fill`; engine default is represented by ABSENCE (correctly - the card's advanced
fold badges it `engine default`), and `derived` does not exist. A value the
assembler measured off the accepted artifact - the normal-depth stage, the
boundary walk, the CFL time step - reads on the card as `template` or
`part RIVER`, not as `derived`. The spec's stated purpose for that row ("nothing
inherited or derived is hidden") is therefore half-served: inherited is visible,
derived is not distinguished.

**D3. Five literal keyword values live in the carrier wrappers' composites.**
`OPTION_FOR_WIND: 1` (`telemac2d.py:184`) restates the dictionary's own default,
which the ruling says stays unwritten. `PREVIOUS_COMPUTATION_FILE_FORMAT:
"SERAFIND"` (`:207`) is a choice among three where the dictionary answers
`SERAFIN` - reasoned in the docstring, but a wrapper's choice.
`RAINFALL_RUNOFF_MODEL: 1` (`:374`) picks the CN model out of four. The arming
booleans beside them (`WIND`, `RAIN_OR_EVAPORATION`, `FRICTION_DATA`, `NESTOR`)
are the composite's own presence said in the engine's words and read as
mechanical rather than as opinion.

**D4. A substance switch survives in `products/`.** `products.py:139`
(`_substance_product`) dispatches on a `substance_class` string, and `:454`/`:461`
branch `if substance_class == "sediment" / elif == "oil"`. The string is a
template-declared constant rather than a param, and the fork is in the READING
path rather than the deck - but it is a substance switch, and one-template-per-
question was ruled to hold "at EVERY level".

**D5. The serializer carries three measured caveats, not the spec's two.**
`_EngineLogical` (`telemac_cas_driver.py:51`) spells a boolean `YES`/`NO`,
because Python's `repr(True)` is `True` and DAMOCLES does not answer to it. It is
the same class of fix as the apostrophe caveat and belongs in the spec's
section 6 list.

**D6. Three stale `FormGate` comment references in the plugin.**
`plugin/ui/gate.py:163`, `plugin/ui/dock.py:2409`, `plugin/ui/cards.py:270` name
a concept that no longer exists. The mechanism they describe (the param sheet as
an editable card) survived by ruling; the NAME did not.

**D7. Three of the eight templates cannot solve on this box: a bed-datum guard
false-positives on a single outlier node.** `telemac_river_oil_spill`,
`telemac_river_scour` and `telemac_river_sediment_plume` all refuse at the mesh
step with the same measured message:

```
MESH_BED_TWO_DATUMS: the bed painted from bed raster supplied directly ...
holds two populations: 906 node(s) over 13.00 m to 26.30 m and 1 node(s)
over 44.95 m to 44.95 m, with 18.65 m of nothing between them against
13.30 m of spread inside them.
```

`workflows/mesh/shared/primitives.py:250` (`_refuse_two_datums`) cuts at the
LARGEST gap in the sorted node elevations and compares that gap to the summed
spread of the two sides. A lone spike gives the high side a spread of zero, so
the test reduces to "is the outlier further than the whole terrain range" - which
one node in 907 satisfies. The guard's own docstring says it is looking for "two
clouds"; its refusal message prints `1 node(s)` and refuses anyway. The same
reach at 14 m edge (the keyword-floor driver) meshes and solves, so the guard is
also mesh-resolution dependent. Reported, not fixed.

**D8. Two of the three open-water acceptance arms fail live.**

- `agitation_supplied` - ARTEMIS stops in FRONT2: `ERROR AT BOUNDARY POINT 204 /
  LOCAL NUMBER 1358 / SOLID POINT BETWEEN TWO LIQUID POINTS`. The supplied-mesh
  arm's boundary stamping (`modules/artemis.py:87`, `stamp_boundary_rows`)
  leaves a lone solid node inside the designated liquid stretch. `agitation_om2d`
  on the same AOI and the same structure solves to status=ok.
- `stratified_om2d` - TELEMAC-3D stops in MURD3D: `ITERATION NO. REACHED 100,
  STOP. ALFA = 4.947E-3 GUILTY POINT = 9878`, after ~700 repetitions of
  `UNKNOWN OPTION IN MURD3D_POS: 4 / OPTION 1 TAKEN INSTEAD / FOR THE KEYWORD:
  SCHEME OPTION FOR ADVECTION OF TRACERS / AVAILABLE OPTIONS: 1 AND 2`. The
  template states `SCHEME FOR ADVECTION OF TRACERS = 13` and leaves SCHEME OPTION
  open, which is correct under the open-set ruling; the engine's own compiled
  default for that keyword is 4, which its own Fortran rejects. Both arms landed
  on 2026-09-06 and neither has a recorded packet: the newest proof in
  `docs/proof/templates/{artemis_harbor_agitation,telemac3d_stratified_flow}/` is
  2026-09-03, before the om2d and supplied-mesh commits.

## Notes that are not deviations

**N1. `BodiesAreStatic` has no fireable seeded break.** A body is read at import,
before any sheet exists, so "a body reading a resolved value" cannot be written
in the real import order; what can be written is a body computing off a sheet
built earlier in the same file, and that is indistinguishable from a literal. The
property is held by construction (`ASSERTED` is frozen at class creation) and
guarded by `test_a_body_is_static_and_no_fill_changes_what_it_asserts`, which was
re-run and holds. Six of the seven seeded breaks fired.

**N2. Two engine defaults ARE written, and both are stated.** The telemac3d
friction pair (`LAW OF BOTTOM FRICTION = 5`, `FRICTION COEFFICIENT FOR THE
BOTTOM = 0.01`) restates the dictionary because LECDON refuses the deck when
only one half is written - measured both ways and written down at
`templates/stratified_flow/stratified_flow.py:134-143`. `TIME STEP = 1.0` and
`MAXIMUM NUMBER OF ITERATIONS FOR ADVECTION SCHEMES = 50` are a derived value and
a param default that happen to equal the dictionary's; neither is a literal
restating a default.

**N3. Twenty slots read back against the raw dico.** A random sample of 20 slots
across the six catalogs was compared to the dico blocks pulled out of the image:
type, TAILLE, NIVEAU, DEFAUT1, CHOIX1, SUBMIT and the de-LaTeXed AIDE1 agree on
every one that a naive block splitter could find (18 of 20; the other two were
the splitter's limitation, not a mismatch). An independent parse of the dico
recovers exactly 376 / 355 / 91 keywords for telemac2d / telemac3d / waqtel,
matching the committed catalogs.

**N4. Cross-panel interrogation of the packets that PASS.**

- river_dye two releases: two distinct plumes at the two source points, legend
  0-101.4 against the reported `dye_cmax_mgl` 101.42. Picture and scalars agree.
- river_dye raw keyword: legend 0-75.87 against `dye_cmax_mgl` 75.87, and the
  plume is a stationary blob (`plume_reach_m` 1.2 against the baseline's 65.3) -
  a Strickler coefficient of 33 read as a Manning n. The discrimination test
  passes: the raw keyword changed the physics, visibly.
- rain_on_grid: outlet peak 4.23 m3/s, continuity 5.3e-15. The animation frames
  carry the wireframe; the max-depth COG does not (it is the published product).
  Two observations worth a reader's eye, neither a spec deviation: the headline
  `max_depth_peak_m` 3.558 m is a SINGLE-CELL spike, not a channel depth - the
  basin is a sheet at 0.05-0.2 m; and the published max-depth COG carries
  clustered circular NODATA voids where the animation frames paint ~1e-3 m, so
  the product's wet mask and the frame renderer's disagree.
- The acceptance evidence JSON records a refusal's `step_state` but not its
  message (`step_error` came back empty for both refusal arms). The message is
  correct and complete on the tool's own envelope and in the daemon log; it is
  the driver's `form_card`/step projection that drops it, together with each
  card row's `desc` and `group`. An evidence file that cannot show the sentence
  the surface is judged on is worth widening.

## The LOC promise, recomputed

Recomputed from git at `09eea8b2` (before) and `b006cf3a` (after), `wc -l` over
tracked `.py`:

| area | before | after | delta |
| --- | ---: | ---: | ---: |
| `telemac/authoring/` | 4,597 | 1,500 | -3,097 |
| `workflows/runtime/` | 6,742 | 5,658 | -1,084 |
| `telemac/workflow.py` | 382 | 349 | -33 |
| `telemac/modules/` | 0 | 2,172 | +2,172 |
| templates (the five pre-wave packages -> the eight) | 3,038 | 4,451 | +1,413 |
| `workers/telemac/` | 3,839 | 954 | -2,885 |
| `shared/physics_registry.py` | 753 | 0 | -753 |
| **total** | **19,351** | **15,084** | **-4,267** |

Whole-tree at the same two commits: `trid3nt_server/` +706, `workers/` -2,885,
so server + worker is **-2,179**; `tests/` -1,341.

**The spec's -7,000 to -8,500 expectation was MISSED.** The measured number over
the seven areas the spec itself named is **-4,267**, about 55% of the low end of
the promise. `docs/validation/module-surface-loc.md` states the same number and
names both places the arithmetic was optimistic - the rerun machinery that was
ruled to survive in `runtime/`, and the 3,585 lines of new surface
(`modules/` + the templates' growth) the spec's table carried no line for. The
ledger is honest and this verification reproduces it exactly.

## Re-verified 2026-09-06, at HEAD `42b9795e`

A second fresh-eyes pass, refuting by default, after the conformance remedy
(`f4e752b5..d02fe7fe`) and the lake-level remedy (`ee55cfab..42b9795e`). Nothing
here is carried from the remedy's own report: every number below was measured on
this box, on a daemon restarted at HEAD (`ws_smoke.py` all_passed=True) with a
clean tree.

**D1 D2 D3 D4 D5 D6 D7 D8 are CLOSED. Two deviations stand: the LOC promise
(clause 7) and the `release_point`/`release_layer` homing (clause 8), and both
stand by ruling rather than by omission.** The per-clause disposition is the
fourth column of the table above.

### The nine canaries, live through the daemon

Nine runs, nine packets, every packet `verdict: PASS` with `missing: []` and
`code_staleness: null`, under
`module-surface/remedy/verify/packets/`. `stratified_om2d` SOLVES for the
first time: the MURD3D iteration stop is gone, because the basin now opens at
the level its own gauge reads rather than at the chart datum.

| run | run id | the answer | packet |
| --- | --- | --- | :---: |
| `telemac_river_dye` | `01M1X9DC40WHHRGMSB2MGMS29J` | 91.0358 mg/L peak, plume 39.9 m | PASS |
| `telemac_river_oil_spill` | `01M1X9EER6X9PV8D1M9HT511F3` | 90.7968 mg/L dissolved, plume 40.2 m | PASS |
| `telemac_river_scour` | `01M1X9FD86HCED18XCSEGQHFXR` | 5.2132 mm scour / 1.6364 mm deposition | PASS |
| `telemac_river_sediment_plume` | `01M1X9GD51SANK57VM3WB76BNM` | 0.6971 deposited fraction, 66.92 kg | PASS |
| `telemac_do_sag` | `01M1X9HEQMGMMV68QVXE43R1DK` | 6.7457 mgO2/l at 1195.7 m | PASS |
| `telemac_rain_on_grid` | `01M1X9MMXXKRV6CP0HQQ5NRSWY` | 4.2293 m3/s peak, continuity 5.33e-15 | PASS |
| `agitation_om2d` | `01M1X9Q78A8RR9YE45RJ1S5K4V` | Kd 0.083 sheltered / 0.727 exposed | PASS |
| `agitation_supplied` | `01M1X9QWW1HF0TMEFVXX3GRH1X` | Kd 0.076 sheltered / 0.853 exposed | PASS |
| `stratified_om2d` | `01M1X9RXW4TE1CPBWWN28SAHW8` | 0.1516 degC surviving over 13 planes | PASS |

Eight of the nine reproduce the remedy's numbers to the LAST DIGIT, which is
what makes D1-D6 physics-neutral rather than merely green. The ninth is
`river_oil_spill` - see F1.

### Seeded breaks, fired and restored

| seed | test | result |
| --- | --- | --- |
| `OPTION_FOR_WIND: 1` inside `telemac2d._wind` | `test_a_composite_sets_only_what_its_value_s_presence_defines` | FIRED: "telemac2d.py:191 _wind sets OPTION FOR WIND to 1" |
| `waqtel.o2` returning `BENTHIC_DEMAND=0.0` instead of its argument | `test_a_coupled_body_states_only_what_its_caller_handed_it` | FIRED: "waqtel.o2 states BENTHIC DEMAND = 0.0" |
| `COEFFICIENT_OF_WIND_INFLUENCE: -0.5` inside `telemac2d._wind` | `test_a_composite_sets_only_what_its_value_s_presence_defines` | DID NOT FIRE - see F2 |

The tree was restored after each and `git status` is clean.

### The grep gates

| gate | result |
| --- | --- |
| `MESH_BED_TWO_DATUMS` / `_refuse_two_datums` | zero in code; the surviving hits are the deletion ledger, the reanalyze ledger, this file and a differently-named open-water test |
| `substance_class` branches in `products/` | zero `substance_class` spellings anywhere in the tree |
| `FormGate` / `DrawGate` | zero in `plugin/`, `trid3nt_server/`, `workers/`, `contracts/`, `tests/`, `scripts/` |
| choice literals in composites | zero, by the suite test and by an independent AST sweep of all five wrappers (arming LOGICALs and the shape a body IS are all that remain) |
| `Physics(` `Forcing(` `_PROCESSES` `_MESH_SHEET_PARAMS` `_refuse_uncovered_fields` `physics_registry` `_setter_envelope` | zero each |

### The models, and the suite

Six models, 0 findings each; `steering-surface.sysml` is 13 blocks, 9 interface
usages, 41 items, 19 requirements, 72 verifications (16/59 at the conformance
read). Five slices, ZERO failures: 1638 / 4357 / 1748 / 1507 / 521.

### The LOC promise, recomputed at HEAD

Same seven areas, same method, `09eea8b2` to `42b9795e`:

| area | before | after | delta |
| --- | ---: | ---: | ---: |
| `telemac/authoring/` | 4,597 | 1,545 | -3,052 |
| `workflows/runtime/` | 6,742 | 5,658 | -1,084 |
| `telemac/workflow.py` | 382 | 349 | -33 |
| `telemac/modules/` | 0 | 2,219 | +2,219 |
| templates | 3,038 | 4,849 | +1,811 |
| `workers/telemac/` | 3,839 | 954 | -2,885 |
| `shared/physics_registry.py` | 753 | 0 | -753 |
| **total** | **19,351** | **15,574** | **-3,777** |

**The honest number is -3,777.** The spec's corrected line (rev 1c) reads
-4,267, which was true at the conformance read and is now stale by 490 lines:
the remedy added +235 and the lake level +255, all of it surface a reader looks
at. The promise of -7,000 to -8,500 is MISSED at about 54% of its low end, and
this pass moves it further from the promise, not closer.
`docs/validation/module-surface-loc.md` carried 4,847 for the templates row
where the tree reads 4,849; that row and its totals are corrected there.

### Findings, filed rather than fixed

**F1. `river_oil_spill` does not reproduce; the other eight do.** Two runs of
the same canary at the same HEAD on the same pinned discharge give
`dye_cmax_mgl` 90.7772 then 90.7968 and `plume_reach_m` 39.9 then 40.2, on a
bit-identical mesh (10.415 m). Every other canary is identical to the last
digit, including the dye run the oil deck is a fork of, so the variation is the
oil arm's own: the dissolved field this canary reports is fed by the drogue
model, and 100 drogues are not placed deterministically. Nothing is wrong with
the number; what is wrong is reading it as a fixed baseline. The canary's
headline needs a tolerance, or the drogue seed needs stating.

**F2. The composite-literal guard cannot see a negative number.**
`test_a_composite_sets_only_what_its_value_s_presence_defines` matches
`ast.Constant`, and `-0.5` in Python source is `ast.UnaryOp(USub,
Constant(0.5))`. Seeded `COEFFICIENT_OF_WIND_INFLUENCE: -0.5` into `_wind` and
the test PASSED. No negative literal exists in a composite today - the rule
holds - but its seeded-break proof only covers half the number line. The
coupled-body guard has no such hole: it compares RUNTIME values, which is why
`CLASSES SETTLING VELOCITIES = [-9.0]` in `gaia.suspended` is visible to it (and
exempt: it is the dictionary's own default sized to this body's one class).

**F3. The stratified packet's standing caveat contradicts the run it rides on
three counts.** `PACKET_NOTES[("telemac3d_stratified_flow", "coarse")]` says
"3000 m HORIZONTAL over a one-hour window", asks "whether a CALM column keeps
its thermocline", and points the reader at "the refined declaration (1000 m at
the same 13 planes)". The acceptance arm is 60 m asked / 32.94 m measured, five
hours, and a prescribed 6 m/s wind whose whole purpose is to mix the column.
The run's own numbers say so: the profile opens 8.801 degC at the bed against
18.0 at the surface and ends 16.975 against 17.126. Reported at the remedy and
still standing.

**F4. The conservation error bar does not reach the evidence.**
`column_heat_drift_frac` is measured (`products/stratified.py:142`), logged and
journaled - and is not among the metrics the run persists, so the acceptance
driver asks for it and reads `null`. It is the one number that separates wind
mixing from numerical diffusion, on the one run whose answer is that a 9.2 degC
column became a 0.15 degC column in five hours. `stratification_dt_init`,
`column_heat_mean_init_c`, `column_heat_mean_final_c` and `column_depth_m` are
dropped with it. `boundary_states` is the same shape of hole on the agitation
arms: the driver reads it off `metrics`, where it never lands.

**F5. One style preset's NAME rides onto the layer it is not named for.** The
bottom-temperature panel captions `style=Surface temperature (degC)` and the
vertical-profile chart titles and axis-labels the same words over a
bed-to-surface column. The two rasters ARE distinct (679,061 of 3,424,200 pixels
differ) and sharing one scale is correct; only the name drifts. Reported at the
remedy and still standing.

**F6. The bed-evolution raster and its own headline are 6x apart.**
`telemac_river_scour`'s published panel legends -0.8427 to +0.8427 mm on a
percentile stretch while `max_scour_mm` is 5.2132: the maximum is one saturated
bullseye at the point source, ringed by its deposition annulus, and everything a
reader can see is the +/-0.84 band. Same class as the rain-on-grid single-cell
peak the notes already carry, and unlike rain-on-grid this one has no note.

**F7. The agitation maximum is on the domain's own rim.** `kd_max` 3.667 (om2d)
and 4.184 (supplied) sit in a bright striped band along the seaward edge of the
domain - a standing wave against the boundary, not a harbour answer. The
sheltering pair (0.083 / 0.727 and 0.076 / 0.853) is what the question asked
for. Reported at the remedy and still standing.

**F8. `test_provider_config_empty_values_do_not_clobber` is not hermetic.** It
monkeypatches `MODEL_PROVIDER` and `TRID3NT_OPENAI_API_KEY` and then reads
`TRID3NT_OPENAI_BASE_URL` from the ambient environment. This box's `.env.local`
sets it to an OpenRouter endpoint, so a slice run after `source .env.local` -
which is how every live driver on this box is invoked - fails it with a 400
demanding a namespaced `vendor/model`. Measured both ways: red with the file
sourced, green with `TRID3NT_OPENAI_BASE_URL` unset and green under `env -i`.
The offline suite's own invocation does not source it, so the ZERO above stands;
the test still asserts against whatever the box happens to export.

**F9. The canvas view frames the whole of Lake Superior.** `Input: nhd
waterbodies` is emitted at its full-feature extent, so the stratified panels
report being "32220x closer than the canvas extent". Pre-existing global-bbox
behaviour; unchanged.
