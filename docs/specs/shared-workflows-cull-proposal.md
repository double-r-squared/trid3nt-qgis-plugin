# Shared-Workflows Cull Proposal (FOR NATE REVIEW)

Scope: five `agent/workflows/shared/model_*` composers suspected of being reference-scenario
wrappers. Read-only audit (readers + cross-check + spot-verify). Nothing deleted here.
CULL STANDARD: a tool dies ONLY after a live replication of its process using
existing/remaining tools; a MISSING primitive => verdict is extract-first, never
cull-first. Function is never lost; only form changes.

Each folder = one `model_*.py` + an empty `__init__.py` (0 lines).

## Verdicts

| Tool | LOC | Verdict | Blocker |
|---|---|---|---|
| model_conservation_priority | 507 | CULL-DIRECT | none |
| model_news_event_ingest | 801 | CULL-DIRECT | none |
| model_goes_fire_animation | 832 | GENERALIZE-THEN-CULL | P1 (fetch_slider_timestamps unregistered) |
| model_satellite_fire_animation | 1337 | GENERALIZE-THEN-CULL | P1 (frame-count peek) |
| model_glm_lightning_animation | 906 | EXTRACT-FIRST (blocked / KEEP for now) | P2 (no historical single-band ABI) |

Rationale, tool by tool:

- **conservation_priority** — CLEANEST. Pure fan-out over six already-registered
  fetchers (geocode_location, fetch_naip, compute_ndvi, fetch_mobi,
  fetch_gbif_occurrences loop, fetch_iucn_red_list_range loop). Zero gridding /
  product math; only try/except + honesty-floor status + summary string. No missing
  primitive.
- **news_event_ingest** — textbook news-step-as-a-tool, the exact anti-pattern the
  satellite HARD PATTERN warns against. Five registered primitives (web_fetch,
  fetch_nws_event, fetch_storm_events_db, aggregate_claims_across_sources,
  geocode_location). The review-gate stop-short is by-design; the conversational
  playground loop (agent narrates derived params, user says go) reproduces it -- it
  is NOT tool-level plumbing.
- **goes_fire_animation** — thin GOES-only auto-snap sibling of satellite. Pure
  orchestration EXCEPT the availability read (`_read_slider_timestamps`) does a
  direct private import of the UNREGISTERED `fetch_slider_timestamps`
  (_satellite_slider.py:242). A playground cell has no sanctioned call. Extract P1
  first, then cull.
- **satellite_fire_animation** — SUPERSET of goes (adds WFIGS incident lookup,
  FIRMS-hotspot AOI localization, a review gate with a pre-fetch frame-count peek).
  The peek uses the same unregistered `fetch_slider_timestamps`. Extract P1, demote
  the rest to a playground recipe, then cull.
- **glm_lightning_animation** — WEAKEST candidate; genuine missing primitive
  CONFIRMED. The visible-base half needs a historical single-band ABI grayscale scan
  (C02/C13) at an arbitrary timestamp on a caller grid. No registered tool provides
  it: fetch_goes_satellite pins `valid_time = now()` (code L804, docstring L751);
  fetch_goes_archive_animation exposes only composite bands. Until P2 exists, culling
  loses capability -> KEEP.

## Generalization design (the three animation tools share ONE denominator)

All three collapse to one shape, with no shared module today (each reimplements it):

    AOI + time window
      -> time-binned imagery fetcher frames (GOES / VIIRS / GLM)
      -> publish_layer per frame (TiTiler)
      -> emit via add_loaded_layer with a shared NAME-TOKEN
      -> plugin render/temporal.py auto-groups tokens -> SequenceScrubber animates

Proposed replacement: NOT a new mega-tool. THE general capability is a **playground
frame-animation pattern** documented once, driven by pure-spec prompts, standing on a
small set of registered primitives -- exactly the satellite HARD PATTERN breed. The
codebase already proves the breed coexists: goes/glm registration comments say "NO
confirm gate" / "DIRECT AOI+window" / "NO news step". To make the pattern fully
playground-expressible we need:

- **P1 register `fetch_slider_timestamps`** (availability + cadence index). Unblocks
  goes + satellite frame-count peek and auto-snap. It already exists as a function;
  add `@register_tool` + AtomicToolMetadata + corpus.
- **P2 register a historical single-band ABI primitive** (`fetch_goes_abi_band` with
  a `valid_time`, or a valid_time path on fetch_goes_satellite). Unblocks glm's
  visible base and generalizes archive access. This is the one real build.
- **P3 document the scrubber name-token contract** (render/temporal.py) as a public
  playground contract so agent-emitted frames auto-group without a web change.
