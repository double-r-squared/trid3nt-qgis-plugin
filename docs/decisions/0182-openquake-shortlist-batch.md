# ADR 0182 - OpenQuake shortlist batch: disaggregation + event-based PSHA + Vs30 A/B fold

Date: 2026-08-07

Status: accepted

## Context

The first SHORTLIST GRIND batch worked the OpenQuake ready-now rows from
`docs/validation/ml-signoff-shortlist.md` (section 1, engine table): hazard
disaggregation, event-based PSHA, the Vs30 site-response row, the source-typology
row, and the liquefaction row. The existing OpenQuake surface: `openquake_psha`
(classical, with the ADR 0149 logic-tree / UHS / multi-PoE knobs and a real-fault
source path), `openquake_scenario_gmf` + `openquake_secondary_perils` (ADR 0164,
local-subprocess scenario calculator + `openquake.sep` liquefaction/landslide).
The engine is oq 3.25.1 with the GEM demos in-package; the 0149/0164 folding
precedent (knobs on existing tools where honest, new tools only for genuinely-new
calculators) governs.

Triage against the installed engine (both new decks proven on a local `oq` run
before any wiring, per local-first):

- **Disaggregation** (`calculation_mode = disaggregation`) is a distinct
  calculator: single-site, decomposes the hazard at a target PoE into
  magnitude / distance / epsilon contribution bins (the `Mag_Dist_Eps` export) -
  the "which earthquake dominates my hazard" answer the classical MAP cannot
  give. Warrants a new tool (precedent: scenario_gmf).
- **Event-based PSHA** (`calculation_mode = event_based`) is a distinct
  calculator: samples stochastic event sets (a synthetic catalogue), computes
  per-rupture GMFs, back-derives the hazard curve and extracts a hazard map.
  Warrants a new tool. The deck over a site GRID requires `minimum_intensity`
  (proven by a live failure without it).
- **Vs30 site response**: `openquake_psha` ALREADY exposes the site-response
  lever (`vs30` -> `reference_vs30`). The row asks for the rock-vs-soil A/B, which
  is an additive KNOB, not a new tool - fold.
- **Source typology**: `openquake_psha` ALREADY renders two source typologies
  (synthetic `areaSource` + real-fault `simpleFaultSource`); the two a hazard
  workbench needs ship. The exotic remainder (point/multipoint/complex/
  characteristic/nonparametric) is low near-term value - partially-subsumed + stop.
- **Liquefaction**: `openquake_secondary_perils` (ADR 0164) already maps scenario
  liquefaction probability via Zhu et al. 2015 - subsumed.

## Decision

Land TWO additive engine templates + ONE knob fold (registry 226 -> 228,
EXPECTED_TEMPLATES 68 -> 70). Both new tools run oq IN-PROCESS (the installed `oq`
CLI as a composer subprocess - NO container image, NO Batch, NO reuse of the
classical `run_solver`/worker `job_ini.py` path), matching the ADR 0164 scenario
lane. A shared self-contained helper `workflows/openquake/_local_oq.py` carries
the deck primitives (a synthetic G-R `areaSource` XML + trivial logic trees +
IML ladder + region string + the `run_oq_local` subprocess runner + a
single-site classical-point deck) so neither new module imports the worker
package (the agent-bundle boundary).

- `openquake_disaggregation` (`workflows/openquake/disaggregation/`): renders a
  disaggregation `job.ini` at the AOI centroid over the demo area source, runs
  oq locally, parses `Mag_Dist_Eps`, surfaces the disaggregation site as a point
  marker (`DisaggregationLayerURI`, layer_type=vector), and emits the M-R
  contribution chart. "Dominant" = the modal magnitude-distance CELL summed over
  epsilon (the standard controlling earthquake, consistent with the chart), not
  the modal single M-R-eps bin. The chart is a legended grouped-series line
  (contribution % vs distance, one line per magnitude bin) because the dock
  interpreter renders a rect heatmap only as a legend-less scatter (readability
  laws demand a legend; the shortlist explicitly allowed grouped series as the
  fallback).
- `openquake_event_based` (`workflows/openquake/event_based/`): renders an
  event-based `job.ini` over a coarse capped site grid, runs oq locally, feeds
  the hazard-map CSV through the shared classical `postprocess_openquake` for the
  COG + metrics (`EventBasedHazardLayerURI`, layer_type=raster), then runs a
  classical single-site deck at the centroid and overlays the two hazard curves -
  the convergence check. The verdict uses the MEDIAN relative PoE difference
  (robust; the max is dominated by the rare high-intensity tail the catalogue
  undersamples).
- `openquake_psha` `vs30_compare` knob: when set, after the primary map the
  composer runs two single-site classical hazard curves at the AOI centroid on
  the same demo source differing ONLY in reference Vs30 (the run's value vs the
  comparison soil) and emits ONE legended overlay with the amplitude ratio at the
  target PoE in the caption. Additive (default `None` = unchanged); best-effort
  (never fails the primary map). This is a reference-Vs30 A/B, NOT a per-site
  fetched-Vs30 `site_model.csv` build - the raster-Vs30 promotion (Wald-Allen
  slope Vs30 already in the 0164 machinery) is deferred.

Shared-seam maintenance: `postprocess_openquake.parse_hazard_map_csv` now skips
`custom_site_id` / `site_id` when picking the hazard-value column (the event-based
export leads with `custom_site_id`, which the old logic mis-picked as the value).

The tool_registry (226 -> 228), EXPECTED_TEMPLATES (68 -> 70), categories.py
(both new tools -> simulation_modeling), and the two co-located corpus.yaml files
are updated; both tools surface 8/8 corpus queries in the model-free
`retrieve_visible_tools(q, None, 8)` top-8.

## Dispositions (per row)

| Row | Disposition |
|---|---|
| `seismic_hazard_disaggregation_by_scenario` | LANDED - `openquake_disaggregation` |
| `stochastic_event_set_ground_motion_fields` | LANDED - `openquake_event_based` |
| `site_model_vs30_amplification_build` | FOLDED - `openquake_psha` `vs30_compare` knob |
| `classical_psha_source_typology_sweep` | PARTIALLY-SUBSUMED (area + simpleFault ship) + STOP (exotic typologies) |
| `scenario_liquefaction_probability_map` | SUBSUMED - `openquake_secondary_perils` (ADR 0164, Zhu 2015) |

## Consequence

Two genuinely-new OpenQuake calculators (disaggregation, event-based) are LLM-
callable and the classical map gains a site-response A/B - the OpenQuake surface
now spans classical map/curve/UHS/logic-tree, scenario GMF, secondary perils,
disaggregation, event-based catalogue, and site-response comparison. The
local-subprocess lane keeps them off Batch (offline-first) and leaves the
classical worker deck byte-identical. Both use a labelled synthetic demo area
source (narrated as such) rather than a real-fault source - the real-fault
physics stays in `openquake_psha`; promoting a fetched-Vs30 site model and a
real-fault disaggregation/event source are the honest follow-ups. Live evidence:
SF Bay AOI, oq 3.25.1 local subprocess - disaggregation (dominant M6.25 @ 10 km
eps+2, mean M6.11, iml@10%/50yr 0.368 g); event-based (168 ruptures / 1527
events, map max 0.425 g, median convergence 18%); Vs30 A/B (soft 260 vs rock
760 m/s amplification). Proofs under `docs/proof/templates/`.
