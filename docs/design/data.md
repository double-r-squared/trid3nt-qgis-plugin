# data/ -- the fetcher and tool surface

`trid3nt_server/tools/` (was `agent/tools/`, renamed in ADR 0277) is the
LLM-facing tool surface: the atomic-tool registry, the spec-driven fetcher
router, and the co-located data assets.

## What lives here

- `__init__.py` -- the `register_tool` decorator + `TOOL_REGISTRY` (collects
  every decorated function at import time; 255 tools after `main._import_tools_registry()`).
- `cache.py` -- the read-through cache shim mediating external-API calls.
- `tool_arg_normalizer.py` -- argument coercion (`coerce_bbox_value`, shape
  normalization) applied before a tool wrapper runs.
- `fetchers/` -- one folder per fetcher; `_router/` is the spec engine
  (`spec.py`, `router.py`, `registration.py`, executors, hooks, transforms,
  transport). Fetchers are DATA-only; analysis is code_exec in the playground.
- `processing/`, `search/`, `simulation/` (setters + `solver` + diagnostics),
  `meta/`, `display/`, `publish_layer/` -- the remaining tool families.
- `tool_query_corpus.yaml`, `duckdb_spatial_functions.json` -- residual data
  assets (moved with the package; `search_tools` and `search_spatial_functions`
  resolve them via `parents[3] / "data" / ...`).

## Composition

`workflows/` composers and `gates/cards/` import tool primitives from here
(absolute `trid3nt_server.tools.*`). Fetchers surface input layers via the
emit-on-fetch seam (`purpose=` on router fetches), consumed by `emission/`.
The retrieval corpus is composed by walking `data/**/corpus.yaml` AND
`workflows/**/corpus.yaml` plus the residual `tool_query_corpus.yaml`.

## Invariants / extension points

- Atomic tools are DATA fetchers + irreducible primitives ONLY -- never
  composed analyses.
- A source with no live value API can be a STAGED DATASET (ADR 0297): a
  committed `scripts/stage_*.py` converts the published archive to a COG,
  proves it against the publisher's own numbers, and uploads it plus a
  provenance sidecar under `s3://<cache-bucket>/staged/<capability>/<version>/`
  -- outside the TTL-evicted `cache/` tree. The spec names the object by
  `s3://bucket/key`; `transport/staged.py` resolves the host at read time.
- A new tool completes the registry checklist: registration import, catalog
  pins, co-located `corpus.yaml`, retrieval top-8 check.
- Gates on tools are DECLARED (GateSpec metadata), never hand-wired.
