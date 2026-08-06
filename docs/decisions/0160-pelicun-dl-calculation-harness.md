# ADR 0160 - Pelicun DL_calculation CLI harness + the two 0146 STOP rows land on it

Date: 2026-08-06
Status: accepted

## Context

The M/L sign-off shortlist (docs/validation/ml-signoff-shortlist.md) ranks the
Pelicun DL_calculation CLI harness as the #1 machinery front: one ~2-4 h build
unblocks ~10 board rows. ADR 0146 had explicitly STOPped two of those rows with
recipes rather than build them, because pelicun's `DL_calculation.run_pelicun`
is cwd-sensitive (it resolves the config / demand / auto-population script
relative to the working directory and writes ~20 output files into it) - fragile
even in the monolith. The two STOPs were:

1. `auto_populated_building_type_seismic_run` - push AIM building attributes +
   a demand through `--auto_script` and reproduce pelicun's checked-in e1 example.
2. `hazus_eq_v5_vs_v6_dataset_comparison` - no v5.1 buildings resource alias
   exists, so the v5.1 fragility/consequence tables must be loaded by path and
   compared against the v6.1 alias at the Assessment-API level.

## Decision

Build the harness once, properly, and land both STOP rows on it as the
acceptance.

**Harness** (`workflows/pelicun/_dl_calculation.py`, `run_dl_calculation`): copies
the AIM config + demand CSV into a fresh tempdir, injects a fixed `Seed` +
`SampleSize` into the config's DL Options (which makes the otherwise-unseeded
Monte-Carlo run byte-reproducible - verified: seed 42 reproduces exactly across
runs), then, under a module-level `threading.Lock`, snapshots cwd, `os.chdir`'s
into the tempdir, runs `run_pelicun`, restores cwd, and restores pelicun's global
`LoggerRegistry._loggers` (dropping the file-backed logger the run registered so
it cannot dangle at the deleted tempdir and raise from the import-time
`sys.excepthook` pelicun installs). Outputs are read into a typed
`DLCalculationResult`; failures raise a loud typed `DLCalculationError`. All
compute runs via `asyncio.to_thread`.

Isolation choice: in-process tempdir + serialized cwd, not subprocess. pelicun is
already an in-process dependency of all four existing pelicun templates; a
subprocess would add re-import latency and lose the typed exception surface. The
process-global `os.chdir` hazard is fully contained by the lock (serializes the
cwd window across worker threads) plus the per-call unique tempdir - correct in
the one-daemon-one-user monolith. This harness is the reusable spine for the rest
of the Pelicun family (wind-only, wind+surge next) - the #31
`custom_model_dl_calculation_cli_wrapper` row is satisfied by the harness itself,
not a separate tool.

**Two templates on it:**

- `pelicun_hazus_seismic_dl_run` (row 1): builds a HAZUS-earthquake AIM from
  building attributes (structure type / height class / design level / occupancy /
  lifeline flag - knobs, defaulting to pelicun's e1 fixture), drives the harness on
  the bundled e1 PGA demand, and reports the auto-populated component, the DL
  output manifest vs e1's checked-in reference set, the coupled-EDP demand
  reproduction, and a seeded loss summary. Chart: repair-cost loss-exceedance
  curve.

- `pelicun_hazus_eq_version_comparison` (row 2): loads the v5.1 and v6.1 seismic
  building fragility + consequence CSVs by path and runs the same building type
  through pelicun's real damage + loss pipeline under an identical demand + seed,
  reporting the damage-state-probability shift, the mean-repair-cost shift, and the
  coverage delta. Chart: grouped DS-probability bars, v5.1 vs v6.1.

## Consequence

Registry 217 -> 219; templates 59 -> 61 (EXPECTED_TEMPLATES bumped; the four
`test_catalog_surfacing` registry pins bumped 217 -> 219; both filed under the
`damage_assessment` primary category with corpus.yaml queries; model-free
retrieval hits both on all probe prompts).

Deltas vs the checked-in references, reproduced:

- Row 1: the e1 AIM (C1 low-rise pre-code lifeline, EDU1) auto-populates the
  component `LF.C1.L.PC` and resolves the HAZUS v6.1 earthquake buildings dataset;
  the run reproduces e1's 20-file output manifest exactly (delta = 0 files -- e1's
  own test asserts existence only, its value checks are marked TODO); `coupled_edp`
  reproduces the input PGA demand to < 1e-6; seeded loss summary (seed 42, N=2000):
  mean repair-cost ratio 0.526, collapse probability 0.048.

- Row 2: for any building type present in both versions, the shift is exactly zero
  - v6.1 did NOT revise the 265 shared v5.1 components (fragility AND consequence
  are byte-identical). STR.C1.L.PC: DS-probability shift 0, mean repair cost 0.1166
  in both. v6.1's real change is coverage: it ADDS 58 components (56 STR + 2 NSA)
  at new SC/VC design levels absent in v5.1; requesting a v6.1-only type
  (e.g. STR.W1.SC) raises a loud typed error on the v5.1 branch.

No deletions (pure addition; no ledger candidates). Offline slice green: the two
new templates' gate (test_pelicun_dl_calculation_harness.py, 9 tests) plus
test_pelicun_validation_templates, test_categories, test_template_hygiene,
test_catalog_surfacing, test_door_dissolution -- 56 passed. Proofs rendered
through the dock's own interpreter to docs/proof/templates/.

Board rows this front now unblocks (beyond the two landed): wind-only HAZUS
hurricane run (#32), coupled wind+surge governing damage state (#33),
collapse-override, p58-db-sweep, story-level, custom-tsunami, and
component-wind-envelope - each an S-batch template that supplies a different
bundled AIM / DL_Method to the same harness.
