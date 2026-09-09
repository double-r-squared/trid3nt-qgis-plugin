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
- tool-list.md - the exhaustive V&V primitive list (verify + calibrate),
  mapped to package functions; the concrete build target
- open-questions.md - what must be answered before anything is built

The fetcher fold wave's measurements also live here, and are records rather than
design space:
- fetcher-fold-census.md - the two-lens census by protocol family, 97 specs
- fetcher-fold-stage0.md - THE TRADE, per library, measured before any spec moved
- fetcher-fold-raster-half.md - the STAC stage
- fetcher-fold-hydro-stage.md - the HyRiver stage, its parity and the FEMA NFHL wall
- nlcd-manning-tables.md - our NLCD -> Manning table beside pygeohydro's, class by
  class, with each one's published source. A DESIGN-STOP for NATE: swapping tables
  changes run numbers, so nothing is switched
- section-vs-hyriver.md - pynhd.flowline_xsection and py3dep.elevation_profile
  measured against the section cut and the profile sampler. Neither folds
