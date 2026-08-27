# Skeleton campaign - LOC ledger

A running, comparable line count for the workflow-skeleton campaign. It exists
to answer one question per wave: **is the generalization actually absorbing
code, or are we bloating?** Every wave adds its rows and a one-line honest
verdict.

## How the numbers are produced (use this EXACTLY, every wave)

    # a landed wave, at its commit:
    wc -l <paths>

    # the pre-wave baseline, without stashing:
    for f in <paths>; do printf "%s %s\n" "$(git show <ref>:$f | wc -l)" "$f"; done

`wc -l` counts PHYSICAL lines: blanks and comments INCLUDED. That is
deliberate - docstrings and constraint comments are part of what a reader has
to hold, and excluding them would let a wave "shrink" by deleting the
explanation. Counts are PRODUCT `.py` only - never tests, never docs, never
scripts. (Wave 2 added `tests/test_workflow_skeleton.py`, 230 lines; wave 2b added
179 more there plus 55 in `tests/test_telemac_do_sag.py`. None are counted here,
by that rule.)

The four surfaces every row splits into:

| surface | what it holds | what growth here MEANS |
|---|---|---|
| lib skeleton | `trid3nt_server/workflows/lib/*.py` | FIXED cost, paid once for the whole fleet |
| engine facade + family steps | `trid3nt_server/workflows/<engine>/workflow.py` + `<engine>/steps/*.py` | PER-ENGINE cost, paid once per engine |
| shared steps | `trid3nt_server/workflows/shared/*.py` (only files the wave touched) | FIXED cost, shared across engines |
| template files | the migrated templates' own `.py` | PER-TEMPLATE - this is the number that has to fall |
| deleted outright | files removed | pure absorption |

A SIMILARITY claim about two functions is a number like any other and gets the
same treatment. The recipe, so a "these are basically the same" reading can be
checked rather than asserted:

    # extract each function by AST span, normalize (strip indent, drop blanks)
    # FOUR-WAY  = |multiset intersection of all N line lists| / mean(len)
    # PAIRWISE  = difflib.SequenceMatcher(None, lines_a, lines_b).ratio()
    # CEILING   = wc -l of the whole candidate span, blanks and docstrings in

Normalization matters and must be stated with the number: the same four
publishers read 24% four-way on raw physical lines, 25% stripped-and-blank-
dropped, 31% stripped-with-blanks-kept. This ledger quotes the STRIPPED,
BLANK-DROPPED figure, because a shared blank line is not shared code.

Two surfaces the wave-2 rows forgot, added here so no wave forgets them again:
**adapters/runtime** (a wave that has to change `trid3nt_server/adapters/` or any
other server module to land) and **fleet adaptations** (product templates the
wave did not migrate but had to touch to keep operable). Both are product `.py`
and both count. A THIRD surface the wave-2c rows had to add: **worker code**
(`workers/<engine>/*.py`) - it is product `.py`, it is inside the reconciliation
command's scope, and a fix that has to change what the worker WRITES lands there
and nowhere else. The check that catches an omission: `git diff --stat
<baseline> -- '*.py'` over the whole tree must reconcile with the sum of the
wave's rows.

## Ledger

A remediation wave touches a handful of files across a surface rather than
rebuilding it, so the 2b rows report the TOUCHED files' before/after and do not
continue the wave-2 surface totals. The DELTA and the running net are what carry
across; the reconciliation check above is what proves nothing was dropped.

RUNNING-NET CORRECTION (2026-08-25, found by the wave-3 review panel): six of the
seven wave-3 running-net CELLS did not carry their own deltas. The column diverged
from the delta column at four separate steps - `+1716` was added as `+1710`,
`+333` as `+288`, `-28` as `-106`, and `-92` as `+31` - and those errors partly
cancelled, which is why the published close, `+1402`, looked only 6 off instead of
obviously broken. The DELTAS were right the whole time: they sum to +663, which is
exactly what the reconciliation command at the foot of this file measures. The
wave-3 close is therefore **+1408** (`745 + 663`), and every cell above is now
recomputed from its delta. Nothing about the verdict changes; the number the next
wave is measured against does.

WAVE 2c is the panel-remediation wave that follows wave 3 CHRONOLOGICALLY. The
letter continues the 2/2b remediation naming (a wave that fixes rather than
migrates), not the wave-3 migration sequence, and its running net continues from
wave 3's +1408.

WAVE-A INSERTION AND FULL RE-CHAIN (2026-08-26). Wave A had a verdict in this file
but no ROWS: its counting note claimed to be "exactly the sum of the seven wave-A
rows above" against rows that were never written. The seven rows are now measured
and inserted, and they sit between the last 2c row (`0ff5231f`, 2026-08-25 02:12)
and the first wave-B row (`33e879cf`, 2026-08-25 23:19) because wave A's own range
is `09e4d734..f76234c3` (18:56 to 21:16 the same day) - after every 2c commit and
before every B commit. Six of the eight wave-B rows did not reproduce at their own
pinned commits and are corrected here from git. Because seven rows joined the table
mid-column, EVERY running-net cell from wave A down has been recomputed as a true
running sum of the delta column: wave A now closes at +2036 (not the +2162 its
verdict claimed), wave B at +2855 (it had no verdict at all) and wave C at +3899
(not +2974). Nothing before wave A moved.

