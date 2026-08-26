# 0319 - Rerun-with-overrides: a run derives from a run, and the setter dies

## Context

Four `set_<engine>_parameters` tools did recalibration by editing a deck. Each
one copied a parent model directory, rewrote named values in the engine's own
file format, read them back, and returned a `SetterEnvelope` describing what it
had changed. `set_telemac_parameters` was the sharpest of them: TELEMAC has no
Python package that round-trips a `.cas`, so it hand-parsed a flat
`KEYWORD = value` deck, rewrote two value tokens in place, and carried a
LAW-AWARE bounds table because the FRICTION COEFFICIENT means a Strickler Ks
under law 3 and its reciprocal, a Manning n, under law 4.

Two things were wrong with that shape, and only one of them is about lines.

The small problem: it was per-engine. Four tools, four bounds tables, one shared
envelope, and nine engines to go.

The real problem: **it recalibrated a DECK, and a deck is not what anyone asked
a question about.** The setter's output was a child deck URI. Getting an answer
out of it meant dispatching that deck through a solver by hand, and the child
deck had no sheet, no provenance, no journal line and no relationship to the run
it came from. What the user wants after seeing an answer is the same question
again with one value moved - and that is a run-level operation that the deck
level cannot express, because everything upstream of the deck (the geocode, the
reach navigation, the discharge read, the terrain) is invisible from inside a
`.cas`.

NATE ruled the shape on 2026-08-25 (decision 1, amended twice): the setter
family's SENTIMENT becomes skeleton machinery - rerun-with-overrides plus
declared coupled-validity rules - as a PRIMITIVE behavior of a workflow rather
than a calibration-track feature, with three consumers named: failure recovery,
manual what-if, and calibration loops. The sample-purity ruling of 2026-08-26
set the timing: it lands WITH `set_telemac_parameters` deleted in the same
series, because interim code inside the TELEMAC sample lies about the
architecture.

## Decision

**A run derives from a run.** `rerun_workflow(run_id, overrides={...})` loads the
parent run's own resolved sheet, seats the named overrides on it through the
USER door labelled `override of run <parent_id>`, re-derives every value that
depends on them, and runs the same template again.

Four things make that more than a re-invocation with different arguments.

**The sheet comes from the parent, not from the wire.** A re-invocation would
re-resolve every door from scratch: the discharge would come off a different
National Water Model cycle, the geocode could answer differently, and the
comparison would be between two runs that differ in more than the one value
named. Starting from the parent's SHEET means exactly one thing moved, which is
what makes the child an answer to "what if".

**Reuse is read off the plan.** `reuse.py` walks the declared reads - the same
walk the validator checks refs with and the interpreter binds them with, now one
definition in `plan.py` with three readers - and finds the first node an override
reaches. That index is a CUT: work before it is inherited, work from it on is
re-done. A prefix rather than a scatter, deliberately, because a step reads the
DOMAIN the steps before it bound and no declaration names that; claiming a node
past the cut is clean would be a claim the plan cannot support.

**Inheritance is the ledger, not a copy.** The parent's own `LedgerRecord`s are
planted under the child's invocation key (`StepLedger.seed`), and the
interpreter's ordinary resume path replays them. So the child does not re-fetch
and then compare - it never asks. The artifacts it reuses are the parent's
objects at the parent's URIs, and byte-identity is not a property that has to be
verified, it is the same object.

That required one new store. The step ledger TOMBSTONES itself the moment a plan
completes, and that tombstone is load-bearing: it is what keeps a
`live-no-cache` tool from quietly becoming a result cache. So the completed run's
records are copied out to a RUN SNAPSHOT keyed by run id (`snapshot.py`, 30-day
TTL), reachable only by a caller that NAMES that run. The tombstone rule is
untouched: a fresh invocation of the same question still re-fetches the world.
Naming a past run is a different request and gets a different answer.

**From the sheet on, the child is an ordinary run.** It gates, ledgers,
journals, publishes an answer and leaves a snapshot of its own - which is what
makes a child a parent, and a calibration loop nothing more than this primitive
driven by a proposer.