- **P4 (optional) `densest_hotspot_bbox`** spatial primitive for data-driven AOI.

With P1 + P2 + P3, all three composers become one recipe an agent writes in
code_exec; every ENDPOINT (GOES blend/single, VIIRS, GLM, FIRMS overlay, perimeters,
publish, emit) is retained via its independently-registered fetcher.

## Primitives to extract (with target locations)

- **P1 fetch_slider_timestamps** -- EXISTS, unregistered.
  `agent/tools/fetchers/imagery/_satellite_slider.py:242`. Add register_tool +
  metadata; import in `agent/tools/__init__.py`; add corpus phrasings. Small.
- **P2 historical single-band ABI** -- NEW.
  `agent/tools/fetchers/imagery/fetch_goes_satellite/fetch_goes_satellite.py` (relax
  the L804 now-pin behind a valid_time param) OR a new sibling; reuse
  fetch_goes_archive_animation's `_grid_for_bbox` / `_warp_band_to_physical` /
  `_list_archive_keys_in_window`. Real work; glm blocker.
- **P4 densest_hotspot_bbox** -- OPTIONAL. Currently `_densest_hotspot_bbox`,
  `model_satellite_fire_animation.py:491-545` (~55 LOC). Extract as a spatial
  primitive OR keep as a documented playground snippet (see disagreement below).

## Replication gates (must pass LIVE before any deletion; pure-spec, no bbox)

- **conservation (ready now):** "Build a conservation-priority view for <named
  place>: aerial base, current vegetation greenness, biodiversity importance, and
  occurrence points for <species>." PASS = playground fires geocode_location ->
  fetch_naip + compute_ndvi + fetch_mobi + fetch_gbif_occurrences(loop) +
  fetch_iucn_red_list_range(loop), publishes the same layer set. Grade = fired-tool
  set == acceptable set.
- **news (ready now):** "Ingest these sources <url / NWS area / storm-events
  year+state>, extract the event location/date/scale, geocode it for review." PASS =
  web_fetch/fetch_nws_event/fetch_storm_events_db -> aggregate_claims_across_sources
  -> geocode_location, same derived-param + provenance envelope.
- **goes (after P1):** "Animate GOES <product> over <AOI> for the last N hours,
  snapped to available frames." PASS = registered fetch_slider_timestamps -> inline
  snap -> fetch_goes_animation/blend -> publish per frame -> emit with matching
  name-tokens -> scrubber auto-groups. Grade = frame count within tolerance +
  animation plays.
- **satellite (after P1):** "Animate <satellite/product> fire imagery over
  <incident/AOI> for <window>; localize the AOI to the active hotspots; overlay FIRMS
  + perimeters." PASS = FIRMS-clustered AOI within tolerance of wrapper + frame-count
  preview via registered fetch_slider_timestamps + overlays present.
- **glm (after P2):** "Animate GLM lightning over <AOI> for <window> on a
  <visible/IR> base." PASS = fetch_glm_lightning per bucket + registered historical
  ABI base + composite + publish + emit. BLOCKED until P2.

## Consumer / test migration

- Registration imports to remove: `agent/tools/__init__.py:578` (satellite), `:585`
  (goes), `:593` (glm); `agent/workflows/__init__.py:49` (news), `:55`
  (conservation).
- `categories.py`: remove 5 category mappings (hazard_modeling fire/news/weather/
  conservation).
- `tool_query_corpus.yaml`: remove 5 central corpus blocks (satellite 6, goes 8, glm
  ~8, news 8, conservation 8). Re-home the retrieval INTENT onto retained fetchers so
  "animate fire / ingest news event / conservation view" still surfaces (open Q5).
- Delete/retire unit suites: test_model_satellite_fire_animation.py,
  test_model_goes_fire_animation.py, test_model_glm_lightning_animation.py,
  workflows/test_model_news_event_ingest.py, test_model_conservation_priority.py.
- Fix cross-ref assertions (these REFUTE two reader "no consumer" claims -- see
  disagreements): `test_elmfire_door.py:69` (goes registration-presence),
  `test_gemini_kwargs_fuzz.py` (news), `test_always_offload_heavy_tools.py:44/189/195`
  (glm).
- `scripts/tool_sweep.py:106/112`: prune satellite + glm smoke fixtures.
- experiments/ bench JSONs name all five but are frozen artifacts -- no code break;
  leave.
- qgis-plugin: no name references; but `render/temporal.py` name-token grouping must
  be preserved (P3), not edited.

## LOC accounting

