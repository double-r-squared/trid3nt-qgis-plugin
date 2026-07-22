# grace2_agent.tools — atomic-tool registry + cache shim

This package is the agent service's M4 atomic-tool surface (FR-AS-3,
FR-CE-8, FR-TA-2, Decision O). It owns:

- **The registry** (`__init__.py`) — `@register_tool(metadata)` decorator,
  module-level `TOOL_REGISTRY`, and the `get_registered_tools()` snapshot
  helper the agent loop builds its tool declarations from.
- **The cache shim** (`cache.py`) — `compute_cache_key`, `cache_path`,
  `read_through`, `is_cacheable`, `ttl_bucket_vintage`. Mediates every
  external-API atomic-tool fetch per FR-DC-3.
- **Pass-through tools** (`passthroughs.py`) — `qgis_process`,
  `cacheable=False` + `ttl_class="live-no-cache"` per FR-DC-6. (A `mongo_query`
  pass-through formerly lived here; it was removed when MongoDB Atlas was torn
  down for DynamoDB, 2026-06-16.)

The `AtomicToolMetadata` model itself is `schema`-owned and lives in
`grace2_contracts.tool_registry`; this package consumes it.

## Registering a cacheable tool (`static-30d` example)

```python
from grace2_contracts.tool_registry import AtomicToolMetadata
from grace2_agent.tools import register_tool
from grace2_agent.tools.cache import read_through

@register_tool(AtomicToolMetadata(
    name="fetch_dem",
    ttl_class="static-30d",
    source_class="dem",
    cacheable=True,
))
def fetch_dem(bbox: tuple[float, float, float, float]) -> str:
    """Fetch a 3DEP DEM tile for the given bbox.

    Use this when: a workflow needs ground elevation for a study area.
    Do NOT use this for: bathymetry (use fetch_bathymetry instead).
    """
    metadata = TOOL_REGISTRY["fetch_dem"].metadata
    result = read_through(
        metadata=metadata,
        params={"bbox": bbox},
        ext="tif",
        fetch_fn=lambda: _download_3dep_tile(bbox),
    )
    return result.uri  # gs://grace-2-hazard-prod-cache/cache/static-30d/dem/<hash>.tif
```

The `@register_tool` decorator:

- Re-validates the metadata (pydantic auto-validates at construction; the
  FR-DC-6 cross-field rule already runs there).
- Records `(fn, metadata, module)` in `TOOL_REGISTRY` keyed by name.
- Fails fast on duplicate names (raises `ToolRegistrationError` at import
  time per FR-CE-8).
- Returns `fn` unchanged so it's still directly callable in tests.

## Registering a `live-no-cache` tool

`qgis_process` is the canonical example. It declares:

```python
AtomicToolMetadata(
    name="qgis_process",
    ttl_class="live-no-cache",
    source_class=None,         # uncacheable: no bucket prefix needed
    cacheable=False,
)
```

The cache shim's `read_through` short-circuits these — it calls `fetch_fn`,
returns `ReadThroughResult(uri=None, hit=False)`, and writes nothing.
The FR-DC-6 enumeration is honored by the metadata declaration plus the
`AtomicToolMetadata` cross-field validator: `cacheable=False` requires
`ttl_class == "live-no-cache"`, and vice versa.

## Forcing a refresh (FR-DC-6 `cache=false` opt-in)

`read_through` exposes `force_refresh=True` for the one-shot diagnostic
case in FR-DC-6: "fetch the absolute latest from NWIS as of right now". The
fresh response is still written through the cache so subsequent callers
benefit, but the lookup is skipped.

```python
result = read_through(
    metadata=fetch_dem_metadata,
    params={"bbox": bbox},
    ext="tif",
    fetch_fn=lambda: _download_3dep_tile(bbox),
    force_refresh=True,
)
```

This is TENTATIVE per the kickoff Open Questions; the alternative (no
override at all) would force callers to delete the blob out-of-band.

## Startup wiring

Importing `grace2_agent.tools` triggers the import-time `@register_tool`
decorators in `passthroughs` (and the other tool submodules), populating
`TOOL_REGISTRY`. The agent loop reads `get_registered_tools()` (a sorted
snapshot, for deterministic diffs) to build its Bedrock Converse tool
declarations directly via the raw SDK in `adapter.py` — there is no ADK
wrapper (`google-adk` was dropped in the GCP decommission).

## Cache key derivation (FR-DC-3)

```
key = sha256(source_id || canonical_params_json || ttl_bucket_vintage)[:32]
```

- `canonical_params_json`: sorted keys, `None` values dropped, compact JSON.
  Bbox / date quantization is the **caller's** responsibility (it's
  domain-specific; the shim stays engine-agnostic).
- `ttl_bucket_vintage`: per TTL class:
  - `static-30d` → `YYYY-MM`
  - `semi-static-7d` → `YYYY-Www`
  - `dynamic-1h` → top-of-hour UTC `YYYY-MM-DDTHH:00:00Z`
  - `live-no-cache` → `"live"` (never lands; `read_through` short-circuits)
- Truncated to 32 hex chars = 128 bits collision resistance (TENTATIVE per
  kickoff; longer narrows collision probability at the cost of path length).

## Bucket layout (per job-0031 live substrate)

```
gs://grace-2-hazard-prod-cache/cache/<ttl-class>/<source-class>/<hash>.<ext>
```

Note: the live substrate nests TTL class above source class, NOT the
FR-DC-1 literal (`cache/<source-class>/<hash>.<ext>`). job-0031's
`OQ-INFRA-31-FR-DC-1` proposes the matching SRS amendment for v0.3.16.
