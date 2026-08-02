# MODFLOW pilot contract - engine-door refactor, slice 1

Status: FOR NATE REVIEW - CONTRACT LANE PIN. Nothing builds until the go.
Date: 2026-07-26. Branch: refactor/engine-doors.
Authority: docs/specs/engine-door-refactor.md (the CORRECTED 2026-07-26
terminology block is binding). This document pins the exact contract, gating,
door, template map, external references, and test migration for the MODFLOW
pilot so the build lanes have a frozen kickoff.

Binding terminology (restated, from the corrected block):
- DOOR = run_modflow, a read-only CONCIERGE. It (1) lists its engine's
  templates from the registry, (2) expands the turn's retrieval gate with those
  templates, (3) fidelity-briefs incl. mismatch redirection. It EXECUTES
  NOTHING. Nothing collapses into internal dispatch; the 14-into-1 mega-tool is
  rejected.
- TEMPLATES = individual REGISTERED TOOLS tagged engine=modflow, tier=template.
  They keep their own schema / envelope / telemetry / direct-call testability /
  bench grading. They are EXCLUDED from the default retrieval pool and surfaced
  only by the door's gate expansion. SELECT-THEN-CALL: the LLM calls the door,
  then calls the chosen modflow_* template directly. Registered names carry the
  engine (modflow_<question>).
- Renames REPLACE old registered names (no aliases). Fold only on functional
  sameness.

---

## 0. The registered MODFLOW family today (exact, from the live registry)

Queried via `trid3nt_server.main._import_tools_registry()` -> `TOOL_REGISTRY`
(205 registered tools total). The MODFLOW-engine family:

Collapse behind the door (the "~14 registered entries -> 1 door" set = 14):

| # | old registered name | source_class | disposition |
|---|---------------------|--------------|-------------|
| 1 | `run_modflow_job` | workflow_dispatch | FOLD -> `modflow_contaminant_plume` |
| 2 | `run_model_multi_species_scenario` | workflow_dispatch | FOLD -> `modflow_contaminant_plume` |
| 3 | `run_model_capture_zone_scenario` | workflow_dispatch | RENAME -> `modflow_capture_zone` |
| 4 | `run_model_wellhead_protection_scenario` | workflow_dispatch | RENAME -> `modflow_wellhead_protection` |
| 5 | `run_model_mine_dewatering_scenario` | workflow_dispatch | RENAME -> `modflow_mine_dewatering` |
| 6 | `run_model_saltwater_intrusion_scenario` | workflow_dispatch | RENAME -> `modflow_saltwater_intrusion` |
| 7 | `run_model_mar_scenario` | workflow_dispatch | RENAME -> `modflow_managed_recharge` |
| 8 | `run_model_asr_scenario` | workflow_dispatch | RENAME -> `modflow_asr` |
| 9 | `run_model_sustainable_yield_scenario` | workflow_dispatch | RENAME -> `modflow_sustainable_yield` |
| 10 | `run_model_wetland_hydroperiod_scenario` | workflow_dispatch | RENAME -> `modflow_wetland_hydroperiod` |
| 11 | `run_model_regional_water_budget_scenario` | workflow_dispatch | RENAME -> `modflow_regional_water_budget` |
| 12 | `run_model_river_seepage_scenario` | workflow_dispatch | FOLD -> `modflow_river_seepage` |
| 13 | `run_river_seepage_job` | workflow_dispatch | FOLD -> `modflow_river_seepage` |
| 14 | `run_model_contamination_affected_fields` | workflow_dispatch | CUT (re-home to playground) |

Net: 11 registered template tools + 1 door. Accounting: 2 folds
(contaminant_plume) + 2 folds (river_seepage) + 1 cut + 9 one-to-one renames =
14 old entries; the door is +1 NEW.

Stays registered, NOT part of the collapse:
- `run_model_groundwater_contamination_scenario` - the NEWS-driven "model the
  spill from this article" composer (news ingest -> claim extraction ->
  confirmation -> plume). It is a DISTINCT question (news parsing is a
  composition, not an engine template) and is cross-listed to `news_events`. It
  is NOT a MODFLOW template; it is a CONSUMER of the folded plume result and
  migrates per section 5.3.
- `set_modflow_parameters` (source_class=param_setter) - the derive-not-mutate
  deck setter. Stays tier=general; relocates to `tools/simulation/modflow/`
  beside the door (spec section 4).
- `analyze_affected_fields` (source_class=affected_fields) - the zonal
  field-scoring analysis tool that the CUT composer used for its second half.
  Stays registered tier=general for now; whether it too becomes a playground
  recipe is a SEPARATE decision (RISK-6). Only the COMPOSER is cut here.

Internal (unregistered) engine surfaces that the templates keep calling:
- `run_modflow_archetype_job` (run_modflow_archetype_tool.py) - the shared
  GWF/GWT/PRT archetype dispatcher every archetype template body calls.
- `run_modflow_multi_species_job` (run_modflow_multi_species_tool.py) - the
  N-species dispatcher; its `build_multi_species_staging` gets folded into
  `build_and_stage_modflow_deck` (section 5.2).
- `workflows/modflow/run_modflow.py` (build/stage/submit/local) +
  `postprocess_modflow.py` (metrics) - unchanged solver seam.

---

## (a) METADATA EXTENSION - `engine` + `tier` on `AtomicToolMetadata`

