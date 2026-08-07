# ADR 0167 - MODFLOW advanced-package MVR/SFR front (Glover + Mover V&V gates)

Date: 2026-08-07
Status: accepted

## Context

The MODFLOW advanced-package front (M/L sign-off shortlist, MODFLOW section:
row 24 `advanced_package_mover_routing_uzf_sfr_lak_wel` [CAND-M], plus the SFR
stream-aquifer / stream-depletion class) targets the stream-aquifer interaction
question class: stream depletion by pumping, gaining/losing reach diagnosis, and
MVR routing between advanced packages. A SFR feasibility smoke fixture already
ships (`services/workers/modflow/fixtures/sfr_smoke/`).

Triage-first against the installed engine (flopy 3.10.0 + local `mf6` 6.7.0 at
`bin/mf6`; in-process via `asyncio.to_thread`, NO image lane - the template runs
`mf6` directly on the agent host like the ADR 0153 package-validation surface)
established, per row, what the machinery already supports:

1. **The georeferenced SFR stream-depletion product already exists.** The
   `sustainable_yield` composer's `stream_depletion` branch already fetches an NHD
   flowline (`fetch_river_geometry`), drapes an MF6 SFR6 reach network onto the
   grid, runs the `stream_depletion` archetype, and emits a `StreamReachLayerURI`
   carrying `total_depletion_m3_day`, `depletion_fraction`, `gaining_reach_count`,
   `losing_reach_count`, and per-reach charts. The board's "SFR stream-aquifer
   exchange on a georeferenced AOI with gaining/losing diagnosis" row is SUBSUMED
   by that surface (no new tool serves it).

2. **The wellhead-track river-capture-fraction pairing is already answered.** The
   IDEAS wellhead rung "NHD river as a head-dependent boundary (river capture
   fraction)" asks, for a pumping well near a reach, how much of its water is river
   capture. That IS `depletion_fraction` (stream capture / pumping rate) on the
   existing `stream_depletion` branch. SUBSUMED - no new surface.

3. **The genuine gap is the closed-form V&V gate.** Nothing validated the SFR
   depletion against the Glover (1954) analytical, and no MVR-conservation gate
   existed. These are computed-vs-analytical benchmarks (the ADR 0153 chart-only
   synthetic pattern), not place-based products - they fold as CASES onto the
   existing `modflow_package_validation` template, NOT as new registered tools.

## Decision

Add TWO cases to the existing `modflow_package_validation` template (engine core
`agent/mesh/modflow_package_validation.py`; NO new registered tool - registry and
EXPECTED_TEMPLATES stay flat, a zero-growth disposition). Both are gated on a
resolvable `mf6` (`$TRID3NT_MF6_BIN` -> PATH -> repo `bin/mf6`).

LANDED (cases of the one template):

