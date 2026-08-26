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

**Wave 2 verdict (corrected): net +487 - invested in the skeleton, not yet
repaid; watch.** The +467 first published here undercounted by 20 product lines
the wave landed outside the five surfaces the rows enumerate. The verdict itself
does not change - the sign, the magnitude and the "not yet repaid" reading all
stand - but the number the next wave is measured against is +487, not +467.

**Wave 2b verdict: net +745 - the remediation is pure COST, and that is
correct.** Every line of it is a refusal, a narration or a placement move: the
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

Counting note for 2c: `git diff --numstat 0f7a6351 HEAD -- '*.py' ':!tests'
':!scripts'` totals +570 / -833 = **-263**, exactly the sum of the eleven 2c rows
above. Commit `06ec2f82` (broken tool/library references repointed) is in that
range and contributes 0 - it touches only `scripts/` and `docs/`, neither of
which this ledger counts.

**Wave A verdict: net +891, running net +2162 - the wave bought STRUCTURE and one
whole capability, and it is honest that neither pays back in lines.**

Three of the seven rows are pure ADDITION of things that did not exist:
`workflows/lib/user_input.py` (207 - the typed user-input species), `journal.py`
(166 - the run journal), and the emission style trio `styles.py` + `restyle.py` +
`cog.py` (557) plus the contracts loader (150) and the `restyle_layer` tool (183).
That is 1263 lines of NEW capability against which no deletion was ever going to
net out. The style landing paid back 564 of it in the same breath - the 59-row
in-code registry, its family rules, its safe default, its band-percentile helper
and the whole of `quantity_styles.py` - so `emission/` finishes -14 despite
gaining three modules, and the docstring sweep took another 55 out of the same
package as archaeology.

THE TEMPLATE ROW IS THE ONE TO READ CAREFULLY, and it is the wave's least
flattering: +282. The six template FILES fell 1721 -> 1141 (-580, -34%), which is
the number the declarations-sibling ruling was aiming at - each recipe now reads on
one page. But PARAMS and DOC did not shrink when they moved; they landed in 862
lines of sibling. A contract moved next door is not a contract absorbed, and this
ledger counts lines, so the row is +282 and stays +282. What it bought is
readability, not brevity, and the ruling said so ("the recipe readable on one page,
the contract one file over"). The absorption to watch for is the NEXT migration's:
a template that lands in this shape from the start writes its declarations once.

The lib row (+663) is the static-plan rule itself. Roughly half of it is
capability the plan gained (the P/D namespaces with construction-site provenance,
the interpreter's branch evaluation, context slots, deep-freeze) and the deletions
inside it are real but small: -48 from `params.py` when the read-recording
apparatus went. A refactor that makes a plan STATIC cannot be expected to shrink
the machinery that makes it static; it should be expected to shrink the templates,
and it did.

Counting note: `git diff --numstat 09e4d734 HEAD -- '*.py' ':!tests' ':!scripts'`
totals +3865 / -2974 = **+891**, exactly the sum of the seven wave-A rows above.
Tests and scripts are excluded by the rule at the head of this file; for the
record, the same command without those exclusions reads +6428 / -4294 = +2134,
the difference being the test migration to the static contract (four new test
files: the user-input species, the run journal, the animation legend stability
and the raster-headline coherence check).