Contracts package: `contracts/src/trid3nt_contracts/tool_registry.py`
(`AtomicToolMetadata`, a `GraceModel` with `extra="forbid"`).

Add two OPTIONAL fields, both defaulting to the safe / no-impact value so all
~30 existing `AtomicToolMetadata(...)` call sites keep working untouched
(additive, same pattern as the Wave 1.5 / 4.10 field additions already in the
model):

```python
EngineTier = Literal["general", "door", "template"]

engine: str | None = Field(
    default=None,
    description=(
        "Owning engine slug for an engine-door family member (e.g. 'modflow', "
        "'sfincs'). None (default) for every non-engine tool - zero impact on "
        "existing registrations. The door lists / gate-expands over its "
        "engine's tier=template members filtered by this slug."
    ),
)
tier: EngineTier = Field(
    default="general",
    description=(
        "Retrieval tier. 'general' (default) - the ordinary per-turn retrieval "
        "pool. 'door' - a read-only engine concierge; ALSO retrievable in the "
        "per-turn pool (doors compete with general). 'template' - a registered "
        "engine template EXCLUDED from the default pool, surfaced only by its "
        "door's gate expansion (select-then-call). Excluding tier=template "
        "decouples registration from retrieval visibility."
    ),
)
```

Rules pinned:
- `tier` default `"general"` == today's behavior (in the pool). `engine`
  default `None`. Backward-compatible: no existing construction changes.
- No new cross-field validator. `engine` and `tier` are orthogonal to the
  cacheable/ttl_class consistency rule; the existing `_validate_cacheable_
  consistency` is untouched. (A soft convention - tier in {door, template}
  SHOULD carry a non-null engine - is enforced at registration/audit time in
  the server, NOT in the contract, to keep the contract a pure shape.)
- The door itself is tier="door", engine="modflow". Every MODFLOW template is
  tier="template", engine="modflow".

Contracts-package test updates (`contracts/tests/test_tool_registry.py`):
1. NEW `test_engine_tier_default_none_general`: a bare cacheable/uncacheable
   construction yields `engine is None` and `tier == "general"` (the
   zero-impact default). Assert the existing 4-TTL parametrized test still
   passes byte-unchanged (defaults present, not required).
2. NEW `test_tier_accepts_three_literals`: `general` / `door` / `template`
   construct; an unknown tier (e.g. `"engine"`) raises `ValidationError`.
3. NEW `test_engine_slug_roundtrips`: `engine="modflow"` round-trips through
   `model_dump` / re-construct; `engine=None` round-trips.
4. EXTEND the existing JSON idempotency test to a metadata carrying
   `engine="modflow", tier="template"` (serialize -> deserialize -> re-serialize
   stable), and confirm `extra="forbid"` still rejects an unknown key.
5. If any test asserts the EXACT `model_fields` set or a frozen
   `model_json_schema()` of `AtomicToolMetadata`, update its expected shape to
   include the two new fields (grep first: none found in contracts/tests today,
   but the server-side `test_tools_registry.py` / `test_tool_registry`-style
   assertions must be re-checked in the same sweep).

Register-time plumbing (server, NOT contracts): `register_tool`
(`server/src/trid3nt_server/tools/__init__.py:93`) already forwards the passed
`AtomicToolMetadata` verbatim onto the frozen `ToolEntry.metadata`
(`tools/__init__.py:81`). No signature change is required - a template's module
constructs its `AtomicToolMetadata(..., engine="modflow", tier="template")` and
passes it as today. (Optionally add `engine=` / `tier=` keyword pass-throughs to
`register_tool` mirroring the `supports_global_query` override at
`tools/__init__.py:159`, but the direct-in-metadata path is sufficient and
preferred - one source of truth.)

---

## (b) GATING - exclude tier=template from the pool; door expands the gate

Two independent seams. Both REUSE existing machinery; neither invents a parallel
gate.

### (b.1) EXCLUDE tier=template from the default retrieval pool

The single default-pool producer is the discover index build in
`server/src/trid3nt_server/tools/discovery/search_tools/search_tools.py` -
`_build_discover_index` iterates `TOOL_REGISTRY` at
`search_tools.py:595` (`for name in sorted(snapshot.keys())`) and appends every
name to `index.tool_names` at `search_tools.py:610`. `index.tool_names` is the
authoritative candidate list that BOTH retrieval faces rank over:
`retrieve_visible_tools` / `retrieve_ranked_tools`
(`tools/discovery/tool_retrieval.py:245` / `:183`, via
`_build_channel_rankings` and `_discover_topk`).

Pin: at `search_tools.py:595-614`, SKIP a registry entry whose
`entry.metadata.tier == "template"` when building `tool_names` (and its parallel
`descriptions` / `documents` / `corpus_tokens`). One guard, one place:

```python
for name in sorted(snapshot.keys()):
    entry = snapshot[name]
    if getattr(entry.metadata, "tier", "general") == "template":
        continue  # templates are surfaced only by their door's gate expansion
    ...
```

Consequences (all correct-by-construction):
- `retrieve_ranked_tools` / `retrieve_visible_tools` can never surface a
  template, so the openai top-k gate (`tool_gating.gate_tool_registry`) and the
  bedrock per-turn view both start template-free.
- tier="door" tools are NOT skipped -> doors stay in the pool and compete in
  per-turn retrieval (spec Rules section 3: "general tier + door tier compete").
