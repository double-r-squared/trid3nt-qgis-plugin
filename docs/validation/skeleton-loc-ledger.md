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
scripts. (Wave 2 also added `tests/test_workflow_skeleton.py`, 230 lines, not
counted here by that rule.)

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
| 2026-08-24 | 2 - CORRECTION (found by the wave-2 adversarial panel) | product lines the wave-2 rows missed: the `_normalize_callable_for_gemini` signature re-stamp (`adapters/gemini_compat.py` +12) and the four non-migrated templates' mechanical `Plan(...)` / chart-function adaptations (+8) | 0 | 20 | +20 | **+487** |
| 2026-08-24 | 2b - panel remediation | lib skeleton (`workflow.py` +105 must-fill + coercion triage + provenance arity + explicit solve step, `params.py` +12 wire-type refusal, `slots.py` -3 corridor fields out, `plan.py` +4, `__init__.py` +6) | 1270 | 1394 | +124 | +611 |
| 2026-08-24 | 2b - panel remediation | telemac facade + family steps (`workflow.py` +40 CorridorPolicy + required-coverage check, `reach.py` +37 MeshSizing + cap narration, `products.py` +21 mesh-override provenance, `deck.py` +9, `mesh_preview.py` +3) | 1867 | 1988 | +121 | +732 |
| 2026-08-24 | 2b - panel remediation | shared steps (`shared/aoi.py` - required `code_prefix` + its rationale) | 68 | 74 | +6 | +738 |
| 2026-08-24 | 2b - panel remediation | cohort templates (`do_sag.py` +7, `river_dye.py` -3 - the corridor slot costs a line and buys the placement) | 854 | 858 | +4 | +742 |
| 2026-08-24 | 2b - panel remediation | fleet adaptations (3 orphaned `_STEPS` constants deleted) | 1209 | 1204 | -5 | **+737** |

**Wave 2 verdict (corrected): net +487 - invested in the skeleton, not yet
repaid; watch.** The +467 first published here undercounted by 20 product lines
the wave landed outside the five surfaces the rows enumerate. The verdict itself
does not change - the sign, the magnitude and the "not yet repaid" reading all
stand - but the number the next wave is measured against is +487, not +467.

**Wave 2b verdict: net +737 - the remediation is pure COST, and that is
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
