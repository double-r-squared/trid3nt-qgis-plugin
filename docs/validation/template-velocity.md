# TEMPLATE VELOCITY LEDGER

Updated at EVERY template close-out (standing rule). Times = builder-agent
wall-clock (incl its own suite runs + live smokes) + the orchestrator
close-out (~10-15 min: slice, commit, deploy, log). Sequential execution
per NATE 2026-08-04.

## Landings to date (wall-clock, measured)

| Wave (ADR) | Templates landed | Builder time | Per-template |
|---|---|---|---|
| HEC-RAS landing (0109) | 1 (engine #11 archetype) | ~86 min (incl 1 resume) | ~86 min - engine-class |
| SCHISM landing (0118) | 1 (engine #12 archetype) | ~84 min (incl 1 resume) | ~84 min - engine-class |
| S-tier wave 1 (0120) | 1 + the rename (+ the hygiene lint + 16 fixes) | ~80 min (2 resumes) | ~40 min effective |
| Easy-four pt 1 (0122) | 1 (folding 3 board rows) | ~47 min | ~47 min - feature-build |
| Easy-four pt 2 (0123) | 3 | ~71 min | ~24 min - recipes pre-scoped |
| Triage-only waves (0121) | 0 (13 ground-truthed) | ~35 min | scoping overhead, amortized |

## Working rates (sequential, incl close-out share)

- TRUE-S (knob/pre-scoped recipe): ~30-50 min each
- FEATURE-BUILD M (new worker branch/parser/postprocess): ~45-90 min each
- ENGINE-CLASS (new worker/image/contract family): ~1.5-2 h each
- TRIAGE overhead: ~30-45 min per unscoped batch of ~10 rows (buys the
  recipes that make the 24-min rate possible - the 0123 evidence)

## Projections (sequential; revised at every close-out)

- SWMM network family (in flight, 7 interdependent, 2 heavy): ~4-6 h
- HEC-RAS x2 + SCHISM x2 (signed, scoped): ~3-5 h
- Blocked-13 (need machinery waves first): machinery ~2-4 h each family,
  then template rates apply
- Easy tier (~98 CAND-S, triage-first at wave-2's honest conversion
  uncertainty): ~55-90 h of loop wall-clock