- FAIL-OPEN paths still work: `_full_registry_floor`
  (`tool_retrieval.py:223`) unions the WHOLE `TOOL_REGISTRY` on a cold/faulted
  index. To keep templates out of the fail-open dump too, the fail-open floor
  must ALSO filter tier=template (single helper: filter `TOOL_REGISTRY` by
  `tier != "template"` in `_full_registry_floor`). Pin this as part of the same
  change so a cold index does not leak all 11 templates into the visible set.
- HOT_SET / AllowedToolSet: no template is in `HOT_SET_TOOLS`
  (`categories.py:837`) and none is a category member unless we add it - so
  `tools_for_category` / opened-category widening never surface a template
  either. The door being called is the ONLY path to a template (below).

Corpus co-location (spec Rules section 3): the composed corpus is walked from
`tools/**/corpus.yaml` at `search_tools.py:410-412`
(`tools_dir.rglob("corpus.yaml")`) plus the residual
`data/tool_query_corpus.yaml` (`:415`). Templates live under
`workflows/modflow/<template>/` (spec section 4), which that walk does NOT
reach today. Pin for v1: a template's co-located `corpus.yaml` is the TEMPLATE
tier and MUST NOT be merged into the main index corpus (it would add tokens for
a tool that is excluded from `tool_names` anyway - dead weight, and it dilutes
the vocabulary). Concretely: keep the tree walk rooted at `tools/` so template
corpus under `workflows/` is naturally out of the main index; the door reads
template cards directly from the registry (section c), not from the index. The
door's OWN `corpus.yaml` lives beside it under
`tools/simulation/modflow/run_modflow/corpus.yaml` and IS walked (door tier ->
in the main pool). The residual `data/tool_query_corpus.yaml` entries for the
migrated names are handled in section (e).

### (b.2) DOOR expansion - reuse the discovery-expands-gate seam

