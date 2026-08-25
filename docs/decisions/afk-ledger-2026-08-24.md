# AFK design-decision ledger - opened 2026-08-24

NATE went AFK after the cohort LGTM + six redline rulings + fleet order
(TELEMAC -> MODFLOW -> SWMM). Every DESIGN decision the orchestrator makes
autonomously while he is away is recorded here, newest last, with enough
to roll it back. NATE-made rulings before AFK live in docs/IDEAS.md
(2026-08-24 entries) and are NOT repeated here.

Format per entry:
- WHAT: the decision, one paragraph.
- WHY: the forcing situation.
- COMMITS: hashes that embody it.
- ROLLBACK: how to undo (revert range + any state to clean up).
- CONFIDENCE: how much this needs NATE's eye (LOW = mechanical
  consequence of a standing ruling; HIGH = genuine judgment call NATE
  should re-make himself).

Verification regime while AFK (standing, per NATE's parting directive):
every migrated template proves itself through the EXISTING driver/LiveRun
harness (repeatability - same driver, same coarse params, before vs
after), spot-check renders persisted to docs/proof/templates/ via the
scripts/ diagnostic lane, 4-lens adversarial panel at each engine-family
close, offline suite at the 4-failure environmental baseline, LOC ledger
rolling net at every landing.

---

## Entries

### 1 - CONSTANT-door wire enforcement is a SCHEMA exclusion, not a runtime refusal

- WHAT: the CONSTANT-door ruling is implemented by narrowing the
  model-facing surface only. `_wire_params` in the registration factory is
  the ONE definition of the wire set, and both the synthesized signature
  and the rendered docstring read it, so a CONSTANT param appears in
  neither. The RUN still honours a constant that arrives anyway: the
  generated body takes `**wire` and the sheet is filtered by DECLARED name,
  so a value from the `!run` / Tier-A lane or a form edit is seated through
  the USER door with `basis=user`.
- WHY: the ruling and the standing verification regime pull in opposite
  directions if "off the wire" is read as "ignored at runtime". The Tier-A
  canary declarations pin CONSTANT rows on purpose (do_sag coarse pins
  `sim_duration_s=600` and `mesh_resolution=coarse`, which is the whole
  reason it runs in minutes), and the kickoff requires that lane to keep
  working. There is no lane marker distinguishing a model call from a
  `!run` at the dispatch seam - both reach `TOOL_REGISTRY[name].fn(**args)`
  - so a runtime refusal would have to invent one. NATE's own ruling text
  says "the factory excludes them from the synthesized tool signature",
  which is exactly what landed; the ParamSheet clause ("they remain
  form-editable surfaces for the user") is what makes honouring a
  user-supplied value the correct reading rather than a loophole.
- COMMITS: the Part-1 library commit of the TELEMAC family wave.
- ROLLBACK: revert that commit. To get the stricter reading instead, filter
  CONSTANT names out of `Workflow._normalize`'s `declared` set and give the
  harness its own supply channel (a `dev-tool-invoke` marker, or a
  `LiveRun` field that re-seats constants after resolution).
- CONFIDENCE: MEDIUM. The mechanism is a mechanical consequence of the
  ruling, but the EDGE - what happens to a constant supplied outside the
  model schema - is a judgment NATE may want to re-make. Nothing else
  depends on the choice.

---

LEDGER CLOSED 2026-08-25: NATE returned mid-TELEMAC-family wave and
ordered "don't proceed past telemac". In-flight wave completes (close-out
+ owed packets + family panel); MODFLOW/SWMM/fix-batch/rain_on_grid/3D
builds HELD for his explicit go. Autonomous decisions made while AFK:
entry 1 (CONSTANT-door edge, MEDIUM confidence - re-make if desired).
Everything else executed under his own pre-AFK rulings.