### Coupled validity

A `Param` declares its own bounds, and a bound is a statement about one value.
The setter's law-aware table existed because no per-param bound can say that
`friction_coefficient` MEANS something different depending on `friction_law`.
So declarations may now carry `Validity` rules: a name, the params it reads, a
predicate over the sheet, and the message it refuses with. The library owns the
mechanism and checks it at resolve time on BOTH lanes; the engine or template
owns the rule, because only they know what their params mean together.

The first rule is the setter's, ported: `friction_coefficient_matches_law` on
`coastal_tidal_surge`. It compares against the CROSSOVER (Manning n = 1/Strickler
Ks, so the two bands sit either side of 1) rather than against each law's
plausible band, and that choice is the whole design. An ATYPICAL value the caller
means - a glass-smooth Ks of 120 - is theirs to set and still proceeds, exactly
as the setter's own bounds policy said it should. A value on the wrong side of
the crossover is not atypical; it is the other quantity, and it refuses typed.
The declared bound on `friction_coefficient` widened to `(0.001, 200.0)` to span
all three laws, because a single band cannot hold both and the only
law-independent physical fact is that the coefficient is positive.

### Failure recovery

The third consumer needed one more thing. A failed run leaves records in the step
ledger, but that ledger is keyed on the param VALUES - so re-running with the bad
value CORRECTED is a different key and would replay nothing, which is precisely
the retry a failure wants. So a failed attempt is now recorded like a completed
run, under a fresh id the error envelope names:

    step 'reviewed_discharge' failed: bank_source 'riverbank' is neither of the
    two the mesher builds ... This attempt is recorded as run 01M0ZXWF1AT..., with
    reach, seed, carrier_discharge, waqtel already done: rerun_workflow(run_id=
    '01M0ZXWF1AT...', overrides={...}) re-runs the question with the value
    corrected and inherits that work.

An attempt that finished nothing offers no handle, and the envelope stays plain.

### The constant door

CONSTANT-door params ARE overridable through this tool, and the docstring says
so. The constant door is a contract about what the MODEL's plan schema offers -
non-question physics the model has no business inventing. Recalibration is a
different surface: naming a value EXPLICITLY, by an agent or a user who has seen
an answer and wants one thing moved, is the sanctioned way a fixed quantity
moves. The proof exercises it (`bank_source` is a CONSTANT and is what the
failure-recovery leg corrects).

## Evidence

Live, on the `telemac_do_sag` refined canary (Eel River near Scotia, 10 m mesh,
600 s), full run in `docs/proof/templates/telemac_do_sag/rerun/`.

| claim | measured |
|---|---|
| reuse cut | `k1_per_day` cuts at node 4, `waqtel`; `mesh_resolution_m` cuts at node 6, `deck` |
| inherited byte-identical | `reach`, `seed`, `carrier_discharge` record sha256 IDENTICAL parent vs child |
| re-executed | `waqtel`, `deck`, `solve`, `do_field` - all four moved |
| the Data never even asked for | `data:rivers` has no child record: the step that reads it replayed, so the flowline was never fetched |
| the solver's own inputs | `bed_source.tif` is the SAME content-addressed object; `river_centerline.geojson` / `river_banks.geojson` re-staged under a new prefix, byte-identical content |
| direction | k1 0.3 -> 0.9 moves DO min 9.0081 -> 8.9804 mg/L. More deoxygenation, deeper sag |
| what-if fan | two children off one parent: `k1_per_day` -> 8.9804, `k2_per_day` 0.9 -> 3.0 -> 9.0082 (more reaeration, shallower sag) |
| chain | child journal line carries `parent_run_id`, `overrides: ["k1_per_day"]`, `replayed: [reach, seed, carrier_discharge]`, and the k1 row reads `door=user basis=user note="override of run 01M0ZXQT..."` |
| failure recovery | `bank_source="riverbank"` refuses typed at `reviewed_discharge`; the attempt is recorded; `rerun_workflow(attempt, {"bank_source": "nhd_area"})` inherits `reach`, `seed`, `carrier_discharge`, `waqtel` and answers 9.0081 |
| law inversion | law 3 -> 4 with the coefficient left at 40 refuses `COUPLED_VALIDITY_REFUSED` before anything runs; naming both (4, 0.033) proceeds; Ks 120 proceeds |
| wall time | parent 63.7 s, child 50.5 s |

