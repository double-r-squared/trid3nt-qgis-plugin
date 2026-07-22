# Writing a tool

This is the step-by-step guide to adding your own atomic tool to the agent. An
"atomic tool" is a single Python function the LLM can call: a data fetcher, a
raster/vector compute, or an irreducible primitive. Follow the seven steps below
and your tool will register at import time, route from natural-language prompts,
render its output on the map, and pass the mandatory acceptance checks.

> Scope reminder (a project norm): atomic tools are DATA fetchers and
> irreducible primitives ONLY. Composed, multi-layer analyses belong in the
> `code_exec` python playground, not in a new tool. If your idea is "fetch X" or
> "compute one primitive from a raster", it is a tool. If it is "combine layers
> A, B, C into an impact number", it is a playground composition.

Two files are your templates. Read them next to this guide:

- Canonical real example (a self-contained raster fetcher):
  `server/src/trid3nt_server/tools/fetchers/ocean/fetch_noaa_slr_confidence.py`
- Copy-me starter (a trivial, dependency-free compute):
  `server/src/trid3nt_server/tools/_example_tool_template.py`

Everything below cites real code. Line numbers drift; grep the symbol.

---

## The seven seams a new tool touches

1. The tool **function** + its **metadata** (`AtomicToolMetadata`) in a new
   module under `server/src/trid3nt_server/tools/<subpackage>/` -- pick the
   folder by what the tool IS: `fetchers/<domain>/` (one file per fetch tool,
   filed by the phenomenon measured: weather / hydrology / ocean / terrain /
   imagery / climate / biodiversity / socioeconomic / hazard / soil),
   `processing/` (compute_* / clip_* / extract_* / charts, flat),
   `simulation/` (run_* engine bridges, model_* engines, the solver seam),
   `discovery/` (catalog + retrieval), or `meta/` (utilities).
   `publish_layer.py` and `cache.py` deliberately stay at `tools/` root.
2. The **`@register_tool`** decorator (registers it in `TOOL_REGISTRY`).
3. An **eager import** in `server/src/trid3nt_server/tools/__init__.py`
   (so the decorator actually fires at startup).
4. A **category** entry in `server/src/trid3nt_server/categories.py`.
5. **Corpus queries** in
   `server/src/trid3nt_server/data/tool_query_corpus.yaml` (the retrieval
   index) + the mandatory `retrieve_visible_tools(prompt, None, 8)` check.
6. A **test** under `services/agent/tests/`.
7. Observe the **1000-char docstring rule** (front-load routing).

---

## Step 1 - the tool function

### Signature conventions

```python
def fetch_noaa_slr_confidence(
    bbox: tuple[float, float, float, float],
    slr_ft: float = 3.0,
    res_deg: float | None = None,
    **_extra_ignored: Any,
) -> LayerURI:
    ...
```

- **Typed params.** The adapter builds the LLM's JSON-schema declaration from
  your signature + docstring
  (`adapter.py`, `FunctionDeclaration.from_callable_with_api_option`). Give every
  param a real type hint. Prefer `Literal[...]` enums for closed choices -- they
  survive schema generation and pin the LLM to valid values.
- **`bbox` is `tuple[float, float, float, float]`** = `(min_lon, min_lat,
  max_lon, max_lat)` in EPSG:4326. `_normalize_callable_for_gemini`
  (`adapter.py`) maps `tuple[float, ...]` to a JSON `list[float]` at the boundary.
- **Trailing `**_extra_ignored: Any`.** Absorbs LLM over-supply. Underscore-
  prefixed params are stripped from the LLM-facing schema by
  `_strip_private_params` (`adapter.py`), so they are invisible to the model but
  keep the call from crashing when the model passes an extra key.

### Sync vs async

- **Fetchers are normally sync `def`** (like `fetch_noaa_slr_confidence`). If the
  fetch is heavy/loop-blocking, do NOT block the asyncio loop yourself -- the
  server offloads registered heavy sync fetchers to a thread via
  `_ALWAYS_OFFLOAD_SYNC_TOOLS` (server-side); you just write a plain sync
  function.
- **Use `async def`** for engine/composer tools and anything that must `await`
  I/O directly (the no-sync-blocking-on-the-asyncio-loop rule).

### The return / result contract

Return one of:

- **A `LayerURI`** (`trid3nt_contracts.execution.LayerURI`) -- this is what puts a
  layer on the map. Fields (see the class): `layer_id`, `name`,
  `layer_type` (`"raster"` | `"vector"`), `uri` (a COG for raster,
  FlatGeobuf/GeoParquet for vector), `style_preset`,
  `role` (`"primary"` | `"context"` | `"input"`), `units`, `bbox` (optional;
  present triggers a `zoom-to`), `legend` (optional data-driven `LegendKey`),
  `fallback_note` (optional honesty marker when you substituted a fallback source).