The existing seam that a discovery tool's result uses to widen the turn's
visible set lives in `server/src/trid3nt_server/server.py`:
- recognition: `_tool_search_tool_names()` (`server.py:1329`) - the set of
  registered names whose results expand the gate (today resolved off
  `search_tools`' `_SEARCH_TOOLS_METADATA` + a legacy alias).
- extraction: `_tool_names_from_search_result(result)` (`server.py:1353`) -
  reads `{"results": [{"tool_name": <name>}, ...]}` in rank order.
- expansion: the dispatch post-processing at `server.py:4504-4540` - when
  `call.name in _tool_search_tool_names()`, it unions up to
  `_DISCOVERY_EXPAND_CAP` NEW names into `_retrieval_registry` AND
  `state.allowed_tool_set.add_tools(...)`, then sets `_tool_decls_dirty = True`
  so the tool declarations rebuild for subsequent rounds.

Pin the door onto this exact seam with three minimal, additive changes:

1. Recognition. Generalize the expander predicate to include engine doors.
   Add a sibling `_gate_expander_tool_names()` (or widen the check at
   `server.py:4512`) that returns `_tool_search_tool_names()` UNION every
   registered name whose `metadata.tier == "door"`. A door is a gate-expander
   by construction. Resolve by registry lookup (never a hardcoded literal) so a
   new engine door is picked up automatically.
2. Extraction. Teach `_tool_names_from_search_result` (`server.py:1353`) to
   also read `templates[].tool_name` when `results` is absent (the door emits a
   `templates` list; section c). One `rows = result.get("results") or
   result.get("templates")` fallback - the rest of the extractor is unchanged.
3. Expansion cap handling. `_DISCOVERY_EXPAND_CAP = 8` (`server.py:1326`) was
   calibrated for OPEN-ENDED dataset discovery (a long ranked tail where the cap
   protects context). A door lists a CLOSED, CURATED set - its own engine's
   registered templates (11 for MODFLOW). Capping at 8 would hide 3 of 11 and
   silently break select-then-call. Pin: DOOR expansion is EXEMPT from the
   shared discovery cap. Implement as a separate `_DOOR_EXPAND_CAP` applied only
   on the door branch, set to a value >= the largest engine's template count
   (pin 24; MODFLOW needs 11, headroom for future engines). The open-ended
   `search_tools` branch keeps `_DISCOVERY_EXPAND_CAP = 8` unchanged. Rationale:
   over-inclusion of a curated 11-item set is cheap and correct; the cap exists
   to bound an unbounded ranked tail, which a door does not have. (Counter kept
   for NATE: if a single shared cap is preferred, raise `_DISCOVERY_EXPAND_CAP`
   to >= 24 instead - but that also loosens open-ended discovery, so the
   separate door cap is the pinned choice.)

Effect: LLM calls `run_modflow` -> the door returns its template listing +
`templates[].tool_name` -> the seam unions all 11 `modflow_*` templates into
`_retrieval_registry` + the Case `AllowedToolSet` -> the model can now call the
chosen template directly on the next round. `validate_function_call`
(`categories.py:1146`) already auto-widens for any registry-valid name, so even
a template called before the expansion round lands is accepted (belt-and-
suspenders) - but the door path is the intended, corpus-routed entry.

---

## (c) DOOR v1 - `run_modflow` concierge (READ-ONLY)

Home: `server/src/trid3nt_server/tools/simulation/modflow/run_modflow/`
(`run_modflow.py` + co-located `corpus.yaml`), per spec section 4
(`tools/simulation/modflow/` holds door + setter). The door GROWS from the
knowledge in `run_modflow_archetype_tool.py` (which already enumerates the
archetype family) but is a NEW read-only tool - it does NOT wrap
`run_modflow_archetype_job` and executes no solve.

Registration metadata:
```python
AtomicToolMetadata(
    name="run_modflow",
    ttl_class="live-no-cache",
    source_class="door",     # new source_class label for engine doors
    cacheable=False,         # the gate-expansion side effect runs every call
    engine="modflow",
    tier="door",
    read_only_hint=True,     # executes nothing; no external write
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=True,    # same registry -> same listing
)
```
(`cacheable=False` + `live-no-cache` satisfies the existing cross-field
validator; a door must never be served from cache because it also drives the
per-turn gate expansion.)

Responsibilities (v1 only; v1.5 ask-hint ranking + readiness checks are
BACKLOGGED per the spec):

1. TEMPLATE LISTING - registry query. The door enumerates
   `TOOL_REGISTRY` for entries with `metadata.engine == "modflow"` and
   `metadata.tier == "template"` (deterministic, sorted). For each it emits a
   card: `tool_name`, one-line `question`, `required_inputs`, `knobs`.

   Per-template declaration mechanism (how each card's fields are sourced -
   NO new registry plumbing, honest-by-construction):
   - `question` = the template's docstring first line (`_first_sentence`,
     already used by `_list_tools_in_category_impl`, `categories.py:1238`).
   - `required_inputs` = the template callable's signature params WITHOUT a
     default (`inspect.signature`), minus the absorbed `**_extra_ignored` /
     `compute_class`. These are the real required args - never fabricated.
   - `knobs` = the signature params WITH defaults (the optional overrides), one
     short line.
   - OPTIONAL override: a template module MAY export a module-level
     `TEMPLATE_CARD` (a small frozen dataclass: `question: str`,
     `required_inputs: list[str]`, `knobs: str`) for a curated one-liner; the
     door prefers it when present and falls back to the signature/docstring
     derivation otherwise. This keeps the door zero-maintenance as templates are
     added (spec: "adding a template = adding a tool folder with the tags - the
     door discovers it; door code never changes").

2. GATE EXPANSION - via the section (b.2) seam. The door's return carries the
   `templates[].tool_name` machine list the server extractor reads to widen the
   turn. The door does not touch `AllowedToolSet` itself (that is the server's
   job at `server.py:4529`); it only RETURNS the names.

3. FIDELITY BRIEF + MISMATCH REDIRECTION. A short `fidelity_brief` string
   (MODFLOW 6 / MF6-GWT; demo aquifer defaults K/porosity are narrated, not
   site-specific; conservative-tracer unless `advanced_physics`; PRT / saltwater
   are LOCAL-only planning-grade), plus a `mismatch_redirect` map pointing
   off-engine asks to the right door: surface-water / inundation flooding ->
   the SFINCS door (run_sfincs, its slice), urban storm-sewer flooding ->
   run_swmm, coastal spectral waves -> run_swan. The redirect is prose the LLM
   narrates; it names doors, not templates.

Response envelope (READ-ONLY; a plain JSON dict, NOT a `LayerURI` - the door
loads nothing):

```json
{
  "engine": "modflow",
  "kind": "engine_door",
  "templates": [
    {
      "tool_name": "modflow_contaminant_plume",
      "question": "how far a contaminant spill spreads in an aquifer + peak concentration",
      "required_inputs": ["spill_location_latlon", "contaminant", "species"],
      "knobs": "duration_days, aquifer_k_ms, porosity; species=[{name, release_rate_kg_s, sorption_kd, decay_per_day}]"
    }
  ],
  "fidelity_brief": "MODFLOW 6 / MF6-GWT groundwater engine. Aquifer K/porosity default to narrated demo values unless supplied. Conservative-tracer transport unless advanced_physics is set. Capture-zone / wellhead / saltwater runs are local, planning-grade envelopes, not calibrated regulatory delineations.",
  "mismatch_redirect": {
    "surface-water / inundation flooding": "run_sfincs (flood door)",
    "urban storm-sewer / pipe-network flooding": "run_swmm",
    "coastal spectral wave field": "run_swan"
  },
  "next_action": "SELECT-THEN-CALL: call the chosen modflow_* template directly with its required inputs."
}
```

`templates[].tool_name` is the machine list the gate expander reads (section
b.2 step 2). The door NEVER returns a run/layer; it returns this concierge
envelope only. Determinism (Invariant 1): every field is derived from the live
registry / signatures / a static brief - no free generation, no fabricated
template.

Door `corpus.yaml` (co-located): hazard-common groundwater phrasings route to
the DOOR (per the dropped-alias decision, spec section HOT-PATH ALIAS): "model a
groundwater contamination plume", "where does this spill go in the aquifer",
"pump test drawdown", "capture zone for this well", "saltwater intrusion",
"managed aquifer recharge", "how much will pumping deplete the river", etc. -
all rank the door, whose expansion then surfaces the specific template.

---

## (d) TEMPLATE MAP - old registered name -> new template-tool name

Naming: `modflow_<question>`, folder-per-template
`workflows/modflow/<template>/<template>.py + corpus.yaml`, file named after
its folder (spec section 4). Each template body is the RENAMED existing composer
(unchanged dispatch to `run_modflow_archetype_job` / the plume path), retagged
`engine="modflow", tier="template"`.

| new template | old registered name(s) | postprocess / carrier (unchanged) | note |
|--------------|------------------------|-----------------------------------|------|
| `modflow_contaminant_plume` | `run_modflow_job` + `run_model_multi_species_scenario` | `postprocess_modflow` / `postprocess_multi_species` -> `plumes[]` | FOLD (section 5) |
| `modflow_capture_zone` | `run_model_capture_zone_scenario` | `postprocess_capture_zone` -> `CaptureZoneLayerURI` | PRT, local-only |
| `modflow_wellhead_protection` | `run_model_wellhead_protection_scenario` | `postprocess_capture_zone` -> `CaptureZoneLayerURI` | PRT, local-only; distinct question (EPA tiers) |
| `modflow_mine_dewatering` | `run_model_mine_dewatering_scenario` | `postprocess_dewatering` -> `DewaterLayerURI` | |
| `modflow_saltwater_intrusion` | `run_model_saltwater_intrusion_scenario` | `postprocess_saltwater_intrusion` -> `SaltwaterWedgeLayerURI` | BUY, local-only |
| `modflow_managed_recharge` | `run_model_mar_scenario` | `postprocess_mounding` -> `MoundingLayerURI` | MAR archetype |
| `modflow_asr` | `run_model_asr_scenario` | `postprocess_asr` -> `ASRLayerURI` | aquifer storage & recovery |
| `modflow_sustainable_yield` | `run_model_sustainable_yield_scenario` | `postprocess_drawdown` -> `DrawdownLayerURI` | |
| `modflow_wetland_hydroperiod` | `run_model_wetland_hydroperiod_scenario` | `postprocess_wetland_hydroperiod` -> `HydroperiodLayerURI` | |
| `modflow_regional_water_budget` | `run_model_regional_water_budget_scenario` | `postprocess_budget_partition` -> `BudgetPartitionLayerURI` | |
| `modflow_river_seepage` | `run_model_river_seepage_scenario` + `run_river_seepage_job` | `postprocess_river_seepage` -> `SeepageLayerURI` | FOLD (engine tool + composer collapse into ONE template) |

CUT: `run_model_contamination_affected_fields` (section 5.4).

Distinct-question note (fold criterion): `modflow_capture_zone` and
`modflow_wellhead_protection` share `postprocess_capture_zone` /
`CaptureZoneLayerURI` and today share ONE composer file
(`model_capture_zone_scenario.py` registers BOTH), but they answer DIFFERENT
questions (general zone-of-contribution vs EPA fixed-travel-time WHPA tiers) ->
they stay TWO templates in TWO folders per the fold criterion (never fold on
shared scaffolding). The one composer file splits into
`workflows/modflow/capture_zone/` and `workflows/modflow/wellhead_protection/`.

### 5. contaminant_plume FOLD - the load-bearing detail

#### 5.1 The template surface (`modflow_contaminant_plume`)

ONE registered template answering "model a contaminant plume in groundwater",
single OR multi species, with:

```python
species: list[dict]  # [{"name": str, "release_rate_kg_s": float, ...}], min 1
```

- Single-species is `species` of length 1. The single-contaminant convenience
  fields (`contaminant`, `release_rate_kg_s`) are ACCEPTED and normalized into
  `species=[{name: contaminant, release_rate_kg_s: rate}]` so a caller with one
  contaminant does not have to build a list (keeps the minimal-parameter
  surface, Invariant 10). Optional per-species `sorption_kd` / `decay_per_day` /
  `parent` ride through to `SpeciesSpec`.
- Contract: `SpeciesSpec` (`modflow_contracts.py:104`) and
  `MODFLOWRunArgs.species` (`:512`) already exist and are additive; no contract
  field is added for the fold. The template assembles
  `MODFLOWRunArgs(..., archetype="multi_species", species=[...])` for every
  call (single = one-element list).

#### 5.2 Fix the build seam - `build_and_stage_modflow_deck` forwards species

Today `build_and_stage_modflow_deck` (`run_modflow.py:613`) does NOT forward
`species`; the multi-species tool works around it with a private
`build_multi_species_staging` (`run_modflow_multi_species_tool.py:121`) that
calls `build_modflow_deck(archetype="multi_species", species=...)` itself. Pin:
thread `run_args.species` (normalized to plain dicts via the adapter's
`_normalize_species`, mirroring `_species_payload`,
`run_modflow_multi_species_tool.py:94`) into the `build_modflow_deck(...)` call
at `run_modflow.py:773-785`, and set `gwt_present=True` / the `gwt_*.ucn` output
globs when species is present. Then `build_multi_species_staging` DELETES -
ONE build/stage seam handles single and multi. (Also thread species through the
offload path `_run_args_to_deck_kwargs`, `run_modflow.py:962`, so the
build-offload lane stays identical.)

#### 5.3 Envelope unification - always `plumes[]`

- `postprocess_modflow` (`postprocess_modflow.py:1180`) returns a single
  `PlumeLayerURI`; `postprocess_multi_species` (`:1398`) returns
  `MultiSpeciesPlumeResult(plumes=[PlumeLayerURI, ...])` (`:1505`,
  `modflow_contracts.py:740`, min_length=1).
- Pin: `modflow_contaminant_plume` ALWAYS returns `MultiSpeciesPlumeResult`
  (`plumes[]`, length 1 for a single species). The single-species postprocess
  path becomes: build one GWT (species length 1) -> `postprocess_multi_species`
  -> `plumes[]` of length 1. `postprocess_modflow` (single) stays available as
  the internal per-UCN reader `postprocess_multi_species` already delegates to,
  but the TEMPLATE's return is uniformly `plumes[]`.
- The emitter loads EACH `plumes[i]` (each is a `PlumeLayerURI` -> the
  `add_loaded_layer` gate fires per layer). The multi-species tool already loops
  the emitter over `result.plumes`; the single path now does the same over a
  1-list.
- Honesty floor (Invariant 9): unchanged - the run errors typed
  (`MODFLOW_MULTISPECIES_EMPTY_RESULT` / equivalent) when EVERY species plume is
  at/below `PLUME_DETECTION_FLOOR_MGL`; a single empty plume errors exactly as
  `run_modflow_job` does today.

#### 5.4 FULL consumer migration list (single PlumeLayerURI -> plumes[])

Every caller that today receives a single `PlumeLayerURI` from `run_modflow_job`
must accept the `plumes[]` (list-of-1) envelope. Load-bearing consumers:

1. `run_model_groundwater_contamination_scenario`
   (`workflows/modflow/model_groundwater_contamination_scenario.py`) - the NEWS
   composer. Migrate: `_registry_fn("run_modflow_job")` ->
   `_registry_fn("modflow_contaminant_plume")`; assemble
   `species=[{name: contaminant, release_rate_kg_s: rate}]`; read `plumes[0]`
   into `Case2Result.plume_layer` (which stays a single `PlumeLayerURI` on the
   wire - the news composer surfaces ONE plume). Also update the confirmation
   envelope `tool_name="run_modflow_job"` (`:711`) -> `"modflow_contaminant_plume"`.
2. `analyze_affected_fields`
   (`tools/processing/analyze_affected_fields/analyze_affected_fields.py`) -
   references `run_modflow_job` in docstring / cross-tool guidance and consumes
   a plume layer input. Migrate name references to `modflow_contaminant_plume`;
   its layer input stays a `PlumeLayerURI` (a single plume the user points at),
   so no envelope change, only the name.
3. `scenario_reuse.py` `_SCENARIO_TOOL_MAP` (`:84`): key
   `"run_modflow_job": "plume"` -> `"modflow_contaminant_plume": "plume"`; keep
   `"run_model_groundwater_contamination_scenario": "plume"` (`:85`). Verify
   `_plume_signature` (`:383`) still keys on spill point + contaminant + rate +
   duration (it reads params, not the result shape - unaffected by plumes[]).
4. `server.py`:
   - `SOLVER_CONFIRM_TOOLS` (`:1099`): DROP `run_model_contamination_affected_
     fields` (cut); the migrated template names that submit a solver
     (`modflow_contaminant_plume` and the archetype templates) are consequences
     - re-key them into `SOLVER_CONFIRM_TOOLS` if they are to be confirm-gated
     (today the archetype composers were not in the set; preserve current gating
     semantics - a rename must not silently add/remove a gate). Keep
     `run_model_groundwater_contamination_scenario` (`:1100`).
   - `_is_terminal_composer` (`:1378`) requires `tool_name.startswith("run_")`
     (`:1394`) AND `source_class == "workflow_dispatch"`. The new
     `modflow_*` template names do NOT start with `run_` -> they would NOT latch
     as terminal deliverables (crisp-end wrap-up, post-deliverable idle reset).
     REGRESSION - the predicate must be widened to also recognize
     `tier == "template"` (or `metadata.engine is not None and
     source_class == "workflow_dispatch"`). See RISK-1.
   - `_terminal_composer` / catalog prose referencing `run_modflow_job` /
     `run_model_groundwater_contamination_scenario` (`:7470`) - re-key to the
     template + door.
5. `adapter.py` system-prompt routing guidance (`:534-590`): rewrite the
   hard-coded "Parameterized spill -> run_modflow_job; spill news article ->
   run_model_groundwater_contamination_scenario" lines to the door model:
   groundwater questions -> `run_modflow` (door) -> select
   `modflow_contaminant_plume`; a spill NEWS article still ->
   `run_model_groundwater_contamination_scenario`. This is load-bearing prompt
   text (the LLM's routing).
6. `tool_arg_normalizer.py` - the `spill_location_latlon` string->tuple coercion
   docstring cites `run_modflow_job` (`:580`); the actual keyed maps (`:114`)
   are flood-scenario keyed, not modflow. Migrate the docstring reference; add a
   `modflow_contaminant_plume` entry only if a name-keyed normalization is
   needed (the template's own `coerce_latlon` already handles it, as
   `run_modflow_job` does today - prefer keeping it in-tool).
7. `tool_catalog_http.py` - the HTTP tool-catalog exposure lists names; it reads
   the live registry, so renames flow automatically, but any hard-coded example
   referencing `run_modflow_job` must be updated.
8. `compute_model_residuals`, `export_case_to_qgis` - docstring / example
   references to `run_modflow_job` (cross-tool prose); rename references, no
   behavior change.

CUT - `run_model_contamination_affected_fields`
(`workflows/modflow/model_contamination_affected_fields/`): delete the composer
+ its `@register_tool`. Its plume half IS `modflow_contaminant_plume`; its zonal
field-scoring half re-homes to a PLAYGROUND RECIPE (model composes
`modflow_contaminant_plume` -> FTW field boundaries -> `analyze_affected_fields`
/ `compute_zonal_statistics` in `code_exec_request`), per the
analysis-is-playground norm. Recipe doc location:
`trid3nt-local/docs/playbooks/modflow-affected-fields-recipe.md` (NEW), with an
ADR-lite note in `trid3nt-local/docs/decisions/` recording the cut + pointer.
Remove `run_model_contamination_affected_fields` from `SOLVER_CONFIRM_TOOLS`
(`server.py:1105`), `categories.py` PRIMARY/SECONDARY, `tools/__init__.py`
import, `workflows/__init__.py`, and `data/tool_query_corpus.yaml`.

---

## (e) Old-name references OUTSIDE tests + handling

Load-bearing (must migrate with the rename; renames replace names, no aliases).
Historical artifacts under `docs/reports/*`, `docs/site/*`,
`experiments/bench/*`, `experiments/embedding-*` are FROZEN run outputs - do NOT
edit (they are dated evidence; a rename does not rewrite history).

| reference site | file(s) | handling |
|----------------|---------|----------|
| Registration / import | `tools/__init__.py` (`:408,411,417,454-478`), `workflows/__init__.py`, `workflows/modflow/__init__.py` | Re-point imports to folder-per-template modules; add the door import; fold the multi-species + run_modflow_tool imports; drop the cut composer. |
| Category membership | `categories.py` PRIMARY_CATEGORY (`:266,313-325` etc.) + SECONDARY_CATEGORIES (`:670`) | Replace old keys with `run_modflow` (door) + `modflow_*` (templates); drop the cut name; keep `run_model_groundwater_contamination_scenario`. Door -> hazard_modeling; templates need NO category membership (excluded from the pool; door-surfaced) - do NOT add them to a category or opened-category widening re-leaks them. |
| Retrieval corpus | `server/src/trid3nt_server/data/tool_query_corpus.yaml` | Remove the migrated names' residual entries; move the door phrasings into the door's co-located `run_modflow/corpus.yaml`; template phrasings move into each `workflows/modflow/<template>/corpus.yaml` (TEMPLATE tier, NOT merged into the main index). The 2 currently-failing canonical-ranking tests re-baseline to expect the DOOR (spec HARD PREREQ: NATE's `tool_query_corpus.yaml` WIP lands first). |
| Persistence / reuse | `scenario_reuse.py` (`:84-85`) | `_SCENARIO_TOOL_MAP` key rename `run_modflow_job` -> `modflow_contaminant_plume`; keep groundwater key; signature matcher reads params (unaffected). |
| Server routing / gating | `server.py` (`:1099 SOLVER_CONFIRM_TOOLS`, `:1105` cut name, `:1155 FETCH_CONFIRM_TOOLS`, `:1378-1395 _is_terminal_composer`, `:7470`) | Re-key the name sets; widen `_is_terminal_composer` for tier=template (RISK-1); drop the cut name. |
| System prompt | `adapter.py` (`:534-590`) | Rewrite routing guidance to door + select-then-call (section 5.4 item 5). |
| Arg normalizer | `tool_arg_normalizer.py` (`:580`) | Migrate docstring reference; keep in-tool latlon coercion. |
| HTTP catalog | `tool_catalog_http.py` | Registry-driven (auto), fix hard-coded examples. |
| Cross-tool prose | `analyze_affected_fields.py`, `compute_model_residuals.py`, `export_case_to_qgis.py`, sibling engine tool docstrings (`run_geoclaw_tool`, `run_landlab_tool`, `run_openquake_tool`, `run_swmm_tool`, `run_telemac_tool`) that cite `run_modflow_job` as an example | Rename the referenced name; no behavior change. |
| Docs (living) | `docs/site/tool-support.md`, `docs/validation/engine-coverage-inventory.md`, `docs/validation/build-report.md` | Update the living inventories (not the frozen dated reports). |
| QGIS plugin | `qgis-plugin/trid3nt/**` | NO registered-tool-name coupling (only a `modflow_*` SOLVER-ID prefix comment at `ui/dock.py:171` and a plume-render note at `render/layers.py:437`). No change required; verify the solver-id prefix note still reads true (solver id stays `modflow`, unaffected by tool renames). |

---

## (f) Test migration map + acceptance runs

### Test migration (server/tests + contracts/tests)

| test file | change |
|-----------|--------|
| `contracts/tests/test_tool_registry.py` | ADD the 4 metadata-extension tests (section a); extend JSON idempotency for engine/tier. |
| `server/tests/test_tools_registry.py`, `test_tool_retrieval.py` | Assert `run_modflow` (door) is registered tier=door and RETRIEVABLE; assert every `modflow_*` template is registered tier=template and ABSENT from `retrieve_visible_tools` / `retrieve_ranked_tools` / index `tool_names`; assert a door dispatch expands the gate with all 11 templates (reuse the discovery-expand test pattern). Re-baseline the 2 canonical-ranking tests to expect the door. |
| `test_run_modflow.py`, `test_modflow_local_backend.py` | Re-key `run_modflow_job` -> `modflow_contaminant_plume`; assert the `plumes[]` (len 1) envelope for a single-species run; keep the mf6 local solve (allowed). |
| `test_run_modflow_multi_species_tool.py`, `test_model_multi_species_scenario.py` | Fold into the `modflow_contaminant_plume` template tests (single + multi); assert `build_and_stage_modflow_deck` now forwards species (the deleted `build_multi_species_staging` path). |
| `test_model_groundwater_contamination_scenario.py` | Migrate the dispatched name -> `modflow_contaminant_plume`; assert `Case2Result.plume_layer` == `plumes[0]`; confirmation envelope `tool_name` updated. |
| `test_model_capture_zone_scenario.py` | Split assertions across `modflow_capture_zone` + `modflow_wellhead_protection` (two templates, two folders). |
| `test_modflow_archetypes.py`, `test_modflow_wave2_archetypes.py`, `test_saltwater_intrusion_metrics.py`, `test_capture_zone_postprocess.py`, `test_river_seepage.py`, `test_engine_chart_emission.py` | Re-key the `run_model_*_scenario` / `run_river_seepage_job` names -> the `modflow_*` template names; postprocess/carrier assertions unchanged. |
| `test_model_contamination_affected_fields.py` | DELETE (composer cut). Move any still-valid zonal assertions into a playground-recipe test if desired (out of scope). |
| `test_scenario_reuse_job0326.py`, `test_scenario_reuse_dispatch_job0326.py` | Re-key `run_modflow_job` in the reuse map + dispatch. |
| `test_dispatch_guards_stage3.py`, `test_duplicate_flood_layer_fix.py`, `test_pipeline_emitter.py`, `test_system_prompt.py`, `test_lessons.py`, `test_sfincs_solve_domain_aoi_guard.py` | Re-key incidental `run_modflow_job` / cut-name references; `test_system_prompt.py` re-baselines to the door-model prompt (adapter.py). |
| `test_modflow_step3_quantities.py`, `test_stream_depletion.py`, `test_land_subsidence.py` | Re-key names; note stream_depletion / land_subsidence archetypes have NO registered composer today (RISK-5) - they ride `run_modflow_archetype_job` internally, unaffected by the registered-name migration. |

Committed tests stay offline (stub_server.py / mocked publish); mf6 solves
allowed (local `TRID3NT_MODFLOW_LOCAL=1`).

### Acceptance runs (per spec section Acceptance)

1. Registry membership: `run_modflow` registered tier=door; 11 `modflow_*`
   templates registered tier=template; 14 old names GONE; cut name GONE.
2. Retrieval: canonical-query suite green (door-baselined); no template in any
   default-pool ranking.
3. Offline suites green (envelope migration covered).
4. LIVE template runs (status ok + honest envelopes + readable diagnostics), at
   minimum: `modflow_contaminant_plume` single (`plumes[]` len 1) + multi
   (`plumes[]` len N), and `modflow_capture_zone`. Drive via a direct-call
   driver on the offline stub first, then a live WS turn (door -> select ->
   template).
5. Flood canary (standing rule - registry + corpus seams touched): direct-call
   flood run (status ok + depth COG + envelope) + WS turn smoke + NATE visual in
   QGIS.
6. NATE visual pass = rendering acceptance.

---

## Risks / open decisions (for NATE)

- RISK-1 (regression, CONFIRMED by code): `_is_terminal_composer`
  (`server.py:1394`) gates on `tool_name.startswith("run_")`. The new
  `modflow_*` template names break that prefix assumption, so a completed
  template would NOT latch as a terminal deliverable (crisp-end wrap-up +
  post-deliverable idle reset never fire) -> the turn spins to the loop cap.
  MUST widen the predicate to `tier == "template"` (or engine-tagged
  workflow_dispatch) in the same landing.
- RISK-2 (gating): `_DISCOVERY_EXPAND_CAP = 8` < 11 MODFLOW templates. Pinned
  fix = a separate uncapped/`>=24` door expand cap (section b.2 step 3); if
  NATE prefers one shared cap, raise `_DISCOVERY_EXPAND_CAP` instead (looser
  open-ended discovery - not the pinned choice).
- RISK-3 (fail-open leak): the FAIL-OPEN floor `_full_registry_floor`
  (`tool_retrieval.py:223`) dumps the WHOLE registry on a cold/faulted index; it
  must also filter tier=template or a cold index re-leaks all 11 templates into
  the visible set. Pinned into the section (b.1) change.
- RISK-4 (news composer scope): `run_model_groundwater_contamination_scenario`
  stays a registered news composer (out of the 14). Confirm this is intended vs
  also folding its news-ingest into a door-adjacent template (the pinned choice
  keeps it - news parsing is a composition, not an engine template).
- RISK-5 (unregistered archetypes): `stream_depletion` + `land_subsidence`
  exist as `MODFLOWRunArgs.archetype` values + postprocess + archetype-tool
  branches but have NO registered composer today. They are NOT templates in this
  slice; adding them = adding two template folders later (door discovers them
  free). Flag so the door's listing does not imply they exist.
- RISK-6 (analysis tool scope): `analyze_affected_fields` stays a registered
  tier=general tool. Whether it too should become a playground recipe (per
  analysis-is-playground) is deferred; only the COMPOSER
  `run_model_contamination_affected_fields` is cut here.
- RISK-7 (HARD PREREQ, from the spec): NATE's `tool_query_corpus.yaml` WIP must
  land BEFORE the corpus explosion so the 2 failing canonical-ranking tests
  re-baseline cleanly against the door.
- RISK-8 (confirm-gate parity): re-keying `SOLVER_CONFIRM_TOOLS` must PRESERVE
  today's gating (the archetype composers were not confirm-gated; the rename
  must not silently add or drop a solver-confirm gate).
