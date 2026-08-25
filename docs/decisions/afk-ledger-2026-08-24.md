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

(none yet - the TELEMAC family wave runs under NATE's own pre-AFK
rulings; the first autonomous judgment call lands here when it happens)
