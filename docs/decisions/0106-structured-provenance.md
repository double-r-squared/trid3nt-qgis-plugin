# ADR 0106 -- Structured input provenance (provenance-chain WAVE 2)

Status: accepted (2026-08-03, NATE-approved)
Follows: the template input-provenance audit
(`docs/validation/template-input-provenance-audit.md`) section 5b + ADR 0102
(WAVE 1 -- the wired fetchers + the two additive prose `source_note` fields this
wave structures) + ADR 0104 (the labeled buildings-absence + DEM-fallback
patterns) + ADR 0105 (the composer dissolution -- templates are single files now).

## Context

WAVE 1 (ADR 0102) wired the have-but-not-wired fetchers so a physically
dominant demo constant became real data, with the provenance carried as HONEST
PROSE in the result envelope (`source_note` / `fallback_note` / a summary
caveat). The audit's section 5b called for the next step: make provenance a
STRUCTURED field that (a) all templates can adopt incrementally, (b) survives
every result-narration path (not just the ones that happen to keep prose), and
(c) is backed by a general system-prompt rule telling the model to ASK for a
physical input it cannot fetch rather than invent one. This wave implements the
structured half.

## Decision

### 1. The typed field -- one shared model, carried by the envelopes

`contracts/common.py` gains `SyntheticInput` (a `GraceModel`) + the `InputBasis`
Literal + a pure `render_assumptions_line(entries)` helper. Per entry:
`{param, value, units, basis, real_source_if_any, note}` where
`basis in {fetched, user, prompt_interpreted, default_demo, derived}`.

The list is carried by the two result-envelope surfaces every template result
flows through, ADDITIVE + default-empty (`Field(default_factory=list)`):
- base `execution.LayerURI.synthetic_inputs` -- inherited by all 20+ LayerURI
  subclasses, so every template can populate it with no per-contract change.
- `impact_envelope.ImpactEnvelope.synthetic_inputs` -- the Pelicun aggregate.

An empty list means "the template has not declared its input provenance yet",
NEVER "all inputs are real". No enum grown; registry unchanged (contract-additive
only).

### 2. Populated this wave (the audit's known cases)

| template | entries | basis |
|---|---|---|
| `geoclaw_inundation` (dam-break) | `dam_break_depth_m`, `source_lonlat` | fetched (NID via `fetch_usace_dams`) or user |
| `geoclaw_inundation` (tsunami) | `fault_geometry`, `source_magnitude` | default_demo (the Okada relocation, item 3 below) |
| `landlab_susceptibility` | rainfall (`rainfall_intensity_mm_hr` / `recharge_mm_day`); `soil_properties` | derived (Atlas-14) / user; soil = default_demo |
| `modflow_asr` | `aquifer_k_ms`, `porosity`, `aquifer_sy`, cycle schedule | default_demo when defaulted, else user |
| `telemac_river_dye` | `discharge_m3s`, `bank_geometry` | fetched (NWM) / user; banks = fetched (NHDArea) or default_demo |
| `swmm_urban_flood` | `total_rain_depth_mm`, `drainage_network`, `overland_manning_n`, `building_obstructions`, `dem` | Atlas-14 fetched / user; synthesized network + flat Manning = default_demo; DEM fallback = fetched |
| `postprocess_pelicun` | `replacement_value` (when `n_assets_default_replacement_value > 0`) | default_demo (HAZUS class-default table) |

The GeoClaw + Landlab prose `source_note` fields (ADR 0102) are KEPT as the
human-readable line; the structured list is now the machine-readable source of
truth (see the ledger -- the standalone prose field is superseded, condition to
delete recorded, not chopped this wave because narration still renders a prose
line from the structured list).

### 3. The three narration-seam fixes (audit-pinpointed)

- **(a) bare-published-LayerURI path.** `adapter._summarize_published_scenario_layer`
  dropped everything but the render metadata, losing any provenance on the
  `sfincs_flood` / `swmm_urban_flood` peak layers. It now threads the structured
  field through (`_hoist_synthetic_inputs`) so the provenance reaches narration on
  that path too.
- **(b) Pelicun default-value flag.** `n_assets_default_replacement_value` was a
  bare int on the aggregate. `postprocess_pelicun._aggregate_gdf` now ALSO emits a
  structured `replacement_value` entry (basis=default_demo) when it is > 0, so the
  transparency reaches the assumptions line, not just a number the LLM may skip.
- **(c) GeoClaw tsunami Okada banner.** The worker printed the "NON-SITE-SPECIFIC
  synthetic source" honesty ONLY to `geoclaw.stdout` (`setrun_builder.py`). The
  same fact is now constructed DETERMINISTICALLY at the server (from the tool's
  `fault_*_deg` / `source_magnitude` params) as a structured `fault_geometry` +
  `source_magnitude` entry on the tsunami layer -- so the honesty rides the
  envelope into narration with NO worker image rebuild required. The worker
  stdout banner is left in place (a harmless run-log aid) and registered in the
  ledger as superseded.

### 4. Narration rendering (the seam that makes it reach the user)

`adapter.summarize_tool_result` gains `_hoist_synthetic_inputs`, invoked on the
published-scenario path, the general dict path, and the bare-LayerURI path
(mirrors the `fallback_note` hoist). When a result carries provenance it hoists a
ONE-LINE `assumptions_summary` (rendered by `render_assumptions_line`) + the
structured `synthetic_inputs` list to the top of the function_response, so the LLM
narrates demo-vs-site-derived without inventing phrasing. Chat stays tight (one
line, never a table).

### 5. The general system-prompt rule

`adapter.py` gained a general "Never invent PHYSICAL MODEL INPUTS" directive next
to the existing "Never fabricate numbers" (which covered only NARRATING result
numbers). The model asks the user for a physical parameter it cannot fetch or
derive -- and when a result carries `synthetic_inputs`, names which quantities are
demo defaults vs site-derived. Pinned by `test_system_prompt_forbids_inventing_
physical_inputs`.

## Consequences

- Two additive contract fields (`LayerURI.synthetic_inputs`,
  `ImpactEnvelope.synthetic_inputs`) + one shared model + one render helper. No
  enum grown; registry UNCHANGED at 172 (in-process). Retrieval unshifted (no
  template docstring changed).
- Offline suite baseline preserved at EXACTLY 9 by SET (`fetch_resolution_gate`
  x4 + `river_dye` x5). The river_dye 5 stay kind-identical: the composer trio's
  stale `_fake_publish` TypeError shifts arg-count 9 -> 10 (same failure line,
  same kind); the two reject tests unchanged (geocode / validation).
- Provenance now SURVIVES the bare-published-LayerURI path -- the one place the
  audit proved a caveat was dropped entirely.
- The Okada honesty (2c) is delivered server-side, deterministically, so the
  provenance is guaranteed regardless of worker version; no worker rebuild this
  wave (the stdout banner is now redundant, ledgered).

## Residuals (feed the gate wave -- two-mode INPUT_REQUIRED, audit 5a)

- The structured field labels a proceeding demo default; it does NOT yet GATE the
  sensitivity-dominant ungated inputs (OpenQuake Vs30, SWAN wave boundary, the
  rest of 5a). The gate wave consumes this field: an `INPUT_REQUIRED` recovery
  envelope offers `provide | proceed_with_defaults | cancel`, and on
  proceed_with_defaults the run stamps exactly these `synthetic_inputs` entries.
- SWAN + OpenQuake + the remaining engines are NOT populated this wave (no prose
  carrier existed to structure; they are gate-wave targets).
- The worker Okada stdout banner can be deleted in a later worker-touching wave
  (superseded by 2c server-side entry).
