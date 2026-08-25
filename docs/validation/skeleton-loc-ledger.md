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
explanation. Counts are `.py` only, never tests, never docs.

The four surfaces every row splits into:

| surface | what it holds | what growth here MEANS |
|---|---|---|
| lib skeleton | `trid3nt_server/workflows/lib/*.py` | FIXED cost, paid once for the whole fleet |
| engine facade + family steps | `trid3nt_server/workflows/<engine>/workflow.py` + `<engine>/steps/*.py` | PER-ENGINE cost, paid once per engine |
| shared steps | `trid3nt_server/workflows/shared/*.py` (only files the wave touched) | FIXED cost, shared across engines |
| template files | the migrated templates' own `.py` | PER-TEMPLATE - this is the number that has to fall |
| deleted outright | files removed | pure absorption |

## Ledger

| date | wave | surface | LOC before | LOC after | delta | running net |
|---|---|---|---|---|---|---|
| 2026-08-24 | 0 - baseline (ref `8304c289`) | lib skeleton (14 files) | 3944 | - | - | - |
| 2026-08-24 | 0 - baseline | telemac family steps (9 files, no facade) | 2874 | - | - | - |
| 2026-08-24 | 0 - baseline | shared steps (`shared/aoi.py` did not exist) | 0 | - | - | - |
| 2026-08-24 | 0 - baseline | cohort templates (`do_sag.py` 399 + `do_sag/steps.py` 278 + `river_dye.py` 698) | 1375 | - | - | - |
| 2026-08-24 | 2 - skeleton + cohort | lib skeleton (16 files; `workflow.py` 368 + `slots.py` 112 new, `plan.py` +30, `params.py` +19, `interpreter.py` +5, `__init__.py` +12) | 3944 | 4490 | +546 | +546 |
| 2026-08-24 | 2 - skeleton + cohort | telemac facade + family steps (`workflow.py` 184 + `water_quality.py` 77 new; `forcing.py` +72, `substance.py` +26, `solve.py` +22, `reach.py` +13, `__init__.py` +17, `deck.py` +1, `products.py` -24) | 2874 | 3262 | +388 | +934 |
| 2026-08-24 | 2 - skeleton + cohort | shared steps (`shared/aoi.py`) | 0 | 68 | +68 | +1002 |
| 2026-08-24 | 2 - skeleton + cohort | cohort templates (`do_sag.py` 399->324, `river_dye.py` 698->530) | 1097 | 854 | -243 | +759 |
| 2026-08-24 | 2 - skeleton + cohort | deleted outright (`do_sag/steps.py`) | 278 | 0 | -278 | +481 |

**Wave 2 verdict: net +481 - invested in the skeleton, not yet repaid; watch.**
The cohort of two templates shed 521 lines (-38% across the three template
files) against 664 lines of NEW fixed machinery (lib `workflow.py` + `slots.py`
480, TELEMAC facade 184). About 215 of the template reduction is RELOCATION into
the family-step tier (the resolved-input review, the WAQTEL process block, four
wire coercions) rather than absorption, so the honest per-template absorption is
roughly 150 lines each. Projection to watch at the next wave: the lib cost never
recurs, the facade cost recurs once per ENGINE (~184), and the saving recurs
once per TEMPLATE (~150-260). A one-template engine roughly breaks even; the
TELEMAC family alone (6 templates) should turn the running net negative. If wave
3 does not move the running net down, the generalization is not paying and the
slot/facade surface needs cutting, not extending.