- Cullable NOW (verdict CULL-DIRECT): news 801 + conservation 507 = **1308 LOC**.
- Cullable after P1: goes 832 + satellite 1337 = **2169 LOC**.
- Cullable after P2: glm 906 LOC.
- Total composer wrapper removed at end state = **4383 LOC** (+ 5 empty __init__.py),
  replaced by 1 playground-pattern doc + 2 small registered primitives (P1 tiny, P2
  real) + optional P4.

## Where reader and my judgment disagree (honest)

1. **glm "no test importers" (reader) vs cross-check** -- REFUTED: glm is imported by
   test_always_offload_heavy_tools.py:44/189/195. I side with the grep; migration
   must handle it.
2. **goes consumer** -- cross-check found test_elmfire_door.py:69 asserts goes
   registration presence; reader implied none. Side with cross-check.
3. **_densest_hotspot_bbox** -- reader calls it "trivial ~15 lines, playground-
   expressible." I lean toward extracting it as a reusable spatial primitive (P4):
   points-to-densest-bbox is an irreducible-ish DATA op many workflows would reuse,
   and doctrine treats such ops as primitives. Not a blocker either way; NATE's call.
4. **The two-phase confirm gate** -- reader frames it as "workflow-runtime plumbing
   not reproducible in a stateless code_exec cell," which reads as a KEEP factor for
   satellite. My judgment: the conversational agent loop reproduces stop-and-ask
   across turns; the ONLY genuinely-unreproducible piece is the pre-fetch frame-count
   PEEK, which is a missing-primitive (P1) issue, not a gate-protocol issue. So the
   gate is not itself a cull blocker.

## Open questions for NATE

1. Register `fetch_slider_timestamps` as a first-class atomic tool (promote the
   private helper, or add a thin registered wrapper)? Unblocks goes + satellite.
2. Historical single-band ABI (glm blocker): add `valid_time` to fetch_goes_satellite
   or ship a new `fetch_goes_abi_band` primitive?
3. densest_hotspot_bbox: extract as a registered spatial primitive (P4) or leave as a
   documented playground snippet? (see disagreement 3)
4. OK to publish the render/temporal.py name-token convention as a public playground
   contract (P3)? It couples playground output to the plugin's grouping rule.
5. Corpus re-homing: re-point the 5 culled corpus intents onto retained fetchers, or
   accept these become multi-step playground compositions the agent plans from
   primitives?
6. Sequencing: cull news + conservation now (gates pass live) -> extract P1 -> cull
   goes + satellite -> build P2 -> cull glm. Confirm.
7. Review-gate: accept the conversational loop as the replacement for the in-tool
   confirm=True/False handshake (news + satellite)?

## Addendum 2026-07-29 -- P2 DROPPED, glm CULLED (NATE)

This addendum supersedes the glm verdict above; the original sections are left
verbatim as the historical proposal record.

NATE (2026-07-29) REVISED the glm gate: the proposal's P2 (a historical single-
band ABI grayscale primitive) is DROPPED, not built. Rationale:

- The wrapper's baked grayscale-ABI base is a WEB-ERA ARTIFACT -- a fixed base
  map baked INTO each frame. In QGIS the base map is native/switchable (or a
  fetched imagery loop), so nothing needs a historical single-band ABI scan.
- The GLM gridding / GED colorizer the composer wrapped ALREADY lives in the
  retained `fetch_glm_lightning` fetcher, which fans an accumulation window into
  ordered `step <N>` scrubber frames on its own.
- Moving-base "lightning over satellite" asks are covered by the retained
  `fetch_goes_archive_animation` (ABI true_color / fire_temperature frames)
  co-published as a SECOND scrubber group under the GLM overlay group.

So glm is no longer EXTRACT-FIRST/blocked: with P2 dropped it is a CULL-DIRECT
playground composition, graded like phase A (fired-tool set == acceptable set +
published layers + `group_frame_layers` grouping). The gate PASSED live
(2026-07-29, Florida AOI, GOES-19): `fetch_glm_lightning(accumulation_window_s=
60)` -> 5 ordered GED frames -> `publish_layer` -> ONE scrubber group; the
moving-base variant co-published 3 `fetch_goes_archive_animation` true_color
frames for TWO groups total. The `run_model_glm_lightning_animation` composer +
its folder, tests, registration, categories, and central corpus block are
deleted (registry 198 -> 197); the animation intents re-home onto
`fetch_glm_lightning` / `fetch_goes_archive_animation` corpus, and the pattern is
documented in `docs/playbooks/frame-animation-recipe.md` (Recipe D + moving-base
variant). See `docs/decisions/0042-glm-cull-p2-dropped.md`.
