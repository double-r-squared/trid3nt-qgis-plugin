# Model wave - kickoff

STATUS: LAUNCHED 2026-08-28 per NATE's close-out rulings. Frozen on launch.
The spec is `docs/specs/gmsh-mesher.html` REV 19 (FROZEN for this wave's
duration) with `docs/specs/workflow-blueprint.html` rev 8 as parent; where
this kickoff and the spec disagree, the spec wins. Acceptance lane: DIRECT
INVOCATION + SCRIPTED tests ONLY - no UI work, no user testing.

## Objective

Land the frozen Domain-and-Mesh model: the aoi/extent declaration surface,
the full OceanMesh2D wrapper (the wrapping is the whole deal), the D-1/D-3/
D-9 rulings, the geometry-by-name server seam, and the 7-template surface
migration - on top of the closed mesh wave's foundation.

## Slices

1. **Declaration surface.** `aoi` becomes a REQUIRED, DEFAULTED,
   USER-EDITABLE declaration (geocode/canvas fills the default); `extent`
   param on `build_mesh`, ALWAYS declared in templates (`extent=P.aoi` the
   common case); the CONTAINMENT RULE binary (crop within staged coverage
   via a journaled `set_extent` edit; outside coverage escalates to the
   rerun primitive with the new bbox). Staging buffer NOT chartered
   (pending NATE). Mesher stays EXPLICIT everywhere - no auto-routing.
2. **D-3 (must-fix).** Template mesh steps (`ReachMesh.corridor`,
   `Catchment.mesh`) read the declaration WHOLE - mesher, kind, and the
   declared `.edit()` chain as the recipe prefix - never reconstruct it.
   Test: a declared edit on `river_dye` lands in the recipe and the built
   mesh; restart truncates to it. D-2's explicit-named-step shape is the
   ruled shape - keep it.
3. **D-1: all meshes editable.** The four reg_grid templates (agitation,
   coastal_tidal_surge, stratified_flow, wave_field) route through the
   tool: explicit named build step, session opens, gate presents in
   USER-GATED (set_extent/set_resolution at minimum), deck consumes the
   accepted artifact. AUTO stays inline and silent.
4. **The om2d wrapper - the core slice.** Wrap ALL of OceanMesh2D's exposed
   functions as the om2d mesher's param/edit surface, with tests.
   ACCEPTANCE BAR = the flagship's four measured fragilities, each fixed
   and RE-MEASURED (spec section 6): deterministic contour selection on
   holed domains; multi-section open boundaries (whole-rim-open forces on
   the whole rim); rim sizing honored within a declared tolerance; a
   lake-capable domain source (Marquette BUILDS).
5. **D-9: discovery offered at the gate.** Compatible case meshes
   (compat-filtered, extent-checked, stash + sidecar channels) OFFERED by
   name at the gate; declining builds the declared default; AUTO never
   adopts a discovered mesh. The incompatible-find tell/silent detail is
   DELIBERATELY UNSPECIFIED - implement the minimal offer, leave the rest.
6. **Geometry-by-name (server seam only).** Named case layer -> geometry
   resolution for geometry-valued edit inputs: name shows, durable id
   travels; ambiguous names refuse listing matches; not-found/wrong-type/
   empty refuse typed. The selectable boundary structure is the POLYLINE.
   NO plugin/UI work - the picker belongs to NATE's UI pass.
7. **7-template surface migration** to the frozen signature (extent always
   declared, explicit mesher, constituents). Deck byte-parity for
   semantically unchanged asks, diffs documented otherwise.

## Acceptance

- Scripted lane only: drivers + `!run` + offline suites. Five slices ZERO
  failures at every landing.
- The om2d bar (slice 4) measured, not asserted.
- LOC ledger rows per landing; DELETION_LEDGER for chops.
- SPEC-CONFORMANCE GATE as the mandatory final stage: fresh-eyes clause
  walk of FROZEN rev 19 + live scripted walkthroughs; deviations REPORTED,
  never fixed.
- Adversarial review at every stage boundary, refute-by-default.

## Constraints (standing)

Fetch + styles + run paradigm + static plans frozen. ASCII hyphens.
Comments = constraints only, no pattern names. Path-scoped commits; no
push until verified close. Worker touched => image rebuild + smoke.
DESIGN-DECISION RULE: a finding whose fix requires choosing semantics,
channels, structure or judgment-deletions STOPS the wave and is REPORTED
to NATE as a question - only mechanical fixes proceed unprompted.
