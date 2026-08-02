# 0036 - generic data-router (fetcher fold) router core

Context: the fetcher family is ~71.8k lines (38% of the agent surface). The audit
(`docs/specs/fetcher-fold-audit.md`) found every fetcher is the same skeleton --
~75-85% boilerplate (typed errors, metadata, payload estimate, cache read-through,
LayerURI) and ~15-25% source-specific body (endpoint(s), response->COG/FGB
ingestion, normalization stamps). NATE greenlit a generic data-router: one YAML
`source.yaml` per source + one shared engine, gated by routing-parity and
replication-parity experiments before any twin is cut (retention principle -- no
endpoint is ever removed; a fetcher only changes FORM).

Decision (phase-1 router core, lane B1; authority `docs/specs/router-pilot-contract.md`):
- `SourceSpec` (pydantic, `trid3nt_contracts.source_spec`, mirrors `CatalogEntry`)
  is the single validated shape both the loader and the parity harness read, so
  the two paths cannot drift. The shape-specific `ingest` + `join` blocks are
  flexible dicts; every other top-level key is a strict typed sub-model.
- One engine (`_router/router.py`): resolve spec -> validate params -> gate ->
  dispatch to an executor by `shape` -> read_through cache -> emit LayerURI. Each
  executor is a pure `(spec, params) -> bytes` closure; the router binds the four
  shared seams by REUSING the exact existing functions (`read_through`,
  `_fetch_common` bbox helpers, `AtomicToolMetadata`, `LayerURI`) -- nothing
  duplicated. Typed errors are stamped `<SOURCE_CLASS>_<SUFFIX>` at raise time so
  the A.6 frame is byte-identical to the hand-written twin (indistinguishability).
- Executors: raster-cog (opendap / direct-window / stac-search), vector-fgb
  (ArcGIS resultOffset pagination + honest-empty header-only FGB),
  station-timeseries-fgb (catalog-discover + per-station loop + inline
  time_series_csv). Named transforms as first-class primitives: tiled-mosaic
  (wraps raster-cog N times + rasterio.merge, categorical-safe) and JOIN-on-key
  (geometry + values left-join, missing -> null NEVER fabricated).
- Fold-arm surfacing is an EXPERIMENT-TIME env toggle (`TRID3NT_FETCHER_FOLD_ARM`),
  never a tree change. The spec-driven virtual tool registers under an internal
  alias `fetch_X__spec` at `tier="template"` (so the EXISTING template-exclusion
  filter keeps it out of the default pool -- baseline byte-identical). The three
  pool producers (`_build_index`, `_full_registry_floor`,
  `_default_declarable_registry`) consult a gated substitution map that swaps the
  twin for its virtual entry under the twin's name when the arm is set. Every
  substitution helper is a STRICT no-op when the env is unset OR no specs are
  registered (the default), so the default pool is provably unchanged when off.

Consequence: a source becomes ~40-60 lines of YAML + the shared engine instead of
~650 lines of Python, and a spec-driven source is indistinguishable from a
hand-written twin (identical pipeline, seams, error frames, surface). The hand-
written twins STAY registered and untouched during the pilot; a twin is deleted
only after BOTH routing and replication gates pass (cull doctrine). B1 does NOT
wire startup spec registration or dispatch-callable swap -- dispatch resolves the
raw `TOOL_REGISTRY[name].fn` and the replication harness calls the router executor
directly; those seams belong to the pilot / experiment lanes.
Related: 0034 (tier-based pool exclusion this reuses), 0019, 0031 (LayerURI).
