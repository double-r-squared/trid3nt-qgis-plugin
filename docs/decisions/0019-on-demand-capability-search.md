# 0019 - search wins at scale; enumerate only what's small and hot

Context: three capability surfaces needed a routing story - the ~190-tool
atomic registry (large, growing, mostly cold per turn), the curated external
data-source catalog (large, documented, occasional), and the DuckDB
``spatial`` extension's ~285 SQL functions (large, documented, only touched
when composing ``spatial_query`` SQL). A category-tree ("browse: hazards ->
flood -> zones") was considered and rejected for all three at this scale: a
tree multiplies wrong turns (each wrong branch is a full round-trip), grows
dead ends as the tree gets deeper than the corpus is wide, and gives the
model nothing to compute-bound on - at 190 tools a flat BM25+dense rank is
sub-millisecond CPU, so there is no latency budget a tree would actually buy
back. Category browsing (``list_categories`` / ``list_tools_in_category``)
still exists as a fallback path, not the front door.

Decision: SEARCH is the front door for any surface that is large,
documented, and touched occasionally per turn; ENUMERATION (a small fixed
list, or a hot-set floor) is the front door for anything small and hit on
most turns. Concretely:
- ``search_tools`` (renamed from ``discover_dataset``): hybrid BM25 + dense
  RRF over the atomic-tool registry. Named verb-first to match the family.
- ``search_data_catalog`` / ``fetch_from_catalog`` (renamed from
  ``catalog_search`` / ``catalog_fetch``): the curated external-data
  Mode-1 substrate (0008 still governs the search -> catalog -> offer-to-add
  ladder; only the names moved).
- ``search_spatial_functions`` (new): BM25 over a vendored, offline JSON dump
  of the installed DuckDB ``spatial`` extension's function catalog
  (``duckdb_functions()`` filtered to ``ST_*``). Composing ``spatial_query``
  SQL no longer requires ``spatial_query``'s own docstring to enumerate every
  ``ST_*`` function inline (that prose was unbounded and duplicated this
  tool's job) - it now says "call ``search_spatial_functions`` when unsure"
  and stops there.
- The hot set (``HOT_SET_TOOLS``, ~17 tools) and the per-Case
  ``AllowedToolSet`` stay ENUMERATED, not searched: they are small, and
  every turn needs them, so a lookup would be pure overhead for zero benefit.

Every routing layer built on search keeps a flat, unconditional fallback:
``search_tools`` degrades to a name-substring match when BM25 and dense both
miss; ``retrieve_visible_tools`` FAIL-OPENS to the full registry on a cold
index or empty ranking; ``search_spatial_functions`` falls back to substring
match over function names/descriptions when ``rank_bm25`` is unavailable.
Nothing in this design has a path that returns nothing usable.

Revisit trigger: ~1000 tools in the atomic registry AND a measured
degradation in flat-ranking recall@k (not a hunch) - only then does a
category layer earn its error-multiplication cost. Renaming three tools
(``search_tools`` / ``search_data_catalog`` / ``fetch_from_catalog`` /
``search_spatial_functions``) cost one corpus-key edit + a grep-driven sweep
of every reference; a category tree would have cost a second routing
decision on every single turn, forever.
