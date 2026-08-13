# ADR 0163 - MODFLOW PRT capture-zone + BUY/Henry: two V&V cases fold onto ADR 0153

Date: 2026-08-06
Status: accepted

## Context

The M/L sign-off shortlist (docs/validation/ml-signoff-shortlist.md) put two
MODFLOW rows in the "ready now" high-value tier and busted the two blocker myths
against the installed binaries:

- Row 11 `prt_backward_capture_zone` - native mf6 PRT ships in the local
  `mf6` 6.7.0 (no MODPATH 7 install needed; the ADR 0153 STOP was moot).
- Row 12 `buy_density_driven_saltwater_intrusion` - GWT is already wired
  (`gwt_adapter`), so BUY is a small add.

Triage-first against the installed engine (flopy 3.10.0 + `mf6` 6.7.0 at
`$TRID3NT_MF6_BIN` -> `bin/mf6`, in-process via flopy, NO container image lane -
the same host local-exec path ADR 0153 uses):

- `flopy.mf6.ModflowPrt / ModflowPrtprp / ModflowPrtmip / ModflowPrtoc /
  ModflowPrtfmi / ModflowEms` all present; native PRT forward + backward (via a
  reversed GWF flow field) solve on 6.7.0. `flopy.mf6.ModflowGwfbuy` present; the
  BUY+GWT Henry deck solves.
- The cited example decks ship locally: `third_party/mf6.5.0_linux/.../examples/
  ex-prt-mp7-p01` (Pollock MODPATH7 example 1) and `ex-gwt-henry-a` (Henry 1964).

These two rows are the SAME shape ADR 0153 established: synthetic
computed-vs-reference V&V questions that cannot fold as a knob onto a place-based
composer, and so mint CASES of the `modflow_package_validation` template (a chart
+ typed-scalar carrier, never a georeferenced map).

### PRT system choice - a numeric reference, not just a qualitative one

The ADR 0153 STOP row anticipated NO numeric reference (the 3-layer
ex-prt-mp7-p01 notebook publishes no reference values, and its forward
capture-fraction is finicky at coarse particle release - a deep well fed by
diffuse aquitard leakage draws essentially no top-released particle down, and
backward traces hit the west no-flow wall). Rather than reproduce that
awkward system, this case uses the CLASSIC capture-zone benchmark that HAS a
closed-form analytical reference: a confined single-layer well in uniform
regional through-flow, whose down-gradient stagnation distance and up-gradient
capture width are the Grubb (1993) solution. So the native PRT is validated
NUMERICALLY, exceeding the STOP's qualitative-only expectation.

## Decision

Land TWO new cases on the existing `modflow_package_validation` template (the
tool count does NOT change - registry stays 222, EXPECTED_TEMPLATES stays 64; a
case is not a tool). Engine core `agent/mesh/modflow_package_validation.py`
(self-contained, flopy on the host; WORKER-IMAGE LAW ADR 0148 NOT triggered).

### LANDED: `prt_capture_zone` (native mf6 PRT vs Grubb 1993)

A confined single-layer 1210x1010 m domain (K=10 m/d, b=20 m, porosity 0.2), a
regional gradient set by a CHD inflow (west) and a RIV discharge boundary (east,
high-conductance -> the "river" the flow drains to), and a Q=60 m3/d well at the
centre. Native mf6 PRT (`ModflowPrt` + `ModflowEms`), backward tracking through
the flopy-reversed GWF head+budget (the canonical ex-prt-mp7-p02 method).

The case ALWAYS solves both directions; a `direction` knob
(`forward`|`backward`, default `backward`) selects the headline framing and the
`n_particles` knob (default 40) sets the backward release ring - "one PRT
template with direction/seed knobs beats two."

