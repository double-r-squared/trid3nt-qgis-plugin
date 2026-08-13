# ADR 0149 - OpenQuake epistemic logic-tree + UHS/multi-PoE folded into openquake_psha knobs

Date: 2026-08-05
Status: accepted

## Context

Four OpenQuake CAND-S board rows were queued: classical PSHA with a non-trivial
source-model logic tree (row 1), G-R a/b + Mmax epistemic uncertainty (row 2,
the GEM LogicTreeCase2 324-realization lineage), Uniform Hazard Spectrum + hazard
map at 10%/2% PoE (row 3), and a scenario-rupture ground-motion-field realization
set (row 4).

Triage against the installed engine (oq 3.25.1) and its bundled GEM demos:
- The existing `openquake_psha` surface renders only a classical single-branch
  deck (trivial 1-branch source/GMPE logic trees, `quantiles =` empty, single
  `poes`), already with UHS export plumbing (env-gated) + a hazard-curve chart.
- Rows 1, 2, 3 are all extensions of that classical deck. Row 4 is a different
  `calculation_mode = scenario` calculator (rupture model + GMF fields), not a
  classical-deck extension.
- Running LogicTreeCase1/Case2 + a multi-PoE/UHS area-source deck verbatim on
  oq 3.25.1 confirmed each mechanism runs in 8-14 s and exports
  `quantile_curve-{0.05,0.5,0.95}` + `hazard_map-mean-{475y,2475y}` +
  `hazard_uhs-mean`.

## Decision

Fold rows 1, 2, 3 into `openquake_psha` as knobs (no new tool; registry stays
210, EXPECTED_TEMPLATES stays 52) - the "prefer fewer well-knobbed surfaces"
discipline:

- `logic_tree`: `"single"` (default, byte-identical classical deck) |
  `"source_models"` (two competing weighted source models + 2 GMPEs,
  LogicTreeCase1) | `"gr_uncertainty"` (abGRAbsolute + maxMagGRAbsolute 3-way
  branches on a two-source model x 2 GMPEs per TRT = 324 realizations,
  LogicTreeCase2). Both epistemic modes bypass the real-fault fetch (they use a
  synthetic AOI demo source), turn on `quantiles = 0.05 0.5 0.95` +
  `individual_rlzs`, and emit a 4-line (mean + 5/50/95) quantile-spread chart.
- `secondary_poe`: a second PoE (e.g. 0.02) -> `poes = 0.1 0.02` so the deck
  exports the hazard map at BOTH return periods (475y + 2475y).
- `uniform_hazard_spectra`: exposes the existing UHS export as a user knob (was
  env-gated) -> UHS SA-vs-period chart.

Row 4 (scenario GMF) is NOT folded here - it is a distinct calculator warranting
its own tool; recorded as a validated STOP with a deck-confirmed recipe.

## Consequence

- Worker deck renderer (`services/workers/openquake/job_ini.py`) gains the
  multi-branch logic-tree renderers, a two-area-source model, and multi-PoE /
  quantiles / individual_rlzs job.ini options. All ADDITIVE: `logic_tree` absent
  / "single" renders the classical deck byte-for-byte (locked by test).
- The `OpenQuakeDeck` grows an `extra_files` map so competing-source-model decks
  can write `source_model_1.xml` / `source_model_2.xml`; the entrypoint + local
  shim write them.
- The chart-dock interpreter does not render `area` marks, so the epistemic
  spread is drawn as four line series grouped by a color field (mean + q05/q50/
  q95), which the dock renders natively.
- No worker IMAGE rebuild was required for the local (offline) build: the local
  solver runs `run_oq.py` as a subprocess against the repo `job_ini.py` directly.
  A cloud/Batch deployment MUST rebuild the openquake image (ADR 0148) before the
  new spec fields take effect there.
