# ADR 0151 - SWMM mechanism-comparison templates for the 12 CAND-S board rows

Date: 2026-08-05
Status: accepted

## Context

Twelve SWMM CAND-S board rows were queued across hydrology (infiltration-method
comparison, pre/post-development runoff), hydraulics (node surcharge/ponding,
outlet-structure family, flow diversion, pump-curve wet well, duty/standby pump
alternation, multi-condition RTC), water quality (curb-length vs area buildup +
EMC vs exponential washoff), and LID (green roof, rain barrel vs rooftop
disconnection, vegetative swale). All twelve are S-tier hypotheses under the
triage-first law.

Triage against the installed engine (swmm-toolkit 0.17.0, swmm-api 0.4.74,
pyswmm 2.1.0) - every mechanism was verified to solve on a small synthetic deck
before any build:

- All three infiltration methods (Horton / Green-Ampt / Curve Number),
  imperviousness, ALLOW_PONDING, transverse + V-notch weirs, circular orifices,
  rating-curve outlets, pumps (Pump2 depth-flow curve) with CONTROLS rules
  (fixed / staged / multi-condition AND), all four LID types (short codes GR /
  RB / RD / VS), and buildup normalization (AREA vs CURB) + washoff (EXP vs EMC)
  run headless with sub-1% continuity.
- Version-specific facts learned in the probe: a `[REPORT] NODES/LINKS/
  SUBCATCHMENTS ALL` block is REQUIRED for the binary `.out` to carry the series
  the charts read; weir `[XSECTIONS]` need a positive width (Geom2); a wet-well
  pump needs a Pump2 (flow-vs-depth) curve, not Pump3 (head-difference, ~0 flow
  against a free outfall); each pump needs its own outfall (an outfall accepts
  one link); the CURB buildup normalizer needs a positive `CurbLen` on the
  subcatchment row; flow diversion under DYNWAVE is a raised-invert relief pipe
  (a `[DIVIDERS]` node is inert under dynamic-wave routing).

The existing SWMM surface (`swmm_urban_flood`, `swmm_network_import`,
`swmm_dual_drainage_coupling`, and the three ADR 0128 published-deck runners)
each runs ONE configuration. Every board row is a COMPARISON question (method A
vs B vs C), which cannot fold as a knob onto a single-run template without
changing its output contract - the same precedent that minted
`swan_physics_sensitivity_sweep` and the ELMFIRE sensitivity templates.

## Decision

Land ONE shared synthetic-comparison paradigm and FIVE knob-consolidated
comparison templates covering all twelve rows (registry 210 -> 215;
EXPECTED_TEMPLATES 52 -> 57):

- Engine core `mesh/swmm_mechanism_compare.py` authors small SYNTHETIC decks
  (verified syntax), varies one knob across variants, and solves each through the
  shared `swmm_deck_runner.solve_deck_text` + continuity gate (reused verbatim).
- Composer `workflows/swmm/mechanism_compare/mechanism_compare.py` solves the
  variants, builds ONE overlay chart (the compared PRIMARY series in one figure -
  the knob is the legend; the single-variant diversion plots its two link series
  instead), emits it, and returns the typed `SWMMComparisonResult` (a NOT-a-
  LayerURI carrier: schematic decks -> charts + typed scalars, no georeferenced
  map).

The five templates and the rows they fold:

| Template | Rows | Knob |
|----------|------|------|
| `swmm_subcatchment_runoff_comparison` | 1, 2 | `compare` = infiltration_method \| development_intensity |
| `swmm_node_hydraulics_comparison` | 3, 4, 5 | `scenario` = outlet_family \| flow_diversion \| surcharge_ponding |
| `swmm_wetwell_pump_control_comparison` | 6, 7, 8 | (control scheme comparison, no arg) |
| `swmm_lid_performance_comparison` | 10, 11, 12 | `lid_type` = green_roof \| vegetative_swale \| rainbarrel_vs_disconnect |
| `swmm_wq_buildup_washoff_comparison` | 9 | `compare` = normalization \| washoff |

The infiltration-method comparison uses a pervious (0% impervious) catchment +
an intense storm so the loss method fully controls the hydrograph; each method
carries representative (not cross-calibrated) default parameters, so the spread
honestly reflects the practitioner's method+parameter choice, not a clean
apples-to-apples identity. The WQ curb-length normalizer holds the buildup
coefficient constant, so the AREA-vs-CURB difference reflects the per-area vs
per-curb unit basis (the documented recalibration pitfall).

## Consequences

- Worker image: NOT rebuilt. The templates solve in-process via pyswmm (agent
  venv), exactly like the ADR 0128 deck runner - they never touch the
  `services/workers/swmm` container lane.
- New typed contracts `SWMMComparisonResult` + `SWMMComparisonVariant` (invariant
  1: the agent narrates the per-variant parsed scalars, never invents them).
- Retrieval: 5 co-located `corpus.yaml` (8 queries each), all surfacing top-8
  model-free; pins bumped (test_door_dissolution EXPECTED_TEMPLATES,
  test_catalog_surfacing registry == 215, categories.py, tools/__init__.py).
- Every landed template has a live smoke (all knob paths through the registered
  `TOOL_REGISTRY[name].fn`, continuity < 0.5%) + an overlay-chart proof rendered
  through the plugin dock's own `render_spec` interpreter, in
  `docs/proof/templates/<stem>_chart.png`.
- The board's roster_gaps stand: the EPA manual PDFs remain non-machine-
  extractable; the decks are synthetic mechanism stubs grounded in the cited
  examples, honestly labeled `SyntheticInput(basis="default_demo")`, not verbatim
  published-deck replications.