Validation basis (Grubb, S., 1993, "Analytical model for estimation of
steady-state capture zones of pumping wells in confined and unconfined
aquifers," Ground Water 31(1):27-32; system after mf6-examples:ex-prt-mp7-p01):

- Backward: max down-gradient particle excursion = 58.8 m vs the Grubb
  stagnation distance x_s = Q/(2*pi*U) = 57.8 m (rel 1.8%). U = K*b*i.
- Forward: captured inflow-band width = 303 m vs the Grubb dividing-streamline
  width evaluated AT the finite release distance (590 m up-gradient) = 331 m
  (rel 8.5%); the asymptote Q/U = 363 m is only reached as L -> infinity, so the
  finite-distance boundary (not the asymptote) is the honest reference.
- Internal consistency: all 303 forward particles terminate; 91 are captured;
  all 40 backward particles trace up-gradient to the west inflow boundary.

The `direction` knob is framing only - both metrics are always computed, so
`validated` is identical for both directions.

MODPATH 7 cross-validation stays a RECIPE (unchanged from ADR 0153): install
USGS MODPATH 7.2.001 + SHA-pin, run mf6-PRT and mp7 off the same GWF output,
exact-match per-particle termination + travel time. Native PRT itself lands now.

### LANDED: `henry_saltwater` (GWF-BUY + GWT vs the published Henry wedge)

The classic Henry 40-layer x 80-column vertical cross-section (2.0 m x 1.0 m,
K=864 m/d, porosity 0.35, diffc=0.57024), a freshwater WEL inflow 5.7024 m3/d
inland vs a 35 ppt GHB seawater boundary seaward, `ModflowGwfbuy` (drhodc=0.7)
coupling the GWT salt concentration to fluid density, two IMS solutions (GWF
before GWT).

Comparison basis: modflow6-examples ex-gwt-henry-a (Henry, 1964). The notebook
publishes the wedge FIGURE, not a reference table, so the check is pattern +
toe: the computed 0.5-relative-salinity isochlor toe penetrates 0.79 m inland
from the sea (bottom layer) vs the published ~0.79 m; the bottom salinity is
monotone increasing toward the sea; the domain is fresh inland-top and salt
seaward-bottom; the toe sits at an intermediate inland position - the classic
stable Henry wedge.

## Consequences

- MODFLOW gains a native-PRT particle-tracking V&V surface and a
  variable-density (BUY+GWT) V&V surface - two packages the place-based
  composers reach only as unvalidated products. The agent can now answer "does
  MODFLOW's PRT reproduce the analytical capture zone" and "does BUY reproduce
  the Henry wedge" against a known answer.
- Distinct from the place-based `modflow_capture_zone` /
  `modflow_saltwater_intrusion` composers (which render a georeferenced product
  over a real AOI): these cases are schematic V&V benchmarks (chart + scalars,
  `schematic_only=True`, `basis="synthetic"`, `SyntheticInput`).
- WORKER-IMAGE LAW (ADR 0148): NOT triggered - flopy on the host, no container
  COPY-set touch, no run_modflow supervisor touch, no image rebuild.
- Zero deletions (nothing superseded). No deletion-ledger entry.

### Adjacent shortlist rows - landed / deferred

- LANDED here: the PRT-front "forward pathlines + travel times" row is covered
  by `direction=forward` (forward pathlines from the regional inflow + the
  per-particle travel-time distribution).
- DEFERRED (need machinery outside this pass, listed honestly):
  - PRT-front `forward_transient_pathlines` (needs a transient GWF field) and
    `backward_lateral_injection` (a different release config).
  - MODFLOW advanced-package front (MVR/UZF/SFR/LAK, rows 24 + front #9) - the
    SFR smoke fixture exists but this is a separate ~2-4 h front.
  - MODFLOW GWE (heat) / GWT-tail (UZT/CSUB) / grid-type (LGR/DISU) - separate
    fronts.

## Evidence

- Offline slice (repo root, `env -u TRID3NT_CACHE_BUCKET pytest`, mf6 resolved to
  the repo `bin/mf6`): test_modflow_package_validation + test_categories +
  test_template_hygiene + test_catalog_surfacing (registry 222) +
  test_door_dissolution (64 templates) = 51 passed. Regression:
  test_modflow_archetypes + test_modflow_contaminant_plume + test_run_modflow +
  test_modflow_wave2_archetypes + services/.../test_gwt_adapter = 195 passed,
  13 skipped (env-gated real-run skips). NOTE: two of those (test_run_modflow's
  local-completion + the multi-species postprocess) FAIL only when
  `$TRID3NT_MF6_BIN` is a RELATIVE path (`bin/mf6`) because the run_modflow
  local backend launches mf6 from a scratch cwd; both PASS with an absolute
  `$TRID3NT_MF6_BIN` - a pre-existing env artifact, not a regression (the
  package-validation core resolves `bin/mf6` to an absolute path internally, so
  its V&V solves run under the relative env too).
- Model-free retrieval gate: `retrieve_visible_tools(prompt, None, 8)` surfaces
  `modflow_package_validation` for all new-case phrasings (capture zone /
  forward pathlines / Henry BUY / variable-density isochlor / backward
  stagnation).
- Live V&V (local mf6 6.7.0): prt_capture_zone validated (stagnation 58.8 vs
  Grubb 57.8 m, rel 1.8%; capture width 303 vs 331 m, rel 8.5%; 91/303 forward
  captured; 40/40 backward up-gradient); henry_saltwater validated (0.5-isochlor
  toe 0.79 m, monotone wedge). Both cases + GWF/PRT/Henry solves complete in ~8 s.
- Proofs (docs/proof/templates/, NEVER cleaned): the two PRT plan-view MAP
  proofs on a neutral synthetic background (captions say so) -
  `modflow_package_validation_prt_forward_pathlines.png` (the red captured band),
  `modflow_package_validation_prt_backward_capture_zone.png` (up-gradient
  pathlines + the Grubb stagnation marker); the Henry cross-section MAP proof
  `modflow_package_validation_henry_wedge.png` (relative-salinity field + the 0.5
  isochlor, scale pinned 0..1); and the two dock charts through the plugin
  `render_spec` interpreter - `modflow_package_validation_prt_grubb_chart.png`
  (computed/Grubb ratio bars vs the 1.0 rule) and
  `modflow_package_validation_henry_isochlor_chart.png` (isochlor interface).

## Registry / pins

- TOOL_REGISTRY 222 -> 222 (NO tool added; two CASES folded onto the existing
  `modflow_package_validation` template). EXPECTED_TEMPLATES 64 -> 64.
  categories.py unchanged (`modflow_package_validation` already
  hazard_modeling). CODED tools this landing: +0. The template's case enum goes
  3 -> 5 (`prt_capture_zone`, `henry_saltwater` added), with `direction` /
  `n_particles` knobs (PRT only).