- `sfr_stream_depletion` (GWF-SFR + WEL) -> **Glover & Balmer (1954)** transient
  stream-depletion V&V. A confined single-layer 6000x4500 m domain (T=200 m2/d,
  S=0.1), a well-connected SFR stream along the west edge, and a Q=400 m3/d well
  300 m from the stream. Stream depletion is extracted by SUPERPOSITION - the
  SFR->GWF leakage difference between a pumping and a no-pumping run - so the
  baseline stream stage / aquifer mounding cancels and only the well's capture
  remains (exactly what Glover's drawdown-only closed form predicts). The
  depletion fraction q(t)/Q is compared to `erfc(sqrt(a^2 S/(4 T t)))` across
  7 times (2..160 d). A well-connected streambed reproduces the fully-penetrating
  Glover curve; the georeferenced `stream_depletion` composer uses realistic
  (lower) streambed K and correctly sits BELOW this bound (stated loudly).
  Acceptance: max relative error over the resolved (q/Q >= 0.05) window < 0.15,
  monotone increasing, fractions in (0,1).

- `mvr_routing` (GWF-MVR) -> the **row-24 mover** conservation gate. A synthetic
  10x12 x 100 m watershed cell block where a UZF column rejects the infiltration
  (2 m/d) its vertical Ks (0.5 m/d) cannot accept, a DRN discharges groundwater,
  and MVR routes BOTH into the head reach of an 8-reach SFR network. The V&V is
  mover mass CONSERVATION within one coupled timestep: the volume SFR receives
  (FROM-MVR) equals the sum drawn from the providers (UZF rejected-infiltration +
  DRN discharge TO-MVR). DRN stands in for the "LAK/WEL discharge" of the board
  row - an equivalent mover provider; the conservation invariant is identical.

Both emit a computed-vs-reference chart + typed `ModflowValidationResult` (the
existing contract; `schematic_only=True`, `basis="synthetic"`, `SyntheticInput`
provenance). No spatial layer (schematic decks), so the proof is a chart.

## Consequence

`modflow_package_validation` gains a Glover stream-depletion V&V and an MVR
mass-conservation V&V - the closed-form gates the georeferenced stream-depletion
product lacked. The agent can now answer "does the SFR-coupled depletion match the
Glover analytical" and "does MVR conserve mass routing rejected UZF + discharge
into SFR" against a known answer, and the georeferenced `stream_depletion` /
gaining-losing product (which already existed) is now anchored by a closed-form
gate above it.

WORKER-IMAGE LAW (ADR 0148): NOT triggered. The cases run `mf6` directly via flopy
on the agent host (the ADR 0153 local-exec path); they do NOT touch the container
COPY set or the run_modflow supervisor. No image rebuild.

Zero deletions (nothing superseded). No deletion-ledger entries.

Further rows / wellhead-track: the georeferenced SFR product + river-capture
fraction are already SHIPPED on `sustainable_yield`; remaining wellhead rungs
(transient pumping schedules, heterogeneous K from SSURGO, kriged potentiometric
surface) stay QUEUED for when NATE picks up the wellhead track.

## Evidence

- Offline slice (from repo root, `env -u TRID3NT_CACHE_BUCKET pytest`,
  `$TRID3NT_MF6_BIN=bin/mf6` so the gated V&V solves run):
  test_modflow_package_validation = 24 passed (incl. the two new deck-authoring
  asserts + the two new real-solve V&V). Pins/hygiene: test_catalog_surfacing
  (registry UNCHANGED) + test_door_dissolution (EXPECTED_TEMPLATES UNCHANGED) +
  test_categories + test_template_hygiene = 36 passed. Regression:
  test_modflow_archetypes + test_run_modflow + test_gwt_adapter = 153 passed,
  13 skipped (pre-existing env-gated real-run skips).
- Model-free retrieval gate (index warmed, k=8): the Glover/MVR VALIDATION
  phrasings ("validate stream depletion ... Glover analytical", "does MVR route
  rejected UZF infiltration into SFR ... conserving mass", "Glover stream
  depletion fraction versus time") surface `modflow_package_validation`. The
  real-site PRODUCT phrasing ("how much of my well's water is captured from the
  stream") correctly routes to `modflow_capture_zone` / `modflow_sustainable_yield`
  (the georeferenced product), NOT the synthetic gate.
- Live V&V (local mf6 6.7.0): `sfr_stream_depletion` validated - late-time
  depletion 0.700 vs Glover 0.708 (delta 0.008), max relative error 4.3% over the
  resolved window, monotone; `mvr_routing` validated - UZF rejected 52482.5 + DRN
  3287.3 = 55769.9 m3/d into SFR, received 55769.9 (conservation delta 0.0).
- Proof charts (through the plugin chart dock's own `render_spec`):
  docs/proof/templates/modflow_package_validation_sfr_stream_depletion.png (MF6 SFR
  vs Glover erfc, both curves, delta in the caption strip),
  _mvr_routing.png (provider bars reaching the providers-total rule, conservation
  delta 0.0 in the caption). No map proof (schematic decks).

## Registry / pins

- TOOL_REGISTRY UNCHANGED; EXPECTED_TEMPLATES UNCHANGED; categories UNCHANGED
  (two new CASES on the existing `modflow_package_validation` tool). CODED tools
  this landing: +0 (zero-growth disposition). Rolling coded-tools metric is
  unchanged by this landing.