- **A plain `dict`** -- for tools whose answer is scalar/tabular, not a layer
  (the copy-me template returns a dict).
- **A `list[LayerURI]`** -- for animation-frame sequences.

### Emitting a layer (how a `LayerURI` reaches the map)

When your tool returns a `LayerURI` whose `uri` is a raw object-store uri
(`s3://` / `gs://`) for a RASTER, the server auto-publishes it: the
`auto_publish` metadata flag (default `True`) makes the dispatch wrapper call
`publish_layer` server-side to convert the COG to a TiTiler `http(s)` tile URL --
no separate LLM step. Set `auto_publish=False` only for pure INTERMEDIATE rasters
the user should not auto-see (e.g. a raw DEM that only feeds `compute_hillshade`).
Vectors render inline as GeoJSON. See `fetch_noaa_slr_confidence` L133-142 for the
`LayerURI` construction.

### Caching

If your tool is a network fetcher, wrap the byte-producing call in `read_through`
(from `trid3nt_server.tools.cache`):

```python
result = read_through(
    metadata=_METADATA,
    params=params,          # dict that fully keys the request
    ext="tif",
    fetch_fn=lambda: export_slr_raster_cog_bytes(service, q_bbox, rd),
)
```

`read_through` keys the cache off `metadata` + `params`; on a hit it returns the
stored uri without refetching. (The copy-me template is `cacheable=False`, so it
does NOT use `read_through` at all.)

### The error / fallback convention

Never return a silent dead-end or a fabricated success. On bad input or upstream
failure, **raise a typed error** -- `ToolInputError` (from
`trid3nt_contracts.tool_registry` or `.errors`), or a tool-specific error subclass.
The server renders it as the `{status: error, error_code, retryable, message}`
envelope and feeds it back to the LLM as a `function_response` so the model
retries with corrected args or narrates honestly. Degrade primary -> fallback ->
typed error; when you substitute a fallback data source, set
`LayerURI.fallback_note` naming BOTH sources so the result is never mistaken for
the primary (the honesty floor).

---

## Step 2 - `AtomicToolMetadata` + `@register_tool`

Declare one `AtomicToolMetadata` at module load (it validates at construction, so
a misconfiguration fails fast at IMPORT time). Every field, from
`contracts/src/trid3nt_contracts/tool_registry.py`:

| Field | Required? | Meaning |
| --- | --- | --- |
| `name` | REQUIRED (`min_length=1`) | The function name = registry key (e.g. `"fetch_dem"`). |
| `ttl_class` | REQUIRED | One of `static-30d`, `semi-static-7d`, `dynamic-1h`, `live-no-cache`. The cache TTL bucket. |
| `source_class` | Required iff `cacheable=True` | Cache-bucket prefix (e.g. `"dem"`). `None` allowed when not cacheable. |
| `cacheable` | default `True` | `False` for interactive / emitter / writer / dispatcher tools. |
| `supports_global_query` | default `False` | Tool accepts `bbox=None` = global. If `False`, `bbox=None` must raise `ToolInputError(code='BBOX_REQUIRED')` before any network call. |
| `payload_mb_estimator_name` | default `None` | Name of a module-level `estimate_payload_mb(**args) -> float` used by the >25 MB chat-warning gate. |
| `read_only_hint` | default `True` | MCP annotation; `False` for writers (`publish_layer`, `run_solver`, ...). |
| `open_world_hint` | default `False` | `True` for anything hitting an external endpoint (all `fetch_*`). |
| `destructive_hint` | default `False` | `True` only for irreversible mutation (`publish_layer`). |
| `idempotent_hint` | default `True` | `False` for dispatchers / emitters / writers. |
| `auto_publish` | default `True` | Auto-publish a returned raster `LayerURI` carrying a raw `s3://`/`gs://` uri. `False` for pure intermediates. |

**Cross-field validator** (`_validate_cacheable_consistency`, runs at
construction):

- `cacheable=True` => `ttl_class != "live-no-cache"` AND `source_class` non-empty.
- `cacheable=False` => `ttl_class == "live-no-cache"`.

A bad combination raises `ValidationError` at import, before the tool is on the
wire.

Real fetcher metadata (`fetch_noaa_slr_confidence.py` L54-61):

```python
_METADATA = AtomicToolMetadata(
    name="fetch_noaa_slr_confidence",
    ttl_class="static-30d",
    source_class="noaa_slr_confidence",
    cacheable=True,
    supports_global_query=False,
    payload_mb_estimator_name="estimate_payload_mb",
)
```