| date | wave | surface | LOC before | LOC after | delta | running net |
|---|---|---|---|---|---|---|
| 2026-08-24 | 0 - baseline (ref `8304c289`) | lib skeleton (14 files) | 3944 | - | - | - |
| 2026-08-24 | 0 - baseline | telemac family steps (9 files, no facade) | 2874 | - | - | - |
| 2026-08-24 | 0 - baseline | shared steps (`shared/aoi.py` did not exist) | 0 | - | - | - |
| 2026-08-24 | 0 - baseline | cohort templates (`do_sag.py` 399 + `do_sag/steps.py` 278 + `river_dye.py` 698) | 1375 | - | - | - |
| 2026-08-24 | 2 - skeleton + cohort | lib skeleton (16 files; `workflow.py` 354 + `slots.py` 112 new, `plan.py` +30, `params.py` +19, `interpreter.py` +5, `__init__.py` +12) | 3944 | 4476 | +532 | +532 |
| 2026-08-24 | 2 - skeleton + cohort | telemac facade + family steps (`workflow.py` 184 + `water_quality.py` 77 new; `forcing.py` +72, `substance.py` +26, `solve.py` +22, `reach.py` +13, `__init__.py` +17, `deck.py` +1, `products.py` -24) | 2874 | 3262 | +388 | +920 |
| 2026-08-24 | 2 - skeleton + cohort | shared steps (`shared/aoi.py`) | 0 | 68 | +68 | +988 |
| 2026-08-24 | 2 - skeleton + cohort | cohort templates (`do_sag.py` 399->324, `river_dye.py` 698->530) | 1097 | 854 | -243 | +745 |
| 2026-08-24 | 2 - skeleton + cohort | deleted outright (`do_sag/steps.py`) | 278 | 0 | -278 | +467 |
| 2026-08-24 | 2 - CORRECTION (found by the wave-2 adversarial panel) | product lines the wave-2 rows missed: the `_normalize_callable_for_gemini` signature re-stamp (`adapters/adapter.py` +12) and the four non-migrated templates' mechanical `Plan(...)` / chart-function adaptations (+8) | 0 | 20 | +20 | **+487** |
| 2026-08-24 | 2b - panel remediation | lib skeleton (the 5 files touched, not the whole 16-file surface: `workflow.py` +106 must-fill + coercion triage + provenance arity + explicit solve step, `params.py` +15 wire-type refusal, `plan.py` +3, `__init__.py` +3, `slots.py` -1 corridor fields out) | 1270 | 1396 | +126 | +613 |
| 2026-08-24 | 2b - panel remediation | telemac facade + family steps (the 5 files touched: `workflow.py` +58 CorridorPolicy + MeshHandle + required-coverage check, `reach.py` +30 MeshSizing + cap narration, `products.py` +21 mesh-override provenance, `deck.py` +9, `mesh_preview.py` +3) | 1867 | 1988 | +121 | +734 |
| 2026-08-24 | 2b - panel remediation | shared steps (`shared/aoi.py` - required `code_prefix` + its rationale) | 68 | 74 | +6 | +740 |
| 2026-08-24 | 2b - panel remediation | cohort templates (`do_sag.py` +5, `river_dye.py` +5 - the corridor slot and the mesh provenance row each cost a line and buy the placement and the honesty) | 854 | 864 | +10 | +750 |
| 2026-08-24 | 2b - panel remediation | fleet adaptations (3 orphaned `_STEPS` constants deleted) | 1209 | 1204 | -5 | **+745** |
| 2026-08-25 | 3 - TELEMAC family (ref `04ab6b1c`) | migrated template files (`coastal_tidal_surge` 848 -> 350, `wave_field` 695 -> 341, `agitation` 840 -> 341, `stratified_flow` 737 -> 343; each = the template + its new sibling coercion + `__init__`) | 3120 | 1375 | -1745 | -1000 |
| 2026-08-25 | 3 - TELEMAC family | TELEMAC facade + family steps (`workflow.py` +139 the `_PROCESSES` routing table, `steps/__init__.py` +35, and the five NEW step modules: `open_water.py` 301 shared by all four, `coastal.py` 269, `wave.py` 286, `agitation.py` 404, `stratified.py` 282) | 326 | 2042 | +1716 | +716 |
| 2026-08-25 | 3 - TELEMAC family | shared steps (`shared/aoi.py` +117 the AOI acquire step, `shared/tide_series.py` 216 the CO-OPS forcing resolver) | 74 | 407 | +333 | +1049 |
| 2026-08-25 | 3 - TELEMAC family | lib skeleton (the CONSTANT-door wire enforcement: `workflow.py` +41 `_wire_params` + the factory note, `docstring.py` +6) | 554 | 601 | +47 | +1096 |
| 2026-08-25 | 3 - TELEMAC family | adapters/runtime (`postprocess_telemac.py` +38 the shared `_local_mesh_origin` + the four call sites, `contracts/telemac_contracts.py` +27 the curve/profile answer fields, `testing/live_run.py` +31 the dispatch-card verdict, `testing/canaries.py` 336 the declared canary registry incl. the six refined-mesh variants) | 3309 | 3741 | +432 | +1528 |
| 2026-08-25 | 3 - TELEMAC family | fleet adaptations (the cohort's now-dead `TEMPLATE_CARD` / `QUESTION` decoration stripped, its prose moved into the module docstrings) | 864 | 836 | -28 | +1500 |
| 2026-08-25 | 3 - TELEMAC family | deleted outright (`telemac/_bed_input.py` 62, `telemac/_template_card.py` 30) | 92 | 0 | -92 | **+1408** |
| 2026-08-25 | 2c - panel remediation | adapters/runtime (`server/protocol/catalog_http.py` - the provider-config coherence gate that refuses an incoherent provider/model pair BEFORE any env mutation), commit `a88e455e` | 2362 | 2499 | +137 | +1545 |
| 2026-08-25 | 2c - panel remediation | adapters/runtime (`catalog_http.py` +26 / `tools/search/search_tools.py` +33 - `_read_corpus_yaml` REFUSES a non-string corpus entry instead of dropping it), commit `01572c97` | 3877 | 3936 | +59 | +1604 |
| 2026-08-25 | 2c - panel remediation | template files (4 TELEMAC coercion siblings + `modflow/regional_water_budget`) - a coercion ABSTAINS when its argument is absent, commit `72f32acd` | 494 | 524 | +30 | +1634 |
| 2026-08-25 | 2c - panel remediation | telemac family steps (`steps/solve.py`, `steps/substance.py` - same abstain fix), commit `72f32acd` | 581 | 597 | +16 | +1650 |
| 2026-08-25 | 2c - panel remediation | shared steps (`shared/publish_product_layer.py` NEW - `_publish_peak_layer` promoted under an honest name), commit `b24feb64` | 0 | 54 | +54 | +1704 |
| 2026-08-25 | 2c - panel remediation | TELEMAC facade + family steps (`open_water.py` +26 the `requires_utm` per-deck flag + `solved_domain_bbox` + sign-checked `mesh_sizing_provenance`, `workflow.py` +4 required physics selector, the four step modules +21 and `steps/__init__.py` +1 onto the promoted publisher), commit `b24feb64` | 2042 | 2094 | +52 | +1756 |
| 2026-08-25 | 2c - panel remediation | template files (`do_sag.py`, `river_dye.py` - one import line each onto the collapsed `read_run_metrics`), commit `b24feb64` | 836 | 836 | +0 | +1756 |
| 2026-08-25 | 2c - panel remediation | adapters/runtime (`testing/canaries.py` +37 the products-not-just-a-turn gate + the idealized canary, `postprocess_telemac.py` +15 the three copies of the origin arithmetic collapsed into one), commit `b24feb64` | 2775 | 2827 | +52 | +1808 |
| 2026-08-25 | 2c - panel remediation | worker code (`workers/telemac/telemac_coastal_build.py`, `workers/telemac/tomawac_build.py` - each echoes the bbox it actually meshed), commit `b24feb64` | 1379 | 1387 | +8 | +1816 |
| 2026-08-25 | 2c - cleanup phase 1 | fleet adaptations (`contracts/{case_results,event,tool_metadata}.py` + `main.py` - the prose/category references the deleted library left behind), commit `0ff5231f` | 1116 | 1113 | -3 | +1813 |
| 2026-08-25 | 2c - cleanup phase 1 | deleted outright (`tools/processing/aggregate_claims_across_sources/` - the orphaned library, module 668 + empty `__init__.py`), commit `0ff5231f` | 668 | 0 | -668 | **+1145** |
| 2026-08-25 | A - static plan + emission style | lib skeleton (`workflows/lib/*.py`: `user_input.py` 207 NEW the typed user-input species, `journal.py` 166 NEW the append-only run journal, `plan.py` +148 the P/D namespaces with construction-site provenance, `data.py` +67, `interpreter.py` +51 the branch evaluation, `slots.py` +37 the context slots, `workflow.py` +16, `validate.py` +14, `__init__.py` +5, `errors.py`/`resolver.py` 0, `params.py` **-48** when the read-recording apparatus went), range `09e4d734..f76234c3` | 3281 | 3944 | +663 | +1808 |
| 2026-08-25 | A - static plan + emission style | template files (the six TELEMAC template folders: the six template `.py` fell 2032 -> 1286, **-746**; six new `declarations.py` siblings 849 + `river_dye/coercions.py` 53 = +902), range `09e4d734..f76234c3` | 2032 | 2188 | +156 | +1964 |
| 2026-08-25 | A - static plan + emission style | emission (`trid3nt_server/emission/*.py`: `styles.py` 367 + `restyle.py` 114 + `cog.py` 87 NEW, against `publish.py` **-432** and `quantity_styles.py` **-156** deleted whole; `outputs_seam.py` +12, `uri_registry.py` +10, `pipeline_emitter.py` -7, `layer_uri_emit.py` -9, `__init__.py` 0), range `09e4d734..f76234c3` | 7454 | 7440 | -14 | +1950 |
| 2026-08-25 | A - static plan + emission style | contracts (`contracts/trid3nt_contracts/styles.py` 150 NEW - the style CONTRACT the one resolver reads; `output_quantities.py` **-23** as the mirror it duplicated went), range `09e4d734..f76234c3` | 481 | 608 | +127 | +2077 |
| 2026-08-25 | A - static plan + emission style | NEW tool (`tools/display/restyle_layer/restyle_layer.py` 177 + `__init__.py` 5 - the fourth entry point onto the one scale schema - plus the single registration line in `tools/__init__.py`), range `09e4d734..f76234c3` | 936 | 1119 | +183 | +2260 |
| 2026-08-25 | A - static plan + emission style | deleted outright (`persistence/case_lifecycle.py` 140, and the one export line for it in `persistence/__init__.py`), range `09e4d734..f76234c3` | 152 | 11 | -141 | +2119 |
| 2026-08-25 | A - static plan + emission style | fleet adaptations (everything the wave had to touch outside the six surfaces above: `tools/processing/_gdal_runner.py` **-65** with `compute_sediment_yield` -15 and `compute_hillshade` -9 onto the collapsed runner, `charts_common.py` +17 NEW, `gates/draw_input.py` +12, `telemac/steps/reach.py` -24, `shared/publish_quantities.py` +2, `telemac/steps/__init__.py` -1, and thirteen signature-only touches that net 0), range `09e4d734..f76234c3` | 19466 | 19383 | -83 | **+2036** |
| 2026-08-25 | B - TELEMAC engine-specific | lib skeleton (`resolution.py` 136 NEW - the four sensitive classes and the one note builder; `user_input.py` +45 `polyline_set`, the plural the polyline context slot normalizes to; `workflow.py` +18 across the wave - the `sensitivity=` declaration threaded to `checks()` at `a67cc188`, the supplied-geometry wiring at `8c796d38`), measured across the wave range `fb4d9c63..8c796d38` | 724 | 923 | +199 | +2235 |
| 2026-08-25 | B - TELEMAC engine-specific | shared steps (`shared/supplied_geometry.py` 121 NEW - the one reader for a filled context slot, layer-or-sketch; it landed in the wave's CLOSE-OUT commit, not with the feature commit), commit `8c796d38` | 0 | 121 | +121 | +2356 |
| 2026-08-25 | B - TELEMAC engine-specific | adapters/runtime (`postprocess_telemac.py` +94 net - the t=0 dry mask, the two-product split, the two COG writers collapsed into one local function; `contracts/trid3nt_contracts/telemac_contracts.py` +16 - the split's answer fields, which the original eight rows omitted and which sits here by the wave-3 precedent), commit `a67cc188` | 3119 | 3229 | +110 | +2466 |
| 2026-08-25 | B - TELEMAC engine-specific | TELEMAC family steps (`agitation.py` **-98** the Overpass helper, its three mirrors, the FlatGeobuf re-upload and the pinned-segment coercion all deleted; `coastal.py` +62 the split publish + the three named duration rungs + the four newly-reachable worker knobs; `deck.py` +20 and `open_water.py` +20 the one `mesh_resolution_label`, which replaced four copies; `substance.py` +8, `solve.py` +6, `stratified.py` +3, `wave.py` +2, `products.py` +2, `__init__.py` -1), commit `a67cc188` | 3339 | 3363 | +24 | +2490 |
| 2026-08-25 | B - TELEMAC engine-specific | template files (six `sensitivity=` declarations + the coastal split's answer/chart/params + the agitation `structure` slot; the three prose-holds-a-number resolution defaults moved from the steps into the declarations that promise them), commit `a67cc188` | 1748 | 1884 | +136 | +2626 |
| 2026-08-25 | B - TELEMAC engine-specific | NEW spec fetcher (`fetchers/ocean/fetch_osm_breakwaters/` - `source.yaml` + `corpus.yaml` + an EMPTY `__init__.py`; the two YAML files are not `.py` and this ledger does not count them) and its hook pair (`hooks/overpass.py` +105 `overpass_breakwaters.build_request` / `.parse_response`), commit `a67cc188` | 560 | 665 | +105 | +2731 |
| 2026-08-25 | B - TELEMAC engine-specific | worker code (`telemac_coastal_build.py` +26 the kept origin + `add_mesh(orig=)` + the echoed corner; `artemis_build.py` +11 the echoed shoal overrides; `entrypoint.py` +9 the echoed rescaled discharge and the one parser stamp), commit `075ad814` | 3338 | 3384 | +46 | +2777 |
| 2026-08-25 | B - TELEMAC engine-specific | proof-path seam (`testing/proof_paths.py` 77 NEW; `canaries.py` +1 onto it; the render + drive scripts moved +23 more, which the counting rule at the head of this file excludes), commit `33e879cf` | 395 | 473 | +78 | **+2855** |
| 2026-08-26 | C - `telemac_rain_on_grid` | the TEMPLATE folder (`rain_on_grid.py` 680 -> 282, `declarations.py` 259 NEW, `cn_infiltration.py` +31 the one AMC normalizer, `__init__.py` 20 -> 10; `mesh_acquisition.py` **-765**, re-homed) | 1835 | 952 | **-883** | +1972 |
| 2026-08-26 | C - `telemac_rain_on_grid` | the shared MESH FRONT (`mesh/watershed.py` 753 NEW - the catchment generation strategy plus its three `Data` producers; `mesh/telemac_build.py` 83 NEW - the thin per-solver SELAFIN writer; `generate_mesh.py` +1 net onto both) | 733 | 1570 | +837 | +2809 |
| 2026-08-26 | C - `telemac_rain_on_grid` | the TELEMAC family steps (`steps/rain_on_grid.py` 941 NEW - acquire, mesh, infiltration, rain, deck, solve, publish; `steps/open_water.py` +21 the extracted `dispatch_and_wait` its second consumer earned; `steps/__init__.py` +21 the exports; `workflow.py` +35 the catchment shape and the seventh process row) | 850 | 1868 | +1018 | +3827 |
| 2026-08-26 | C - `telemac_rain_on_grid` | contracts + harness (`telemac_contracts.py` +74 `TelemacRainOnGridLayerURI` - the 19 typed answer scalars the composer had nowhere to put; `flood_2d.py` -2 onto the shared delineator) | 1713 | 1785 | +72 | **+3899** |

NOTE ON THE JOURNALED ANSWERS - read this with the wave-B `resolution.py` row above.
Runs journaled before the resolution-label fix may carry the inverted sentence: a
default-spacing run whose resolution lever was declared optional on the USER door
but never supplied was labeled RESOLUTION-SENSITIVE instead of RESOLUTION-LIMITED,
TREAT AS A BOUND, so the unsafe-direction warning is missing from those journaled
answers.

**Wave 2 verdict (corrected): net +487 - invested in the skeleton, not yet
repaid; watch.** The +467 first published here undercounted by 20 product lines
the wave landed outside the five surfaces the rows enumerate. The verdict itself
does not change - the sign, the magnitude and the "not yet repaid" reading all
stand - but the number the next wave is measured against is +487, not +467.

**Wave 2b verdict: net +258, running net +745 - the remediation is pure COST, and
that is correct.** (The figure first published here, "net +745", was the running
CLOSE, not the wave's own net; the five 2b rows sum to +258.) Every line of it is a refusal, a narration or a placement move: the
must-fill facade check, the both-directions signature check, the coercion triage,
the wire-type refusal, the width-cap provenance note. None of it absorbs template
code, so none of it can pay back through repetition - it is bought once and it
buys correctness, not brevity. The number to watch remains wave 3's: the saving
recurs per TEMPLATE, and nothing in 2b changes that arithmetic.

The original wave-2 reading, unchanged:
The cohort of two templates shed 521 lines (-38% across the three template
files) against 650 lines of NEW fixed machinery (lib `workflow.py` + `slots.py`
466, TELEMAC facade 184). About 215 of the template reduction is RELOCATION into
the family-step tier (the resolved-input review, the WAQTEL process block, four
wire coercions) rather than absorption, so the honest per-template absorption is
roughly 150 lines each. Projection to watch at the next wave: the lib cost never
recurs, the facade cost recurs once per ENGINE (~184), and the saving recurs
once per TEMPLATE (~150-260). A one-template engine roughly breaks even; the
TELEMAC family alone (6 templates) should turn the running net negative. If wave
3 does not move the running net down, the generalization is not paying and the
slot/facade surface needs cutting, not extending.

**Wave 3 verdict: net +663, running net +1408 - the family did NOT turn the
campaign net negative, and the reason is worth more than the number.**

The projection this wave was measured against said a six-template TELEMAC family
should turn the running net negative, on the arithmetic that the lib cost never
recurs, the facade cost recurs once per ENGINE, and the saving recurs once per
TEMPLATE. Two of those three held. The third did not, and here is the honest
accounting.

THE TEMPLATE FILES DID COLLAPSE, hard: 3120 lines to 1375, -56%. Four files that
were each a 695-848 line composer are now each a 288-301 line declaration plus a
45-49 line sibling coercion (plus a 4-line `__init__`). Read on its own that row
is the best result the campaign has produced.

BUT THE MECHANISM DID NOT VANISH - IT MOVED. Of the 1716 lines the step tier
gained, only `open_water.py` (301) is genuinely shared: staging, dispatch, result
download, the peak publish and the in-worker bed surfacing, written once for four
templates and ready for the fifth. 1241 are FOUR per-template modules -
`coastal.py` 269, `wave.py` 286, `agitation.py` 404, `stratified.py` 282 - one
per template, at a different address; the remaining 174 are the facade's
`_PROCESSES` routing table (+139) and `steps/__init__.py` (+35). Counted per
template, the family's own surface (the template file PLUS its step module) fell
from 3120 to 2616: a real -504, about 126 lines a template - but 504 against the
template row's own -1745 is 29%, so barely more than a quarter of what that row
alone suggests.

WHAT THE REST WENT ON, stated rather than buried, and summing to the wave's +663
without a residue: 808 lines of front the four now share and a fifth will
(`shared/aoi.py` +117 and `shared/tide_series.py` 216, the `_PROCESSES` routing
table +139, `steps/__init__.py` +35, and `open_water.py` 301); 1241 lines of the
four per-template step modules above; 432 lines of runtime and instrumentation,
of which 336 is `testing/canaries.py` - the declared canary registry that made
every parity claim AND every refined-mesh comparison in this wave reproducible,
and which by the drivers-are-product rule counts here in full; 47 lines of
CONSTANT-door enforcement in the library, which is a refusal and can only ever be
a cost; and -28 of fleet adaptation. That is 808 + 1241 + 432 + 47 - 28 = +2500,
against -1745 of template files and -92 deleted outright: +663.

A LINE THAT EARNED ITSELF, for the record: `mesh_sizing_provenance` in
`open_water.py` is about 20 of those lines, and the refined-mesh pass is what
found it missing - telemac3d asked for 1000 m, solved at 1150, and said nothing.
Twenty lines that turn a silent override into a stated one are the cheapest
honesty in the ledger.

WHAT WOULD ACTUALLY PAY IT BACK, named so the next wave can aim at it: the four
per-template step modules are the same four shapes each time - a `write_*_deck`,
a `publish_*_products`, a `_provenance` and a `_honesty_note`. The deck writers
genuinely differ (four different worker configs). The publishers were first
written up here as "roughly 80% identical" worth "perhaps 600 of the 1241 back".
Neither figure was measured. Both are withdrawn and replaced by the measurement
(recipe in the section above, run at `b24feb64`):

- FOUR-WAY similarity: 17 normalized lines are common to all four publishers,
  against a mean publisher length of 67.5 - **25%**. Those 17 are the shape, not
  the substance: the `async def` line, the staging/download calls, the log line.
- PAIRWISE similarity ranges **25% to 43%** (`coastal`/`stratified` 25%,
  `coastal`/`agitation` 36%, `coastal`/`wave` 40%, `wave`/`agitation` 43%,
  `wave`/`stratified` and `agitation`/`stratified` 28% each).
- ABSORBABLE PHYSICAL CEILING: **458 lines** - the four publishers plus their
  `_provenance` and `_honesty_note` in full (121 + 108 + 110 + 119). That is the
  most a DECLARED products shape could ever reach, and at 25% four-way agreement
  the realistic absorption is a fraction of it, not the 600 first projected.

25% four-way is a family resemblance, not a duplicate. The honest reading is that
the publishers are NOT the payback the wave-3 verdict hoped for.

THE MEASURED BETTER TARGET is one tier down, at the seam the publishers all call.
`_publish_peak_layer` - download nothing, style the COG through `publish_layer`,
`model_copy` the narration on - was copied into engine after engine. Commit
`b24feb64` promoted it to `trid3nt_server/workflows/shared/publish_product_layer.py`
(54 lines) under an honest name (it is not peak-specific) and moved the four
TELEMAC step modules onto it. What is left is the fold target, and it is
registered in `docs/DELETION_LEDGER.md` as QUEUED with its condition:

    grep -rn "_publish_peak_layer\|publish_peak_layer" --include=*.py trid3nt_server/
    # 13 hits across 7 files
    grep -rn "def _publish_peak_layer" --include=*.py trid3nt_server/
    # 4 remaining private definitions

FOUR engine families still define their own: `geoclaw/inundation` (:2183),
`swan/wave_field` (:1047), `swmm/urban_flood` (:1315) and `telemac/steps/products`
(:162, the dye publisher the reach cohort still uses). `swmm/dual_drainage`
imports `urban_flood`'s copy; `elmfire/fire_spread` and `landlab/susceptibility`
carry docstring "mirrors `_publish_peak_layer`" references that go stale the
moment one copy is fixed and the others are not. A review-panel note put this at
"8 engine copies"; the command above does not produce 8 by any reading, so the
measured breakdown is what stands here.

That is the measurement to re-make at the MODFLOW family, where the same four
shapes will appear again and the question stops being TELEMAC's.

Counting note for reproducibility: the wave's whole-tree reconciliation is
`git diff --numstat 04ab6b1c 0f7a6351 -- '*.py' ':!tests' ':!scripts'`, which
totals +3809 / -3146 = **+663**, exactly the sum of the seven wave-3 rows above.

**Wave 2c verdict: net -263, running net +1145 - the first wave whose net is
NEGATIVE, and only one row is responsible.** Four fix commits added +408 of
product `.py` between them (the provider-config coherence gate 137, the
corpus-loader refusal 59, the coercion-abstains fix 46, the TELEMAC open-water
front 166), and the cleanup-phase-1 deletion of the orphaned
`aggregate_claims_across_sources` library removed 671. Take the deletion out and
2c reads +408 on a running net of +1816. That is the honest shape of it: this
wave bought refusals and correctness, which cost lines, and it collected an
eighteen-wave-old debt, which paid more of them back. Neither number says
anything about whether the GENERALIZATION is absorbing code - no template
migrated in 2c. The campaign question is still open and still belongs to the
MODFLOW family.

One row inside 2c does bear on it: `shared/publish_product_layer.py` (+54) is the
first line of the fold named in the wave-3 verdict above. It cost 54 to write and
removed nothing yet, because only the four TELEMAC step modules ride it so far.
Whether it pays is a MODFLOW-wave measurement, not a TELEMAC one.

Counting note for 2c: `git diff --numstat 0f7a6351 06ec2f82 -- '*.py' ':!tests'
':!scripts'` totals +570 / -833 = **-263**, exactly the sum of the eleven 2c rows
above. (This note originally ended the range at `HEAD`, which has since moved
through waves A, B and C and no longer reproduces the figure; `06ec2f82` is the
pin that does.) Commit `06ec2f82` (broken tool/library references repointed) is the
range's endpoint and contributes 0 - it touches only `scripts/` and `docs/`,
neither of which this ledger counts.

**Wave A verdict: net +891, running net +2036 - the wave bought STRUCTURE and one
whole capability, and it is honest that neither pays back in lines.**

Three of the seven rows are pure ADDITION of things that did not exist:
`workflows/lib/user_input.py` (207 - the typed user-input species), `journal.py`
(166 - the run journal), and the emission style trio `styles.py` (367) +
`restyle.py` (114) + `cog.py` (87) = 568, plus the contracts loader (150) and the
`restyle_layer` tool (183, its two files plus the one registration line).
That is 1274 lines of NEW capability against which no deletion was ever going to
net out. The style landing paid it back in the same breath out of `publish.py`
(-432) and the whole of `quantity_styles.py` (-156) - the in-code registry, its
family rules, its safe default and its band-percentile helper - so `emission/`
finishes -14 despite gaining three modules.

THE TEMPLATE ROW IS THE ONE TO READ CAREFULLY, because it is the row the whole
campaign is measured on: +156. The six template FILES fell 2032 -> 1286 (-746,
-37%), which is the number the declarations-sibling ruling was aiming at - each
recipe now reads on one page. But PARAMS and DOC did not shrink when they moved;
they landed in 902 lines of sibling (six `declarations.py` totalling 849 plus
`river_dye/coercions.py` at 53). A contract moved next door is not a contract
absorbed, and this ledger counts lines, so the row is +156 and stays +156. What it
bought is readability, not brevity, and the ruling said so ("the recipe readable on
one page, the contract one file over"). The absorption to watch for is the NEXT
migration's: a template that lands in this shape from the start writes its
declarations once.

(The figures in the two paragraphs above were published as 557 / 1263 / +282 /
1721 -> 1141 / 862. None of them reproduced. Every one is now the measured value
from `git diff --numstat 09e4d734 f76234c3` and `git show <ref>:<file> | wc -l`,
and the sign and the reading are unchanged.)

The lib row (+663) is the static-plan rule itself. Roughly half of it is
capability the plan gained (the P/D namespaces with construction-site provenance,
the interpreter's branch evaluation, context slots, deep-freeze) and the deletions
inside it are real but small: -48 from `params.py` when the read-recording
apparatus went. A refactor that makes a plan STATIC cannot be expected to shrink
the machinery that makes it static; it should be expected to shrink the templates,
and it did.

Counting note: `git diff --numstat 09e4d734 f76234c3 -- '*.py' ':!tests'
':!scripts'` totals +3865 / -2974 = **+891**, exactly the sum of the seven wave-A
rows above. (`f76234c3` is the parent of the wave-A close-out doc commit
`6a0f87aa`; this note originally ended the range at `HEAD`, which has since moved
through waves B and C.)
Tests and scripts are excluded by the rule at the head of this file; for the
record, `git diff --numstat 09e4d734 f76234c3 -- '*.py'` reads +6428 / -4294 = +2134,
the difference being the test migration to the static contract (four new test
files: the user-input species, the run journal, the animation legend stability
and the raster-headline coherence check).

**Wave B verdict: net +819, running net +2855 - the most expensive wave since the
family migration, and not one line of it was absorption.**

Wave B migrated no template. It bought three things: a resolution LABEL the answers
never carried, a SPLIT of the coastal product into the two layers the question
actually asks for, and a `structure` context slot that lets a user hand the model a
breakwater instead of hoping OSM has one. All three are capability, and capability
costs lines. There is no row here that the generalization thesis can point at.

THE LEAST FLATTERING ROW IS THE TEMPLATE ROW: **+136**, in a wave that migrated
nothing. The surface this campaign exists to SHRINK grew, and it grew in the six
files wave A had just finished cutting. The reason is not a defect: six
`sensitivity=` declarations, the coastal split's answer/chart/params, the agitation
`structure` slot, and three resolution defaults that had been prose holding a number
inside a step. Every one of those is a promise the template previously made silently
or not at all. It is still +136 on the wrong side of the ledger, and the only thing
that can pay it back is the next migration landing in this shape from the start
rather than being retrofitted into it.

THE ROW THAT DID ITS JOB is the family-step row at **+24**, the smallest number in
the wave and the only one that looks like absorption. `agitation.py` gave back 98
lines - the Overpass helper, its three mirrors, the FlatGeobuf re-upload and the
pinned-segment coercion, all deleted onto the new spec fetcher and the
supplied-geometry reader - and `open_water.py`'s one `mesh_resolution_label` (+20)
replaced four copies. Against that, `coastal.py` +62 and `deck.py` +20 are the split
and its newly-reachable worker knobs. Net, the step tier spent almost exactly what
it absorbed, which is the best a non-migrating wave can honestly do.

WHAT THE FETCHER ROW ACTUALLY COSTS, stated because the row first published here
overstated it by 90: **+105, not +195**. `fetch_osm_breakwaters/` is a SPEC fetcher
- `source.yaml`, `corpus.yaml` and an EMPTY `__init__.py` - and the rule at the head
of this file counts product `.py` only. The 105 real lines are the
`overpass_breakwaters` hook pair. A new data source arriving for 105 lines of hook
instead of a 300-line coded fetcher is the universal-fetcher endgame working, and it
belongs in the ledger at its true size rather than at a flattering one.

TWO BOOKKEEPING FACTS the original eight rows got wrong, recorded so the arithmetic
and the history agree. First, the `structure` slot landed LATE: `shared/supplied_geometry.py`
(121 lines, not the 93 first published) and its `lib/workflow.py` wiring are in
`8c796d38`, the wave's close-out commit - the same commit that first wrote these
rows - not in the feature commit `a67cc188`. That is why its row is pinned there and
why the lib row is measured across the wave's whole range instead of at one commit.
Second, `contracts/trid3nt_contracts/telemac_contracts.py` (+16, the split's answer
fields) appeared in NO row at all; it now sits in the adapters/runtime row, which is
where wave 3 put the same file. Row 8's published `2827 -> 2921 | +94` was a
byte-identical copy of row 3's cells; measured on its own it is `395 -> 473 | +78`.

Counting note: the wave's whole-tree reconciliation is `git diff --numstat fb4d9c63
8c796d38 -- '*.py' ':!tests' ':!scripts'`, which totals +1179 / -360 = **+819**,
exactly the sum of the eight wave-B rows above. `fb4d9c63` is `33e879cf~1`, the
parent of the wave's first commit; `8c796d38` is its close-out. Commit `2b02e061` is
inside that range and contributes 0 - it touches only `docs/`.

**Wave C verdict: net +1044, running net +3899 - the last composer died and the
ledger got its worst-looking row for the best reason. Honest: this wave BUILT more
than it absorbed.**

The template folder is the row the migration was aiming at, and it landed:
**1835 -> 952, -48%**, with `mesh_acquisition.py` deleted outright. That is the
whole of what "migrate the template" was supposed to mean, and it is the only row
here that measures it.

Everything else is what the composer had NOT been doing. Read the three additive
rows for what is actually in them:

- `mesh/watershed.py` (753) is not new code so much as code that was in the wrong
  place, plus the three `Data` producers the composer performed as buried fetches.
  The relocation is provably overdue: `generate_mesh` was importing four symbols
  out of a TELEMAC template's private module and `hecras_flood_2d` a fifth.
- `steps/rain_on_grid.py` (941) carries the deck, the solve and the publish the
  composer had - and then the provenance rows, the honesty note, the chart, the
  resolution label, the two-artifact narration and the typed answer it did NOT.
  638 of those lines are code and 303 are constraint prose, which this ledger
  counts on purpose.
- `telemac_contracts.py` (+74) is nineteen answer scalars that previously existed
  only inside a log line.

The composer declared ZERO params, persisted NO metrics and NO chart spec, emitted
NO provenance rows and carried NO honesty note; its canary had read `NoSuchKey`
for both products since the day it was written. A migration that adds a 24-row
contract, a persisted answer, a chart and a labeled provenance trail is going to
be net-positive in lines, and pretending otherwise would mean deleting the
explanation to make the number look better - which is exactly what the counting
rule at the head of this file exists to prevent.

WHAT WOULD HAVE MADE IT NEGATIVE, and did not: there was no fourth template to
share the catchment front with. The reach family paid for `steps/reach.py` across
three templates and the open-water family paid for `open_water.py` across four.
The catchment front has ONE consumer today. The honest read is that this row is an
INVESTMENT with no second consumer yet, and the thing to watch is whether the next
basin-shaped template (SWMM's `urban_flood`, whose AOI giant tranche is queued)
lands on it for free. If it writes its own delineator, this row was overhead.

COUNTING NOTE. The canary declaration's move to `user_gated` (+7 in
`testing/canaries.py`, with the law-9 reason stated beside it) landed inside
`a7febb61`, the concurrent proof-packet lane's commit, because the two waves were
editing that file at the same time. It is NOT counted in the rows above; it is
named here so the arithmetic and the history agree.

One de-duplication did land and is worth naming because it is the reuse-sweep norm
working rather than a guess: `dispatch_and_wait` moved to `open_water.py` on its
CONFIRMED second consumer, not preemptively. It nets about zero in lines and
removes the second copy of a cancellation clause that a hand-copied version drops.

## Wave D - rerun-with-overrides + coupled validity (2026-08-26, ADR 0319)

| date | wave | surface | before | after | delta | running |
|---|---|---|---|---|---|---|
| 2026-08-26 | D - rerun-with-overrides | lib skeleton, EXISTING files (`workflow.py` +89 the `run`/`execute` split, the validity hook, the failure-attempt recorder, the snapshot write; `interpreter.py` +10 the carried records minus the de-duplicated walk; `ledger.py` +21 `seed()`; `plan.py` +23 `declared_reads`, the ONE walk; `resolver.py` +6 the `door=` / `occasion=` seams; `journal.py` +9 the parent/overrides columns; `__init__.py` +7; `validate.py` **-18**, its private twin of the walk deleted) | 3354 | 3501 | +147 | +4046 |
| 2026-08-26 | D - rerun-with-overrides | lib skeleton, NEW files (`snapshot.py` 181 - the derivable past a finished run leaves; `rerun/derive.py` 148 - the primitive; `validity.py` 111 - the coupled-rule species; `reuse.py` 89 - where reuse stops; `rerun_workflow.py` 85 - the one registered surface; `rerun/__init__.py` 8) | 0 | 622 | +622 | +4668 |
| 2026-08-26 | D - rerun-with-overrides | template files (`coastal_tidal_surge/declarations.py` +58 - the law-crossover predicate, the `VALIDITY` tuple and the two re-written friction rows; `coastal_tidal_surge.py` +4 - the `validity=` declaration and its two-line reason) | 400 | 462 | +62 | +4730 |
| 2026-08-26 | D - rerun-with-overrides | deleted outright (`telemac/set_parameters/set_telemac_parameters.py` 559 + its empty `__init__.py`; `corpus.yaml` is not `.py` and is not counted) | 559 | 0 | **-559** | **+4171** |

**Verdict: +272, and it is the honest shape for this one.** The setter deletion
pays back 559 of the 769 lines the primitive cost, which is the closest this
campaign has come to a capability landing for free - but the reason to look
twice is that the two numbers are not the same KIND of line. The 559 deleted
were ONE engine's recalibration, hand-written against a `.cas` text format, with
no equivalent for the other nine engines. The 769 added are the whole fleet's,
and the next engine's recalibration costs zero new lines: no per-engine setter,
no per-engine bounds table, no per-engine envelope. The per-template cost of a
coupled rule is what the coastal row measures, and it is 62 lines for a
predicate, a message and a declaration.

What this row does NOT claim: `_setter_envelope.py` (433) is still alive for the
three unmigrated setters and is not counted as absorbed. It comes off the board
when the last of them migrates, and the campaign net will move again then.

NOT COUNTED, by the rule at the head of this file: `tests/test_rerun_with_overrides.py`
(575), `scripts/proof_rerun_with_overrides.py` (351),
`scripts/replay_canary_evidence.py` (182), and the 4-line net in
`tools/__init__.py` (one registration import out, one in, plus the comment
saying which three setters are now the pre-migration lane).

## Mesh wave - slice 1: the mesh router, the session, the mesher registry (2026-08-27)

| date | wave | surface | before | after | delta | running |
|---|---|---|---|---|---|---|
| 2026-08-27 | mesh slice 1 | mesh tool, NEW files (`workflows/mesh/session.py` 422 - the session, the recipe journal, replay, the display face and the probes; `workflows/mesh/tool.py` 351 - the router, the declaration value, the resolution order and the registered tool; `workflows/mesh/meshers/__init__.py` 285 - the registry, the field/action declarations and the shared hand-edit action; `workflows/mesh/meshers/reg_grid.py` 95 - the regular-grid mesher over the existing grid math) | 0 | 1153 | +1153 | +5324 |
| 2026-08-27 | mesh slice 1 | mesh tool, EXISTING files (`workflows/mesh/artifact.py` +6 - `recipe_uri`, and `utm_epsg` widened to optional so a geographic lattice does not have to name a zone it is not in) | 362 | 368 | +6 | +5330 |

**Verdict: +1159, and every line of it is the fixed cost the meshers are about
to be paid out of.** Nothing is absorbed yet: the scattered meshing paths
(`generate_mesh`, the deck-writer meshers, the policy classes, `MeshHandle`) are
all still standing, and slices 2 and 5 are where they come off the board. What
this row buys ahead of that is one router, one session, one recipe format and
one registry, so a mesher is a file of declarations plus a build - `reg_grid` is
95 lines and 58 of them are its declarations.

NOT COUNTED, by the rule at the head of this file:
`tests/test_build_mesh_tool.py` (351), `workflows/mesh/corpus.yaml` (not `.py`),
and the 5-line registration import plus comment in `tools/__init__.py`.

## Mesh wave - slice 1 review: typed refusals on the build seam (2026-08-27)

| date | wave | surface | before | after | delta | running |
|---|---|---|---|---|---|---|
| 2026-08-27 | mesh slice 1 review | `workflows/mesh/session.py` (+23 - `_refuse_unbound`: a late-bound spec field or edit input is refused as `MESH_SPEC_UNBOUND` at the build seam instead of failing inside the mesh library on the shape of a placeholder) | 422 | 445 | +23 | +5353 |
| 2026-08-27 | mesh slice 1 review | `workflows/mesh/tool.py` (+9 net - an explicit mesh uri with no object-store reader names the missing reader rather than blaming the mesh for carrying no record) | 351 | 360 | +9 | +5362 |

**Verdict: +32, all of it refusal text.** Both rows close the same gap: a
failure that was arriving untyped (a `TypeError` from inside the grid math) or
under the wrong cause (the caller's missing reader reported as the mesh's
missing record) now leaves as a named mesh refusal.

NOT COUNTED: the 50 lines added to `tests/test_build_mesh_tool.py` (351 -> 401).


## Mesh wave - slice 3: the display face moves to emission (2026-08-27)

| date | wave | surface | before | after | delta | running |
|---|---|---|---|---|---|---|
| 2026-08-27 | mesh slice 3 | `emission/mesh_display.py` NEW - the one `.2dm` writer, in both faces (a built mesh, and the raw `(points, cells, z)` arrays a producer holds) | 0 | 68 | +68 | +5430 |
| 2026-08-27 | mesh slice 3 | `workflows/mesh/session.py` (-24 - the mesh-typed writer and its element-tag table leave for emission; the session imports it) | 445 | 421 | -24 | +5406 |
| 2026-08-27 | mesh slice 3 | `workflows/mesh/generate_mesh/generate_mesh.py` (-21 - the array-typed writer leaves for the same home) | 811 | 790 | -21 | +5385 |

**Verdict: +23 for collapsing two writers into one.** The two `.2dm` writers
that existed wrote the same format with different signatures; what lands in
emission is one implementation with the two entry points its callers actually
have, and the arity check that used to exist on only one of them now guards
both.

## Mesh wave - slices 2 + 3: the lifts, and generate_mesh dissolves (2026-08-27)

| date | wave | surface | before | after | delta | running |
|---|---|---|---|---|---|---|
| 2026-08-27 | mesh slices 2+3 | `workflows/mesh/generate_mesh/` DELETED (`generate_mesh.py` 789 + `hecras_build.py` 184 + `__init__.py` 3 - the standalone tool, its mode inference, its two build providers, its stage-and-record and its SCHISM gr3 emission) | 976 | 0 | -976 | +4409 |
| 2026-08-27 | mesh slices 2+3 | meshers, NEW files (`meshers/watershed.py` 249 - the catchment ask and the provenance it copies from its resolvers; `meshers/coastal_edge.py` 383 - the water-edge domain, its container seam and the open-boundary designation; `meshers/corridor_tin.py` 326 - the reach corridor, wrapping the triangulator where it lives; `meshers/hecras.py` 235 - the graded-seed cell mesh and its authoring bundle) | 0 | 1193 | +1193 | +5602 |
| 2026-08-27 | mesh slices 2+3 | `meshers/__init__.py` (+54 - a mesh whose cells an engine re-realizes, and the edge-band declarations the dissolved tool carried) | 285 | 339 | +54 | +5656 |
| 2026-08-27 | mesh slices 2+3 | `workflows/mesh/session.py` (+73 - the artifact facts only the mesher knows: its own display face, its per-solver files, its authoring bundle, its input rows, and probes for a mesh with no cells of its own) | 417 | 490 | +73 | +5729 |
| 2026-08-27 | mesh slices 2+3 | `workflows/mesh/tool.py` (+24 - the mesher roster the import block registers, the edge-band specs on the tool, and a docstring that routes to five meshers instead of one) | 360 | 384 | +24 | +5753 |

**Verdict: +368 across the lifts, and 976 lines of tool came off the board.**
The four builders that were reachable only through one tool's mode inference are
now four files a template can name, each declaring its own fields and its own
edits; what used to be a 176-line stage-and-record function is the session's, and
what used to be a mode string is the mesher's name. The number is honest about
what the lift costs: a mesher that used to lean on a shared composer now states
its own provenance and its own artifact facts, which is why four files come to
1,193 lines against the 976 they replace - but only one of them is ever read to
answer "how is a coastal mesh built".

NOT COUNTED, by the rule at the head of this file: `tests/test_mesh_meshers.py`
(the renamed `test_generate_mesh.py`, 455 -> 515), the corpus YAML, and the
registration comment in `tools/__init__.py`.

## Mesh wave - slices 2 + 3 review: the adopted layer is a different topology (2026-08-27)

| date | wave | surface | before | after | delta | running |
|---|---|---|---|---|---|---|
| 2026-08-27 | mesh slices 2+3 review | `meshers/__init__.py` (+10 - the adopted-layer action drops the meta bound to the topology it replaced: the per-solver geometry files, the engine authoring bundle, and the probes measured on the old cells) | 339 | 349 | +10 | +5763 |

**Verdict: +10, and it closes a silent substitution.** The lift gave `watershed`
and `coastal_edge` a `files` map naming the `.slf` / `.gr3` their build wrote;
the hand-edit action copied the whole meta forward, so an edited mesh would have
been accepted carrying the PRE-EDIT geometry under the edited mesh's name - a
solver reading the artifact would have run the mesh the user had just changed.
With the topology-bound keys dropped, TELEMAC's SELAFIN is rewritten from the
edited arrays and SCHISM declines the mesh loudly (no `gr3_uri`) instead of being
handed a stale one.

NOT COUNTED: the 37 lines added to `tests/test_mesh_meshers.py` (515 -> 552).

## Corrected campaign net

**The campaign net is +5763** - the true running sum of every delta in the
tables above, from wave 2's first row to the mesh wave's slices 2 and 3 and their
review.

## Mesh wave - slices 2 + 6: the two library wrappers, and the measured cut (2026-08-27)

| date | wave | surface | before | after | delta | running |
|---|---|---|---|---|---|---|
| 2026-08-27 | mesh slices 2+6 | `meshers/om2d.py` NEW - the OceanMesh2D wrapper: the shoreline domain, the bed the sizing reads, the box seam, the four edit actions, the measured conformal offset and the per-side bed the seaward designation is chosen on | 0 | 631 | +631 | +6394 |
| 2026-08-27 | mesh slices 2+6 | `meshers/telapy_mesh.py` NEW - the TELEMAC-geometry wrapper: adoption through HermesFile, the punch/refine/classify ops, and the `.slf`+`.cli` pair every mesher writes through it | 0 | 505 | +505 | +6899 |
| 2026-08-27 | mesh slices 2+6 | `meshers/__init__.py` (+45 - `checked_refine`, the by-name check for the knobs inside a refine block; `Mesher.deterministic`, a measured claim about the library) | 349 | 394 | +45 | +6944 |
| 2026-08-27 | mesh slices 2+6 | `workflows/mesh/tool.py` (+9 - the two new meshers on the roster and in the tool's own routing docstring) | 434 | 443 | +9 | +6953 |
| 2026-08-27 | mesh slices 2+6 | `workflows/mesh/session.py` (+7 - the recipe's spec line carries `determinism: false` for a mesher that does not reproduce itself) | 494 | 501 | +7 | +6960 |
| 2026-08-27 | mesh slices 2+6 | `workflows/mesh/artifact.py` (+3 - `cli_uri`, the TELEMAC boundary file written from this geometry's own numbering) | 369 | 372 | +3 | +6963 |

**Verdict: +1,200, and it is the whole of two new capabilities.** Nothing was
replaced here - `om2d` and `telapy_mesh` are meshers the tool did not have, so
every line is addition rather than absorption. What the size buys, per file:
`om2d` carries a rebuild state (an obstacle and a refine region are INPUTS to
DistMesh, so an edit re-enters the box rather than patching arrays), the
conformal measurement, and the format fan-out from one topology pass;
`telapy_mesh` carries the adoption, the four ops, and the `.slf`/`.cli` pair
that `om2d` also writes through it rather than duplicating.

The two in-container drivers (`scripts/sandbox/oceanmesh/_om2d_incontainer.py`
295, `scripts/sandbox/telemac/_telapy_mesh_incontainer.py` 305) were NOT COUNTED
by the rule at the head of this file - they were scripts, mounted into the boxes
where oceanmesh and telapy live. The review remediation moved them into
`trid3nt_server/workflows/mesh/meshers/drivers/` and they are counted from
there; see the remediation section at the foot of this file. Neither is `tests/test_mesh_om2d_telapy.py`
(502) nor the ten corpus phrasings.

## Mesh wave - slices 2 + 6 review remediation: the wrappers stop reimplementing (2026-08-27)

| date | wave | surface | before | after | delta | running |
|---|---|---|---|---|---|---|
| 2026-08-27 | mesh 2+6 remediation | `meshers/drivers/om2d_driver.py` MOVED into the product tree and now COUNTED (from `scripts/sandbox/oceanmesh/_om2d_incontainer.py`, 295 uncounted); +117 for the `ocean_boundary` op - the per-component walk, the library's own section identification, and the op dispatch | 0 | 412 | +412 | +7375 |
| 2026-08-27 | mesh 2+6 remediation | `meshers/drivers/telapy_mesh_driver.py` MOVED and now COUNTED (from `scripts/sandbox/telemac/`, 305 uncounted); +55 for the one-count IPOBO walk, the permutation check, the contour-run measurement and the fully-liquid-contour refusal | 0 | 360 | +360 | +7735 |
| 2026-08-27 | mesh 2+6 remediation | `meshers/drivers/coastal_edge_driver.py` MOVED and now COUNTED (from `scripts/sandbox/oceanmesh/_mesh_water_edge_incontainer.py`, 245 uncounted); content unchanged but for its header | 0 | 245 | +245 | +7980 |
| 2026-08-27 | mesh 2+6 remediation | `meshers/drivers/__init__.py` NEW - `drivers_dir()`, the one path a mesher mounts | 0 | 21 | +21 | +8001 |
| 2026-08-27 | mesh 2+6 remediation | `meshers/om2d.py` (+74 - the contiguous-section resolution, its two typed refusals, the evidence every section rides back in, and the op-aware box seam) | 631 | 705 | +74 | +8075 |
| 2026-08-27 | mesh 2+6 remediation | `meshers/telapy_mesh.py` (-5 - the sandbox path constant and its repo-root helper die with the move) | 505 | 500 | -5 | +8070 |
| 2026-08-27 | mesh 2+6 remediation | `meshers/coastal_edge.py` (+4 - the drivers mount, and the fetch-shape readers replacing two attribute reads) | 383 | 387 | +4 | +8074 |
| 2026-08-27 | mesh 2+6 remediation | `meshers/__init__.py` (+35 - `fetch_activation_rows` / `fetch_fallback_note`: one reader for both shapes a fetch answers in) | 394 | 429 | +35 | +8109 |

**Verdict: +1,038 counted, and 845 of it is a MOVE, not new code.** The three
in-container drivers were product code living in `scripts/sandbox/`; they now
sit beside the meshers that shell them and are counted like everything else, so
the "sandbox" line in this ledger stops hiding a third of the mesh tool. The
172 lines the remediation actually added are the two library calls the drivers
had inlined (`om.Difference`, `om.enforce_mesh_gradation` on a seeded obstacle
band), the section identification the open boundary now comes from, and the
IPOBO permutation and contour-run checks that would have caught the defect
this remediation fixes.

NOT COUNTED by the rule at the head of this file: `workers/schism/schism_gr3.py`
294 -> 335 (+41, `open_sections=` and the contiguous land-run split - the one
gr3 writer, worker tree), `scripts/sandbox/oceanmesh/mesh_formats.py` 245 -> 285
(+40, the same two changes in `write_fort14`), and
`tests/test_mesh_om2d_telapy.py` 502 -> 738 (+236).

## Mesh wave - slice 4: the gate loop (2026-08-27)

| date | wave | surface | before | after | delta | running |
|---|---|---|---|---|---|---|
| 2026-08-27 | mesh 4 | `workflows/mesh/gate.py` NEW - the mount/unmount lifecycle, the generated per-action tools, the presentation, and the parked loop for a demanded build | 0 | 437 | +437 | +8546 |
| 2026-08-27 | mesh 4 | `tools/__init__.py` (+44 - `mount_tool` / `unmount_tool` / `MOUNTED_TOOLS`: the one seam a session-scoped tool enters and leaves the registry through) | 944 | 988 | +44 | +8590 |
| 2026-08-27 | mesh 4 | `workflows/mesh/tool.py` (+19 - the `input_mode` lever and the user-gated branch that presents instead of accepting) | 443 | 462 | +19 | +8609 |
| 2026-08-27 | mesh 4 | `tools/search/tool_retrieval.py` (+2 - mounted names join the visibility floor) | 345 | 347 | +2 | +8611 |
| 2026-08-27 | mesh 4 | `gates/tool_gating.py` (+5 - the same floor on the openai gate) | 355 | 360 | +5 | +8616 |

**Verdict: +507, and none of it is a second mechanism.** The gate reuses the
pending-confirmation spine, `resolve_input_gate_mode`, and
`publish_input_layer` verbatim; what is actually new is the mount lifecycle
(44 lines in the registry, the rest of it generation and the loop). The tool
schemas the model reads are GENERATED from the mesher's own action registry, so
a new mesher adds edit tools without a line here.

NOT COUNTED by the rule at the head of this file: `tests/test_mesh_gate_loop.py`
346 (new).

NOT DELETED, and why: the template-specific approve-mesh GateSpec plumbing
(`river_dye._TELEMAC_RIVER_DYE_METADATA.gate_spec`,
`solver_confirm:estimate_telemac_mesh` / `pin_telemac_mesh` /
`_build_telemac_mesh_envelope`, `telemac/steps/mesh_preview.py`) still stands.
`telemac_river_dye` meshes through `MeshPolicy` / `CorridorPolicy` /
`MeshHandle`, not through a `MeshSession`, so the new gate does not yet fire on
that template - deleting its card now would remove the user's only mesh review
with nothing in its place. The chop belongs to the template migration that
moves `river_dye.MESH` onto `tool.build_mesh`.

## Mesh wave - slices 5 + 7: the template migration and dt from measured edges (2026-08-27)

| date | wave | surface | before | after | delta | running |
|---|---|---|---|---|---|---|
| 2026-08-27 | mesh 5 | `lib/slots.py` (-28 - `MeshPolicy` deleted; the universal sizing ask is spec fields on the mesher that reads them) | 148 | 120 | -28 | +8588 |
| 2026-08-27 | mesh 5 | `lib/workflow.py` (-6 - `EngineOps.build_mesh` deleted; `solver_spec` -> `solve`, `read_results` -> `read`) | 648 | 642 | -6 | +8582 |
| 2026-08-27 | mesh 5 | `telemac/workflow.py` (-33 net - `CorridorPolicy` + `MeshHandle` out, `_Process.domain_ref` + `mesh_deck_fields` in) | 429 | 396 | -33 | +8549 |
| 2026-08-27 | mesh 5 | `telemac/steps/rain_on_grid.py` (-24 - `CatchmentPolicy` out; `Catchment.mesh` unpacks the declaration) | 941 | 917 | -24 | +8525 |
| 2026-08-27 | mesh 5 | `mesh/meshers/watershed.py` (+38 - the two ex-policy knobs declared and carried, and an acquired window read as an extent plus its outlet) | 249 | 287 | +38 | +8563 |
| 2026-08-27 | mesh 5 | the seven TELEMAC templates (+42 across `river_dye` +6, `do_sag` +6, `coastal_tidal_surge` +8, `agitation` +8, `wave_field` +8, `stratified_flow` +8, `rain_on_grid` -2) | 1699 | 1741 | +42 | +8605 |
| 2026-08-27 | mesh 7 | `mesh/artifact.py` (+23 - `probes` on the artifact + `measured_min_edge_m`) | 372 | 395 | +23 | +8628 |
| 2026-08-27 | mesh 7 | `mesh/session.py` (+1 - accept records what it measured) | 501 | 502 | +1 | +8629 |
| 2026-08-27 | mesh 7 | `telemac/steps/reach.py` (+10 - `suggest_time_step_s` prefers the measured edge) | 865 | 875 | +10 | +8639 |

**Verdict: +23 across fifteen files, and four classes gone.** The migration is
close to LOC-neutral because it is a MOVE, not an addition: the sizing and shape
fields the three policy classes carried are now declared on the meshers that read
them, where the router already validates every field by name. What the +42 on the
templates buys is a declaration that states which mesher builds the domain and
what it is asked for, in place of two opaque policy objects assembled at a facade
call; the +38 on the watershed mesher is the ex-`CatchmentPolicy` knobs becoming
declared, documented, replay-carried fields rather than kwargs forwarded blind.

DECK PARITY, measured rather than asserted: every one of the seven templates'
FULL plans - each step's runner, stage, name and every kwarg - was dumped before
the migration and after it and diffed. Byte-identical, `rain_on_grid` included,
which is why nothing here needed the "legitimate diff" the kickoff allowed for.

NOT COUNTED by the rule at the head of this file: `tests/test_workflow_skeleton.py`
504 -> 580 (+76, the fleet's mesh declarations and the deck fields they reach) and
`tests/test_build_mesh_tool.py` 396 -> 455 (+59, the artifact's probes and the
measured-edge timestep).

STILL NOT DELETED, and why: the template-specific approve-mesh GateSpec plumbing
(`river_dye._TELEMAC_RIVER_DYE_METADATA.gate_spec`,
`solver_confirm:estimate_telemac_mesh` / `pin_telemac_mesh` /
`_build_telemac_mesh_envelope`, `telemac/steps/mesh_preview.py`) still stands.
The migration moved the ASK onto `tool.build_mesh`; the corridor mesh a reach run
solves on is still built inside the TELEMAC deck writer and the worker, so no
`MeshSession` opens on that template and the standard mesh gate still does not
fire there. Deleting the card now would remove the user's only mesh review with
nothing in its place. CONDITION unchanged: `author` demand-pulls a `corridor_tin`
build and the reach deck consumes the accepted `MeshArtifact`. (Met in the
lens-1 remediation section below; all four are deleted there.)

Slice 7 lands the seam and its measurement, not new call sites: the accepted
artifact carries its probes and `suggest_time_step_s` prefers the measured
minimum edge. Every TELEMAC caller today is an ESTIMATE path - the reach deck and
the mesh preview both derive dt before any mesh exists - so they pass no artifact
and read the requested edge, which is the honest answer at that moment. The
tightening becomes live for the reach family under the same condition as the gate
chop above. (Met in the lens-1 remediation section below: the reach deck passes
the accepted artifact.)

## Mesh wave - panel lens-1 remediation: the reach family reaches the gate (2026-08-27)

| date | wave | surface | before | after | delta | running |
|---|---|---|---|---|---|---|
| 2026-08-27 | lens-1 F1 | `mesh/meshers/watershed.py` + `coastal_edge.py` (+10 - the measured determinism each registers, with the 3-run evidence) | 674 | 684 | +10 | +8649 |
| 2026-08-27 | lens-1 (a) | `mesh/meshers/__init__.py` (+24 - an action's inputs are its generated tool's parameters, refused in declaration order) | 429 | 453 | +24 | +8673 |
| 2026-08-27 | lens-1 (b) | `mesh/tool.py` (+14 - a mesher that declares no extent refuses one by name) | 462 | 476 | +14 | +8687 |
| 2026-08-27 | lens-1 (c) | `mesh/meshers/om2d.py` (-2 - the ADCIRC emission drops; the shared writer stays) | 704 | 702 | -2 | +8685 |
| 2026-08-27 | lens-1 F3 | `mesh/meshers/corridor_tin.py` (+30 - the build's geometry, its `.cli` and its topology bundle staged onto the mesh) | 326 | 356 | +30 | +8715 |
| 2026-08-27 | lens-1 F3 | `mesh/artifact.py` (+5 - `topology_uri`: what a geometry file cannot say about its own boundary) | 395 | 400 | +5 | +8720 |
| 2026-08-27 | lens-1 F3 | `telemac/steps/reach.py` (+76 - `ReachMesh.corridor` + `build_corridor_mesh`, the session the demand-pull opens) | 875 | 951 | +76 | +8796 |
| 2026-08-27 | lens-1 F2+F3 | `telemac/steps/deck.py` (+35 - the accepted mesh staged for the solve, and dt read off it) | 475 | 510 | +35 | +8831 |
| 2026-08-27 | lens-1 F3 | `telemac/workflow.py` +5, `telemac/steps/__init__.py` +2, `do_sag` +2, `river_dye` -11, `river_dye/coercions.py` +4 | 1055 | 1057 | +2 | +8833 |
| 2026-08-27 | lens-1 F3 | `telemac/steps/mesh_preview.py` (-277 - DELETED; the standard mesh gate presents the same build) | 277 | 0 | -277 | +8556 |
| 2026-08-27 | lens-1 F3 | `gates/cards/solver_confirm.py` + `gates/cards/__init__.py` (-211 - the template-specific approve-mesh card and its two providers) | 1420 | 1209 | -211 | +8345 |
| 2026-08-27 | lens-1 F3 | `workers/telemac/_staged_mesh.py` + `entrypoint.py` (+129 - the worker's half of the hand-off: write the accepted topology, adopt one when staged) | 1581 | 1710 | +129 | +8474 |

**Verdict: -165 net across nineteen files, and the mesh gate is the only mesh
gate.** The reach family used to reach its mesh twice - once as a preview built
for a card, once again inside the solve - and neither pass produced an artifact
anything downstream could read. It now builds once, through a `MeshSession` over
the `corridor_tin` declaration, and the solve adopts what was accepted. The 488
lines that came out are the second path; the 129 that went into the worker are
what makes adoption possible at all, since a SELAFIN states which nodes lie on a
boundary and never which stretch of it the flow enters by.

DECK PARITY, measured rather than asserted: `tests/test_telemac_reach_mesh_session.py`
restates the reach deck from the ask - every field, both reach shapes - and the
writer matches it field for field. The only line the refactor could have moved is
`time_step_s`, and it moves ONLY when the artifact reports an edge finer than the
one that was asked for, which is what slice 7's seam was built to do.

LIVE: `corridor_tin` built through the rebuilt image (Scotia, California, 25 m
ask) -> 4191 nodes / 7660 elements, min measured edge 5.0 m, and the accepted
mesh staged `river.slf`, `river.cli` and `river_mesh.npz` beside its display face
and recipe. At that mesh the deck's dt reads 0.25 s off the measurement where the
ask alone would have written 1.0 s.

NOT COUNTED by the rule at the head of this file:
`tests/test_telemac_reach_mesh_session.py` (new, 218 lines),
`tests/test_build_mesh_tool.py` 455 -> 537, `tests/test_mesh_om2d_telapy.py`
+24, plus the stub rows in `test_telemac_do_sag.py`, `test_run_river_dye_scenario.py`,
`test_rerun_with_overrides.py` and `test_gate_collapse_specs.py`.

## Mesh wave - re-verify round 2: the client half, and the late refusals (2026-08-27)

| date | wave | surface | before | after | delta | running |
|---|---|---|---|---|---|---|
| 2026-08-27 | r2 G1 | `mesh/meshers/__init__.py` (+48 - the hand-edit's regeneration hook, the claims an adopted mesh restates, and the refusal for a mesh nothing could be staged from) | 453 | 501 | +48 | +8522 |
| 2026-08-27 | r2 G1 | `mesh/meshers/corridor_tin.py` (+234 - the corridor's re-adoption: boundary walk, IPOBO ranking, carried roles, and the three files rewritten from the edited nodes) | 356 | 590 | +234 | +8756 |
| 2026-08-27 | r2 G1 | `mesh/telemac_build.py` (+5 - the geometry names the domain it actually is) | 83 | 88 | +5 | +8761 |
| 2026-08-27 | r2 minor | `mesh/artifact.py` (+8 - a corridor is bed-less and still TELEMAC's to solve; the SWAN line stops describing a format nothing reads) | 400 | 408 | +8 | +8769 |
| 2026-08-27 | r2 G2 | `mesh/gate.py` (+122 - the card's own knob rows, and the reply routed back into the actions those knobs turn) | 437 | 559 | +122 | +8891 |
| 2026-08-27 | r2 G3 | `plugin/ui/cards.py` (-105 - the release-point picker row, its map tool, its click handler and its teardown) | 3100 | 2995 | -105 | +8786 |
| 2026-08-27 | r2 G3 | `plugin/ui/gate.py` (-27 - the two release-point readers and the decision branch on them) | 1483 | 1456 | -27 | +8759 |
| 2026-08-27 | r2 G3 | `plugin/ui/dock.py` (-3 - the card no longer needs the canvas) | 2838 | 2835 | -3 | +8756 |

**Verdict: +282 net across eight files, and every refusal moved to where the
user can still act on it.** The corridor's 234 lines are the price of an honest
hand-edit: a reach solve is staged from a topology bundle rather than a geometry
file, so adopting an edited layer means rewriting that bundle - the boundary
walk and its IPOBO ranking come out of the cells, and which stretch is the
inflow comes from the nearest node the build already classified, with the
distance that carry spanned reported rather than assumed. What a mesher cannot
rewrite it now refuses AT THE EDIT; a session can no longer accept a mesh the
deck would decline afterwards.

The gate's +122 buys the only revision channel the shipped client can reach: a
param sheet of `<action>.<input>` rows plus the truncation row, rendered by the
card the plugin already had. The client half of the deleted approve-mesh gate
(-135) had no producer left at all.

LIVE (`scripts/proof_corridor_hand_edit_solve.py`, Eel River near Scotia,
California, 45 m ask over a 3 km reach): the corridor built through its box at
788 nodes / 1337 elements; one interior triangle was split at its centroid the
way a QGIS refinement would, giving 789 / 1339 - counts no rebuild of the same
corridor could report, which is what makes the run's own numbers discriminate.
Boundary roles carried a measured 0.25 m at worst. The accepted mesh staged
`s3://trid3nt-cache/mesh/01M12NH049B6WYPJ8KEFX8DJRA/river_mesh.npz`, the DO-sag
deck consumed it, and run `01M12NJXWPK89EDQ536AH689J4` completed
`status=ok exit_code=0 correct_end=true wall_s=14.0` reporting `npoin=789
nelem=1339 nptfr=237 n_inflow_nodes=7 n_outflow_nodes=7`. The solve ran on the
EDITED geometry. What this run does NOT claim is physics: 900 s of a coarse
reach leaves the tracer field at zero, and the assertion here is provenance.

NOT COUNTED by the rule at the head of this file:
`tests/test_corridor_mesh_readopt.py` (new, 331 lines),
`tests/test_mesh_gate_loop.py` +129, `tests/test_mesh_meshers.py` +6,
`plugin/tests/headless_mesh_gate_drive.py` +
`plugin/tests/validate_mesh_gate_driver_offline.py` (rewritten from the two
deleted bk3b drivers).
