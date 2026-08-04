# ADR 0117 -- ESRI Living Atlas as a discoverable, two-pool data population

Status: accepted (2026-08-04, NATE tier-2 approval)
Follows: stratified-pools.md (the signed pool architecture, flag-gated NO_ADVANCE),
0053/0066 (ArcGIS imageserver_export / esri_json router modes), the catalog
tooling (search_data_catalog / fetch_from_catalog).

## Context

NATE, verbatim: "only adding authoritative to our data pool and maybe adding
another pool for community data pools so they never get priority in a
authoritative data fetch ask." The public ESRI Living Atlas of the World is a
large (~10k items), keyless, ArcGIS-served corpus. We want it as a discoverable
data population WITHOUT letting community-curated content compete with
authoritative content on an authoritative ask.

## Decision

### 1. The harvest (`scripts/harvest_living_atlas.py`, ops tooling)

Query the keyless ArcGIS Online `sharing/rest/search` API for the PUBLIC items in
the canonical Living Atlas of the World curation group ("LAW Search", owner
`Esri_LivingAtlas`, id `47dd57c9a59d458c86d3d6b978560088`), paginated per
consumable service type (Image / Feature / Map Service; web maps/apps/scenes
skipped). Normalize each item to a fetchable entry and write TWO YAML catalogs
(DATA, not code) under `data/living_atlas/`. Re-runnable / idempotent; polite
rate limit; offline-testable via `--fixture`.

Live harvest (2026-08-04): 6737 unique consumable items (703 Image + 5591 Feature
+ 443 Map), 3434 authoritative / 3303 community, 1030 premium-flagged.

### 2. Authoritative detection -- the API-field verdict

The ESRI authoritative badge is the item `contentStatus` field, values
`public_authoritative` / `org_authoritative` (verified against live items). It is
NOT a searchable `q=` filter (returns 0), so the harvest reads it per-item and
splits in-code: `authoritative = contentStatus.endswith("authoritative")`.
Premium/subscription content is the `typeKeywords` signal (`Requires
Subscription` / `Requires Credits`) -- also not searchable, filtered in-code.

### 3. The two strata (NATE's rule, structural)

- `living_atlas_authoritative.yaml` joins the data-source search surface as the
  DEFAULT population.
- `living_atlas_community.yaml` is a SEPARATE stratum with ZERO default quota.
  It surfaces ONLY on explicit `include_community=true` (a small labelled quota,
  always ranked BELOW the authoritative results, never interleaved above) or as a
  LABELLED last resort when the authoritative stratum returns nothing.

The composition is a per-stratum BM25(+dense) index (`living_atlas_index.py`,
reusing `search_tools`'s `_tokenize` / `_TypoTolerantBM25` / `_select_dense_backend`
/ `_reciprocal_rank_fusion` -- the exact machinery `_router/stratified.py` reuses)
with no fused leaderboard across strata.

### 4. Pools-arm relationship (built vs gated)

The signed pools arm (`TRID3NT_CATALOG_ARM`) is flag-gated with a NO_ADVANCE
verdict (ADR 0050), so the LIVE surface is a SCOPED SEARCH TOOL, not a pool:
`search_living_atlas(query, include_community=false)` -- one registered coded
tool, BM25+dense over the harvested entries, returning ranked entries + curation
flag + fetch instructions. The per-stratum index + quota composition ARE the pool
mechanics, so when the arm lights this ranking drops behind the harness trigger
unchanged. Chosen: build the tool now (NATE's lean given the gate).

### 5. The fetch bridge + the DYNAMIC SourceSpec verdict

`fetch_living_atlas_layer(item_id | service_url, bbox)` -- one registered coded
tool -- builds a DYNAMIC `SourceSpec` per call from the entry's service type and
hands it to the router's `route()`.

VERDICT: a dynamic SourceSpec RIDES `route()` for free. `route(spec, params)` is
registry-free and spec-driven (`try_dispatch` is a no-op without `spec.dispatch`;
`synthesize_metadata` / `validate_params` / `select_executor` / `read_through` all
read the passed spec object), so param validation, the payload gate, typed errors,
caching, and LayerURI emission all ride unchanged. No pre-registration, no bespoke
transport. Service-type -> mode: Image Service -> `imageserver_export`, Map Service
-> `mapserver_export`, Feature Service -> `esri_json` vector.

Two deliberate engineering points:
- CACHE DISAMBIGUATION: the content-addressed cache key is
  `sha256(source_class || params || ttl)` and does NOT include the service URL, so
  `source_class` is per-item (`living_atlas_<item_id>`) -- two different layers at
  the same bbox never collide.
- URL SHAPING: `imageserver_export` / `mapserver_export` rebuild
  `{base}/{service}/ImageServer|MapServer/export...` from a `service_by_param` map,
  so the builder strips the `/ImageServer` (or `/MapServer`) suffix and splits off
  the last path segment as the service name, plus a single-value synthetic `_svc`
  enum param. Feature Service probes the FeatureServer for the first sublayer id.

### 6. Curation + premium honesty

The fetch return is `LivingAtlasLayerURI` (a `LayerURI` subclass) carrying
`curation` (authoritative|community), `item_id`, `service_type`, and a
`provenance` dict -- so the agent/user can never mistake community content for
authoritative. Premium/subscription items raise a typed
`LIVING_ATLAS_SUBSCRIPTION_REQUIRED` error (missing-key parity: no ArcGIS token is
registered, so premium content is never silently half-fetched). The gate is the
harvest `premium` flag PLUS a fetch-time `?f=json` token-required probe.

## Consequences

- +2 coded tools (search + fetch); registry 177 -> 179 (daemon), 173 -> 175
  (in-process); spec-served (data) unchanged (both are coded, no `source.yaml`).
- The two harvested catalogs are DATA (excluded from coded LOC); the harvest is
  ops tooling.
- The Living Atlas is now discoverable + fetchable end-to-end, with NATE's
  two-pool rule enforced structurally at both the search and fetch surfaces.
- Not a bespoke per-source fetcher: `fetch_living_atlas_layer` is a GENERIC
  dynamic-spec router client, so it does NOT count against the fold-to-zero coded-
  fetcher endgame -- it embodies the spec-driven pattern rather than adding a twin.