Decorate the function. Any non-`None` decorator kwarg overrides the metadata via
`model_copy(update=...)` and re-validates (fail-fast):

```python
@register_tool(_METADATA, open_world_hint=True)
def fetch_noaa_slr_confidence(bbox, slr_ft=3.0, res_deg=None, **_extra_ignored):
    ...
```

`register_tool` (`tools/__init__.py`) stores a
`RegisteredTool(metadata, fn, module)` in the module-level
`TOOL_REGISTRY: dict[str, RegisteredTool]`, keyed by `metadata.name`. A
**duplicate name raises `ToolRegistrationError` at import** -- a copied template
must use a fresh name. The decorator returns the original function unchanged, so
tests call it directly via `TOOL_REGISTRY[name].fn(...)`.

---

## Step 3 - eager import (required)

`@register_tool` only fires if the module is imported. Add one line to the eager
import block near the bottom of
`server/src/trid3nt_server/tools/__init__.py`:

```python
from .fetchers.ocean import fetch_noaa_slr_confidence  # noqa: E402,F401
```

The block is grouped by subpackage and sorted; add your line to the group
matching your module's folder.

This is what puts your tool in `TOOL_REGISTRY` at startup. Omit it and the tool
silently never exists.

---

## Step 4 - categories.py

Every registered tool has exactly one **primary category**. Add your tool name to
`PRIMARY_CATEGORY` in `server/src/trid3nt_server/categories.py`:

```python
PRIMARY_CATEGORY: dict[str, str] = {
    ...
    "fetch_noaa_slr_confidence": "coastal",
    ...
}
```

Optionally cross-list it in `SECONDARY_CATEGORIES` when it materially belongs to a
second category too. The 12 categories are the `CategorySpec` entries in
`CATEGORIES`. A tool that is a cross-cutting entry point can also be added to the
always-on floor `HOT_SET_TOOLS`, but keep that set small -- it is loaded every
turn.

Note: `validate_function_call` (categories.py) auto-widens the allowed set for
ANY registry-valid call, so category membership drives `list_tools_in_category`
and discovery, not hard permission.

---

## Step 5 - the corpus + the mandatory retrieval check

This is a HARD rule: **every new tool gets `tool_query_corpus.yaml` queries AND
must pass the `retrieve_visible_tools(prompt, None, 8)` visibility check before
acceptance.**

### Why the corpus exists (the retrieval index)

The per-turn tool list is trimmed for token cost: instead of showing the LLM all
~190 tools every turn, `retrieve_visible_tools`
(`server/src/trid3nt_server/tools/discovery/tool_retrieval.py`) composes the visible
set as:

```
HOT_SET_TOOLS  UNION  the Case's accumulated allowed-set  UNION  discover top-k
```

The `discover top-k` term ranks tools against the user's text with a BM25 + local
dense + name-substring fusion over an index built from each tool's audited
docstring **plus its `tool_query_corpus.yaml` example queries**
(`discover_dataset._build_index`). If your tool has no corpus entry, the index has
nothing but the docstring to route on, and natural user phrasings that do not
literally echo the docstring will MISS it -- the tool becomes unreachable even
though it is registered.

Add 5-10 realistic, natural user-prompt queries keyed by your function name.
Cover synonyms, regional variants, and adjacent intent. Real entry
(`tool_query_corpus.yaml`):

```yaml
fetch_noaa_slr_confidence:
  - "how confident is the sea-level-rise inundation mapping at 3 feet here"
  - "show me the NOAA SLR mapping confidence raster for this coast"
  - "where is the sea-level-rise inundation high vs low confidence at 2 ft"
  - "give me the SLR mapping uncertainty overlay for this coastline"
  - "flag the low-confidence sea-level-rise areas before I report exposure"
  - "NOAA SLR viewer confidence layer at 5 feet for this estuary"
```

Follow the no-downtown-city and natural-prompts-no-bbox norms: use place names,
counties, states, or "this area", never `downtown <city>` (single-building
geocode) and never explicit bbox coordinates.

### The visibility check (acceptance gate)

Warm the discover index, then confirm every corpus query surfaces your tool in
the top-8 -- and that it is real routing, not a cold fail-open (a cold index
returns the FULL registry, which would "pass" trivially). This is the exact check
run against the copy-me template:

```python
import trid3nt_server.tools as T
from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools import discover_dataset as dd
from trid3nt_server.tools.tool_retrieval import retrieve_visible_tools

dd._get_index()  # warm the BM25 + dense index from TOOL_REGISTRY + corpus
name = "fetch_noaa_slr_confidence"
for q in ["show me the NOAA SLR mapping confidence raster for this coast"]:
    vis = retrieve_visible_tools(q, None, 8)
    assert name in vis, f"{name} not surfaced for {q!r}"
    assert len(vis) < len(TOOL_REGISTRY), "full registry == cold fail-open, not real routing"
```

`retrieve_visible_tools` is FAIL-OPEN by design (a cold index, an empty ranking,
or any error returns the full registry, logged) -- over-inclusion is cheap,
dropping a needed tool is a silent break. The check above rejects the fail-open
case so it proves the CORPUS actually routes.

---

## Step 6 - the test

Model your test on `services/agent/tests/test_fetch_noaa_slr_siblings.py`. A
minimal test asserts four things: registration + metadata, the category mapping,
the corpus coverage, and the tool's own behavior (called directly via
`TOOL_REGISTRY[name].fn`, since the decorator returns the undecorated function):

```python
from trid3nt_server.tools import TOOL_REGISTRY

def test_registered():
    assert "fetch_noaa_slr_confidence" in TOOL_REGISTRY
    m = TOOL_REGISTRY["fetch_noaa_slr_confidence"].metadata
    assert m.source_class == "noaa_slr_confidence"
    assert m.ttl_class == "static-30d" and m.cacheable is True

def test_categories():
    from trid3nt_server.categories import PRIMARY_CATEGORY
    assert PRIMARY_CATEGORY["fetch_noaa_slr_confidence"] == "coastal"

def test_corpus():
    import pathlib, yaml
    from trid3nt_server.tools import fetch_noaa_slr_confidence as mod
    p = pathlib.Path(mod.__file__).resolve().parents[1] / "data" / "tool_query_corpus.yaml"
    corpus = yaml.safe_load(p.read_text())
    assert len(corpus["fetch_noaa_slr_confidence"]) >= 3
```

Monkeypatch the network (see the sibling test's `_FakeClient` / `read_through`
stubs) so the test is offline and deterministic. Run with the agent venv:

```bash
cd services/agent && python -m pytest tests/test_<your_tool>.py -q
```

---

## Step 7 - the 1000-char docstring rule (front-load routing)

The Bedrock adapter **always truncates the tool description to 1000 chars**
(`server/src/trid3nt_server/bedrock_adapter.py`,
`tool_declarations_to_bedrock_tools`: `(dumped.get("description") or name)[:1000]`;
`adapter.py` applies the same `doc[:1000]` cap on the docstring-only fallback
path). Everything past ~1000 chars is invisible to the model.

Therefore:

- **Front-load the routing block** in the first ~1000 chars: What it does / When
  to use / When NOT to use. That is what the LLM reads to decide whether to call
  your tool.
- **Lift closed choices into `Literal[...]` in the signature** -- enums survive
  schema generation independently of the truncated prose.
- **Purge dead infra prose.** Do not spend the budget on implementation notes;
  put those below the routing block (for humans) where truncation is harmless.

The docstring structure that works (see `fetch_noaa_slr_confidence`): a one-line
summary, then `**What it does:**`, `**When to use:**`, `**When NOT to use:**`,
`**Parameters:**`, `**Returns:**`, `**Cross-tool dependencies:**`.

---

## The complete minimal tool, end to end

`server/src/trid3nt_server/tools/_example_tool_template.py` is a full,
working, copy-me tool: `example_bbox_area`, a dependency-free planar area compute
that returns a dict. It shows metadata (a `cacheable=False` / `live-no-cache`
compute), the `**_extra_ignored` signature, a front-loaded routing docstring, the
typed error convention, and `@register_tool`. It ships gated behind
`TRID3NT_ENABLE_EXAMPLE_TOOL` so it stays out of the production catalog; a real
tool decorates unconditionally (delete the gate). Its corpus block lives under
`example_bbox_area:` in `tool_query_corpus.yaml`.

To copy it into your own tool:

1. `cp _example_tool_template.py fetch_my_thing.py`; rename the function, the
   `name=` in the metadata, and `__all__`.
2. Replace the body with your fetch/compute; return a `LayerURI` (map layer) or a
   dict (scalar/tabular).
3. Set the metadata correctly for your case (a fetcher: `cacheable=True` +
   `ttl_class="static-30d"` + a `source_class` + `open_world_hint=True`).
4. Delete the `TRID3NT_ENABLE_EXAMPLE_TOOL` gate; decorate the function directly
   with `@register_tool(_METADATA, ...)`.
5. Add the eager import (step 3), the category (step 4), the corpus (step 5), and
   the test (step 6).
6. Run the visibility check (step 5) and your test. Ship.