The wall-time saving is the least impressive number here and is worth stating
plainly: on this canary the inherited work is a geocode, an NLDI navigation and a
pinned discharge, all of which are seconds and some of which the router already
caches. The reuse that MATTERS is not speed, it is IDENTITY - the child is
measured against the parent's own inputs rather than against a fresh read of a
world that moved.

**A plan-shape observation the proof surfaced, recorded rather than fixed.** The
reuse is exactly as fine-grained as the plan's node boundaries. `telemac_do_sag`
stages its terrain inside the `deck` step, so a `k1_per_day` override - which has
nothing to do with terrain - re-executes the step that stages it. The bed is the
same object anyway, because the router's cache is content-addressed, so nothing
is wrong; but a template that wants finer reuse declares its bed as its own
`Data` or its own step. That is a template-authoring note, not a defect in the
primitive, and it belongs to whoever next looks at the reach family's plan shape.

## Gates

- Five slices: 1748 / 6736 / 2161 / 1752 passed, **zero failures**; contracts 789 passed.
- Canary replay, 13 of 13 replayable: **all IDENTICAL** (`scripts/replay_canary_evidence.py`,
  reports in `docs/proof/templates/canary_replay*.json`). Four of them - both
  `coastal_tidal_surge`, `artemis_harbor_resonance_idealized`,
  `telemac_rain_on_grid` - were recorded in `user_gated` sessions and cannot run
  headless at all: law 9 refuses their physics-consequential labeled defaults
  when there is no card to approve them on. They replay under
  `--approve-defaults`, which supplies those DECLARED defaults by name - the same
  values through the user door instead of through the card - and the flag reports
  which rows it approved. `coastal_tidal_surge` is the template this wave touched
  and both its canaries are 18/18 identical. The 14th evidence file,
  `telemac_river_dye/coarse`, predates the `tool`/`args`/`metrics` fields and
  describes a run nobody can re-issue; its refined sibling is identical.
- `scripts/ws_smoke.py` all_passed=True; `scripts/run_sfincs_direct.py` status=ok
  with the depth COG published.
- `rerun_workflow` retrieval: top-8 for all three canonical phrasings, and the
  registry-wide corpus checks (`test_every_registered_tool_has_corpus_queries`,
  `test_no_dead_corpus_keys`) pass with the setter's corpus gone.
- Registry stays at 260: one tool out, one in.

## Consequences

`set_telemac_parameters` is DELETED - module, corpus, package dir, registration
import, and its 462-line test file, which is replaced by
`tests/test_rerun_with_overrides.py`. `docs/DELETION_LEDGER.md` carries the
one-for-one capability mapping and names the single thing NOT carried over:
editing a deck the workbench did not author. That was never reachable from the
product - nothing produced such a URI - and importing a foreign model is the
`DESCRIBE_MODEL` idea in `docs/IDEAS.md`, not a setter.

The other three setters are OUTSIDE the TELEMAC sample and keep
`workflows/lib/_setter_envelope.py` alive as a pre-migration lane. The module
now states that constraint in its first paragraph, and the new primitive does not
import it; the envelope dies with the last of them.

**Calibration readiness.** A calibration loop is this primitive driven by a
proposer, and the three pieces it needs are all here: a parent to derive from
(the snapshot), a way to move named values and get a comparable answer (the
override lane), and a chain to walk back (`parent_run_id` in the journal). What
is NOT here, and is the next wave's work rather than a gap in this one: the
OBSERVATIONS to score against, the objective that turns "this answer vs that
observation" into a scalar, and the proposer that picks the next override. The
loop driver should be a consumer of `rerun_workflow`, never a second
implementation of it - the moment a calibration lane grows its own re-run path,
the two will disagree about what a derived run is.
