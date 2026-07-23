# Open questions - answer before building anything

SCOPE + PURPOSE
- What claim does a "validated" TRID3NT sim actually make, to whom? (screening
  vs design-grade; this bounds everything else)
- Which engines are in scope first? (hydrology first per NATE: SFINCS, SWMM,
  MODFLOW - confirm order)
- Where does liability/wording land - how do results phrase confidence
  honestly without implying engineering certification?

OBSERVATIONS
- Which observation sources are first-class for computed-vs-observed
  (USGS NWIS, CO-OPS tides - already fetchable; HWMs? satellite extents?) and
  what is the UX for the user supplying their own?
- Event selection: who picks the validation event/window, and how is that
  choice recorded?

ACTIVATION
- The activation boundary (activation-boundary.md) says the loop is a
  simulation-class property via a tool flag. Confirm the flag lives on tool
  metadata; confirm the fetch/processing/query classes are all fast-path.
- Within a flagged sim: tier-1 diagnostics always run (cheap); what gates the
  EXPENSIVE tiers (fresh-context review + re-run loop)? default-on / user mode
  / a stakes heuristic?

MECHANICS
- Where do validation results LIVE - on the run? the case? a report artifact?
  Do they persist and travel with exports?
- Does a failing tier-1 check block anything, or only warn? (current instinct:
  warn loudly, never block - confirm)
- Review card lifecycle: once signed off, is it immutable? re-opened on rerun?
- Calibration state: where do parameter sets/iterations live; how are
  calibrated vs default runs distinguished in the UI?

UNKNOWNS FROM RESEARCH
- CIWEM/WaPUG exact numeric bands unverified (PDF extraction failed) - do not
  hardcode until confirmed.
- MODFLOW 6 LST exact field names to confirm against a real listing file.
- SFINCS has no explicit mass-balance field - derivation approach must be
  validated against a known-good run.
- No precedent exists for boundary-condition linting - confirm it stays
  human-only or find prior art we missed.
