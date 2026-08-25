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

Two surfaces the wave-2 rows forgot, added here so no wave forgets them again:
**adapters/runtime** (a wave that has to change `trid3nt_server/adapters/` or any
other server module to land) and **fleet adaptations** (product templates the
wave did not migrate but had to touch to keep operable). Both are product `.py`
and both count. The check that catches an omission: `git diff --stat <baseline>
-- '*.py'` over the whole tree must reconcile with the sum of the wave's rows.

## Ledger

A remediation wave touches a handful of files across a surface rather than
rebuilding it, so the 2b rows report the TOUCHED files' before/after and do not
continue the wave-2 surface totals. The DELTA and the running net are what carry
across; the reconciliation check above is what proves nothing was dropped.

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
| 2026-08-25 | 3 - TELEMAC family (ref `04ab6b1c`) | migrated template files (`coastal_tidal_surge` 848 -> 347, `wave_field` 695 -> 340, `agitation` 840 -> 340, `stratified_flow` 737 -> 342; each = the template + its new sibling coercion + `__init__`) | 3120 | 1369 | -1751 | -1006 |
| 2026-08-25 | 3 - TELEMAC family | TELEMAC facade + family steps (`workflow.py` +139 the `_PROCESSES` routing table, `steps/__init__.py` +35, and the five NEW step modules: `open_water.py` 273 shared by all four, `coastal.py` 267, `wave.py` 281, `agitation.py` 399, `stratified.py` 277) | 326 | 1997 | +1671 | +665 |
| 2026-08-25 | 3 - TELEMAC family | shared steps (`shared/aoi.py` +117 the AOI acquire step, `shared/tide_series.py` 216 the CO-OPS forcing resolver) | 74 | 407 | +333 | +998 |
| 2026-08-25 | 3 - TELEMAC family | lib skeleton (the CONSTANT-door wire enforcement: `workflow.py` +41 `_wire_params` + the factory note, `docstring.py` +6) | 554 | 601 | +47 | +1045 |
| 2026-08-25 | 3 - TELEMAC family | adapters/runtime (`postprocess_telemac.py` +38 the shared `_local_mesh_origin` + the three call sites, `contracts/telemac_contracts.py` +27 the curve/profile answer fields, `testing/live_run.py` +31 the dispatch-card verdict, `testing/canaries.py` 258 the declared canary registry) | 3309 | 3663 | +354 | +1399 |
| 2026-08-25 | 3 - TELEMAC family | fleet adaptations (the cohort's now-dead `TEMPLATE_CARD` / `QUESTION` decoration stripped, its prose moved into the module docstrings) | 864 | 836 | -28 | +1371 |
| 2026-08-25 | 3 - TELEMAC family | deleted outright (`telemac/_bed_input.py` 62, `telemac/_template_card.py` 30) | 92 | 0 | -92 | **+1279** |

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

**Wave 3 verdict: net +534, running net +1279 - the family did NOT turn the
campaign net negative, and the reason is worth more than the number.**

The projection this wave was measured against said a six-template TELEMAC family
should turn the running net negative, on the arithmetic that the lib cost never
recurs, the facade cost recurs once per ENGINE, and the saving recurs once per
TEMPLATE. Two of those three held. The third did not, and here is the honest
accounting.

THE TEMPLATE FILES DID COLLAPSE, hard: 3120 lines to 1369, -56%. Four files that
were each an 700-850 line composer are now each a ~290-line declaration plus a
~48-line sibling coercion. Read on its own that row is the best result the
campaign has produced.

BUT THE MECHANISM DID NOT VANISH - IT MOVED. Of the 1671 lines the step tier
gained, only `open_water.py` (273) is genuinely shared: staging, dispatch, result
download, the peak publish and the in-worker bed surfacing, written once for four
templates and ready for the fifth. The other 1224 are FOUR per-template modules -
`coastal.py`, `wave.py`, `agitation.py`, `stratified.py` - one per template, at a
different address. Counted per template, the family's own surface (the template
file PLUS its step module) fell from 3120 to 2593: a real -527, about 130 lines a
template, but a sixth of what the template row alone suggests.

WHAT THE REST WENT ON, stated rather than buried: 780 lines of front the four now
share and a fifth will (the AOI acquire, the tide-series resolver, the
`_PROCESSES` routing table); 354 lines of runtime and instrumentation, of which
258 is `testing/canaries.py` - the declared canary registry that made every
parity claim in this wave reproducible, and which by the drivers-are-product rule
counts here in full; and 47 lines of CONSTANT-door enforcement in the library,
which is a refusal and can only ever be a cost.

WHAT WOULD ACTUALLY PAY IT BACK, named so the next wave can aim at it: the four
per-template step modules are the same four shapes each time - a `write_*_deck`,
a `publish_*_products`, a `_provenance` and a `_honesty_note`. The deck writers
genuinely differ (four different worker configs) but the PUBLISHERS are roughly
80% identical: download the result, call the engine's postprocessor, fold the
worker's extra scalars on, publish through the one seam, surface the bed, log.
Absorbing that into a DECLARED products shape - which postprocessor, which extra
scalars, which honesty note - would take perhaps 600 of the 1224 back. That is
the measurement to make at the MODFLOW family, where the same four shapes will
appear again and the question stops being TELEMAC's.

Counting note for reproducibility: the wave's whole-tree reconciliation is
`git diff --numstat 04ab6b1c -- '*.py' ':!tests' ':!scripts'`, which totals
+3680 / -3146 = **+534**, exactly the sum of the seven rows above.
