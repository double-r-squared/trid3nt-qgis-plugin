# Simulation validation, review, and calibration

STATUS: THINKING - nothing here is implemented or approved for implementation.
This folder is the design space for making simulations reliable and accurate:
review (is the model set up sanely), validation (does it match observations),
calibration (adjusting parameters until it does). It is a large part of the
system and gets thought through fully before any action.

Files:
- research.md - primary-source research: per-engine calibration practice,
  numeric acceptance criteria, review checklists, parseable diagnostics
- responsibility-cut.md - the central design principle: which checks are
  machine-enforced, machine-assisted, or human-only
- roadmap-proposal.md - a PROPOSED build order (A-D). Not approved.
- activation-boundary.md - WHEN the loop applies (simulation-class only,
  via a tool metadata flag) vs the fast path for fetch/processing/query
- agentic-loop.md - the research/plan/execute/review/edit loop mapped to
  simulations; context isolation as the review principle
- open-questions.md - what must be answered before anything is built
