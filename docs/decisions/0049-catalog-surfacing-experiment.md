# 0049 - catalog-surfacing experiment: registry-shrink decision (INCONCLUSIVE)

Context: 14 spec-served data sources each register as an individually-declared
`tier="general"` virtual tool (`_router/registration.py`), so each occupies a slot
in the ambient declarable pool the model sees every turn. NATE's goal is for the
registry to SHRINK as generic surfaces absorb per-source functionality. The signed
experiment (`experiments/catalog_surfacing/DESIGN.md`, NATE 2026-07-30) compares
three arms to decide WHICH surfacing design shrinks the ambient pool without
degrading source selection or param validity: Arm 0 baseline (14 ambient); Arm 1
Design 1 (card-carried -- `search_data_catalog` returns full source cards,
`fetch_from_catalog(source=...)` routes, router-side validation is the sole gate);
Arm 2 Design 2 (discovery-expands-declaration -- the 14 leave the ambient pool but
stay indexed, and a `search_tools` hit gate-expands the matched per-source tool,
keeping the provider FunctionDeclaration). Sign-off: N=1, temperature 0, 2-pt noise
band, Design 2 FAVORED (surface stability governs) -- Design 1 advances only if it
beats Design 2 outside the band on BOTH selection accuracy AND first-attempt validity.

Decision:
- BUILD both arm prerequisites, identity-gated behind a reversible env flag
  (`TRID3NT_CATALOG_ARM` in {1,2}); DEFAULT config is byte-identical (registry 190,
  ambient declarable 170, `fetch_from_catalog` signature + docstring unchanged, the
  offline suite's 9-failure baseline unchanged):
  * New `tier="catalog"` (`EngineTier`): EXCLUDED from the default declarable pool
    (`_default_declarable_registry` + the tool-retrieval fail-open floor) yet KEPT in
    the search index -- diverging from `tier="template"` (which is also index-excluded).
    Under an arm flag the 14 specs register `tier="catalog"` (registry stays 190; only
    the ambient pool drops -14). Arm 2 needs NOTHING further: the existing
    `search_tools` gate-expander declares an indexed, pool-excluded source on a hit.
  * Arm 1: `fetch_from_catalog` grows a `source` branch (`_fetch_from_catalog_via_spec`
    -> `router.route`, router validation as the sole gate) ONLY under the flag;
    `search_data_catalog` returns spec CARDS (full untruncated docstring + typed param
    schema + gates/caveats/fallback) via `registration.search_spec_cards`.
- RUN the signed experiment model-in-the-loop through the production dispatch seam
  (stack default adapter = OpenRouter `nvidia/nemotron-3-super-120b:free`, temp 0,
  N=1), deterministic grading (fired/selected NAME vs acceptable set +
  `router.validate_params` pass/fail), model-free reachability precondition first.
- VERDICT: **NEITHER design advances; run flagged INVALID by the control gate ->
  the registry-shrink is NOT rolled out.** Do NOT delete per-source registration; do
  NOT switch any default surface. Keep the arm mechanisms as reversible, default-off
  scaffolding for a re-run.

Consequence:
- Model-free reachability was strong in all arms (recall 0.9904 = 103/104: the card /
  ranked discovery surface finds the correct source top-k). The mechanisms are SOUND:
  the one live Arm-2 discovery invocation worked end-to-end (`search_tools` ->
  `fetch_usgs_water_quality` declared-by-expansion + fired, first-attempt params
  valid); the Arm-1 2-hop was validated in isolation.
- But BOTH designs collapsed on selection: baseline 60.6%, Design 1 0.0%, Design 2
  0.96%. Cause is empirical, not structural -- the weak default free model almost
  never invokes discovery when semantically-adjacent ambient SIBLING tools are
  declared (arm1: `search_data_catalog` 11/104, `fetch_from_catalog` 0/104; arm2:
  `search_tools` 1/104). It fires an ambient sibling (`fetch_raws_weather`,
  `fetch_hrrr_forecast`, ...) or nothing.
- The control-identity gate FAILED (16/19 identical) purely on model non-determinism
  (2 arm1 NO_CALLs + 1 baseline geocode mis-fire), not a surfacing leak -- default
  config is provably byte-identical, so there is no real cross-arm routing leak; the
  gate is over-sensitive to a non-deterministic model.
- What rolls out: NOTHING to the live default. The registry-shrink go/no-go is
  UNDECIDED. Re-run the experiment with a capable model (and/or a system prompt that
  steers data needs to the discovery surface) before revisiting. The favored-arm
  tie-break (Design 2) is moot (neither advanced) and the surface-stability principle
  behind it is undisturbed. Supersede this ADR with the re-run's verdict; do not
  rewrite it.
