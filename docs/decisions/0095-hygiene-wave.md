# ADR 0095 -- Combined hygiene wave: north-star purge, composer characterization, CaMa deletion, NEXRAD reclassify

Status: accepted (2026-08-03)
Follows ADR 0094 (door dissolution). NATE-decided 2026-08-03/04. Four independent
legs landed in one wave.

## Context

Post-door-dissolution cleanup. Four unrelated hygiene items accumulated, all
NATE-decided, all touching the tool/workflow surface:

1. The term "North Star" (the overfit-era demo-replication framing NATE has since
   repudiated -- see ADR 0024) still rode ~30 docstrings, comments, and specs.
2. Ten `model_*` scenario composers (3 registered + retrievable, 7 internal
   siblings) needed a keep-vs-cull characterization after the door dissolve.
3. `fetch_cama_flood_discharge` (CaMa-Flood global river discharge) violated the
   US-only doctrine and had a dead registration-gated upstream.
4. `fetch_nexrad_reflectivity` was miscategorized as a fetcher -- it composes a
   live WMS GetMap URL and transfers no data bytes.

## Decision

### Leg 1 -- North-Star verbiage purge (term BANNED)

Reworded every "North Star" carrier in `server/src`, `server/tests`, `services`,
and the non-ADR specs to plain description / "reference scenario" / "validation
case". No history notes ("formerly the North Star") -- comments state
constraints, not archaeology. Load-bearing engineering prose kept verbatim; only
the epithet was stripped. The two OLDER ADRs that carry the term (0024, 0041) are
immutable decision history and were LEFT UNTOUCHED (the term is not propagated
anywhere new). Retrieval-sensitive docstrings (fetch_topobathy, the SFINCS flood
composer, the adapter routing block) reworded without dropping domain keywords;
the model-free retrieval surface is rank-stable before/after (fetch_topobathy
top-8 for its coastal-bathymetry queries, identical retrieved sets).

### Leg 2 -- Composer keep-vs-cull characterization (report-only)

`docs/validation/composer-cull-characterization.md` characterizes the 10
composers: 3 KEEP (run_model_nws_flood_event_scenario,
run_model_groundwater_contamination_scenario, compute_impact_envelope -- each a
genuinely distinct question archetype above its engine's atom template) and 7
CULL-CANDIDATE (the internal `model_*` siblings, each the 1:1 private
orchestration body of exactly one registered engine template under an overfit
scenario name). No deletions -- NATE decides; 7 QUEUED ledger rows drafted (each
condition is a fold-into-template or rename, never an engine-run deletion).

### Leg 3 -- fetch_cama_flood_discharge DELETED

US-only doctrine + NWM covers US rivers + a dead registration-gated U-Tokyo
mirror (no live no-auth source to prove parity against). Supersedes the ADR 0078
PERMANENT-BESPOKE HELD verdict and the ADR 0069 HELD row. Removed the module,
test, corpus, registry/main registration, categories catalog entry + description,
the `_ALWAYS_OFFLOAD_SYNC_TOOLS` entry, the adapter error-class mention, the
credential support-page map entry, the fuzz sample-args, and all sibling
docstring cross-references. Registry 176->175, coded tools 86->85. The generic
SFINCS `discharge_forcing_from_cama_cog` sampler (+ `cama_cog` plumbing) is
RETAINED but now orphaned (no producer) -- a separate QUEUED ledger row, because
excising it is engine-seam flood-solver work outside this leg's scope and
flood-canary-risky. Reopen only on a live no-auth CaMa mirror + a real global
need.

### Leg 4 -- fetch_nexrad_reflectivity -> show_nexrad_radar (reclassify + rename)

The ADR 0078 PERMANENT-BESPOKE verdict STAYS TRUE (a zero-byte WMS-URL composer,
coded, no SourceSpec home) -- it was just miscategorized as a fetcher. Moved the
module out of `tools/fetchers/weather/` into a new `tools/display/` package (the
honest home for live map-overlay tools that compose a service URL and transfer no
bytes; the co-located corpus.yaml stays discoverable via the `tools/**` rglob).
Renamed the registered tool `fetch_nexrad_reflectivity` -> `show_nexrad_radar`:
registry name, docstring (rewritten to say it composes a live WMS GetMap URL and
fetches nothing), corpus (re-authored for radar phrasings), and every
consumer/test re-pointed. NOT a deletion -- registry count unchanged. Retrieval
proof: `show_nexrad_radar` surfaces top-8 model-free for all four radar queries
(parity with the pre-rename name).

## Consequence

- Registry 176 -> 175 (cama -1; nexrad renamed, not removed).
- Coded tools 86 -> 85 (cama -1; nexrad stays coded).
- Campaign coded-data-fetcher counter 7 -> 5. NEW BASIS: the remaining coded
  data-fetchers IN the fetchers package are topobathy, dem, storm_tracks,
  goes_satellite, nwm = 5 (cama deleted; nexrad reclassified out to
  tools/display).
- New `tools/display/` package established for zero-byte service-URL overlay
  tools (the "future display-services pool" the ADR 0078 nexrad row anticipated).
- "North Star" survives only in the two older immutable ADRs (0024, 0041).
- Offline suite baseline unchanged: 9 failures (test_fetch_resolution_gate x4 +
  test_run_river_dye_scenario x5).
