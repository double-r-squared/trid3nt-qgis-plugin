# Writing a tool

This is the step-by-step guide to adding your own atomic tool to the agent. An
"atomic tool" is a single Python function the LLM can call: a data fetcher, a
raster/vector compute, or an irreducible primitive. Follow the six steps below
and your tool will register at import time, route from natural-language prompts,
render its output on the map, and pass the mandatory acceptance checks.

> Scope reminder (a project norm): atomic tools are DATA fetchers and the
> simulation-automation primitives ONLY. Standard geoprocessing runs in the
> user's QGIS session through `run_qgis_algorithm`, and composed, multi-layer
> analyses through `run_pyqgis`, not in a new tool. If your idea is "fetch X" it
> is a fetch spec; if it is "pair a model with observations" it is a derive
> tool; if it is "slope of the DEM" or "combine layers A, B, C into an impact
> number", it is the session's work.

A DATA FETCHER is not written in Python at all: it is DECLARED as a
`source.yaml` beside a `corpus.yaml` under `trid3nt_server/tools/fetchers/`, and
the router promotes the declaration into a registered tool. Read
`trid3nt_server/tools/README.md` for that path. Everything below is the CODED
path - the derive, display, search and meta primitives.

Two files are your templates. Read them next to this guide:

- Canonical real example (a read over a handed-in layer returning a chart):
  `trid3nt_server/tools/derive/compute_cross_section/compute_cross_section.py`
- Copy-me starter (a trivial, dependency-free compute):
  `trid3nt_server/tools/_example_tool_template.py`

Everything below cites real code. Line numbers drift; grep the symbol.

---

## The six seams a new tool touches

1. The tool **function** + its **metadata** (`AtomicToolMetadata`) in its own
   DIRECTORY under `trid3nt_server/tools/<subpackage>/<tool_name>/`, holding
   `<tool_name>.py`, `corpus.yaml` and an `__init__.py`. Pick the subpackage by
   what the tool IS: `derive/` (compute_* / extract_* / charts / the two
   session tools), `search/` (catalog and tool retrieval), or `meta/`
   (utilities: case report, run frames, spatial input). `cache.py`, `vector_tiles.py` and `tool_arg_normalizer.py`
   deliberately stay at `tools/` root.
2. The **`@register_tool`** decorator (registers it in `TOOL_REGISTRY`).
3. An **eager import** in `trid3nt_server/tools/__init__.py`
   (so the decorator actually fires at startup).
4. **Corpus queries** in the sibling `corpus.yaml` (the retrieval index) + the
   mandatory `retrieve_visible_tools(prompt, None, 8)` check.
5. A **test** under `tests/<subsystem>/`, mirroring the product tree.
6. Observe the **1000-char docstring rule** (front-load routing).

---

## Step 1 - the tool function

### Signature conventions

```python
def compute_cross_section(
    layer_uri: str,
    line: Any,
    n_stations: int = _DEFAULT_N_STATIONS,
    extra_layer_uris: list[str] | None = None,
    *,
    _created_turn_id: str | None = None,
    **_extra_ignored: Any,
) -> dict[str, Any]:
    ...
```

- **Typed params.** The adapter builds the LLM's JSON-schema declaration from
  your signature + docstring
  (`adapter.py`, `FunctionDeclaration.from_callable_with_api_option`). Give every
  param a real type hint. Prefer `Literal[...]` enums for closed choices -- they
  survive schema generation and pin the LLM to valid values.
- **A derive tool takes a LAYER** (`layer_uri`, `dem_uri`, ...) and never
  fetches: the fetch tool that declares the data runs first, and the tool
  refuses, naming that fetch, when the layer was not given. `dev/lint/derive_fetches.py`
  refuses a registry lookup, a `read_through` or a fetcher import under
  `tools/derive`. A `bbox` belongs to a fetch spec, as `(min_lon, min_lat,
  max_lon, max_lat)` in EPSG:4326.
- **Trailing `**_extra_ignored: Any`.** Absorbs LLM over-supply. Underscore-
  prefixed params are stripped from the LLM-facing schema by
  `_strip_private_params` (`adapter.py`), so they are invisible to the model but
  keep the call from crashing when the model passes an extra key.

### Sync vs async

- **Compute tools are normally sync `def`** (like `compute_cross_section`).
  If the work is heavy/loop-blocking, do NOT block the asyncio loop yourself --
  the server offloads named heavy sync tools to a thread through
  `_ALWAYS_OFFLOAD_SYNC_TOOLS` (`server/dispatch/emitter.py`), which refuses to
  arm for a tool that emits; you just write a plain sync function.
- **Use `async def`** for engine/composer tools and anything that must `await`
  I/O directly (the no-sync-blocking-on-the-asyncio-loop rule).

### The return / result contract

Return one of:

- **A `LayerURI`** (`trid3nt_contracts.execution.LayerURI`) -- this is what puts a
  layer on the map. Fields (see the class): `layer_id`, `name`,
  `layer_type` (`"raster"` | `"vector"` | `"mesh"`), `uri` (a COG for raster,
  FlatGeobuf/GeoParquet for vector), `style` (the DECLARED style row; the
  publish path resolves it into `legend`), `quantity`,
  `role` (`"primary"` | `"context"` | `"input"`), `units`, `bbox` (optional;
  present triggers a `zoom-to`), `fallback_note` and `fallbacks` (the honesty
  markers when a fallback source or a declared ladder rung served the layer).
- **A plain `dict`** -- for tools whose answer is scalar/tabular, not a layer
  (the copy-me template returns a dict).
- **A `list[LayerURI]`** -- for animation-frame sequences.

### Emitting a layer (how a `LayerURI` reaches the map)

When your tool returns a `LayerURI` whose `uri` is a raw `s3://` COG for a
RASTER, the emission seam publishes it on the way out (`publish_for_emission` in
`trid3nt_server/emission/layer_uri_emit.py`, which calls `publish_layer` off the
event loop). There is no per-tool opt-out flag: a raster that should not be seen
is one the tool does not return. A failed publish degrades to the unstyled
`s3://` COG rather than dropping the layer. Vectors render inline as GeoJSON.
See `delineate_watershed`'s `WatershedLayerURI(...)` return for the
construction, style row included.

### Caching

Only a FETCHER caches, and a fetcher is declared, not coded. The one hand-written
exception, `lookup_precip_return_period` (a point read of an endpoint that returns
a scalar, not a layer), wraps its byte-producing call in `read_through` (from
`trid3nt_server.tools.cache`):

```python
result = read_through(
    metadata=_METADATA,
    params=params,          # dict that fully keys the request
    ext="csv",
    fetch_fn=_fetch,
)
```

`read_through` keys the cache off `metadata` + `params`; on a hit it returns the
stored uri without refetching. A derive tool never calls it: the layer it reads
was cached by the fetch that produced it.

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
`contracts/trid3nt_contracts/tool_registry.py`:

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
| `engine` | default `None` | Owning engine slug for an engine-door family member; `None` for every non-engine tool. |
| `tier` | default `None` | The door/template tier a family member sits at; `None` for a plain atomic tool. |

**Cross-field validator** (`_validate_cacheable_consistency`, runs at
construction):

- `cacheable=True` => `ttl_class != "live-no-cache"` AND `source_class` non-empty.
- `cacheable=False` => `ttl_class == "live-no-cache"`.

A bad combination raises `ValidationError` at import, before the tool is on the
wire.

Real metadata (`compute_cross_section/compute_cross_section.py`):

```python
_METADATA = AtomicToolMetadata(
    name="compute_cross_section",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
)
```

Decorate the function. Any non-`None` decorator kwarg overrides the metadata via
`model_copy(update=...)` and re-validates (fail-fast):

```python
@register_tool(_METADATA)
def compute_cross_section(layer_uri, line, n_stations=_DEFAULT_N_STATIONS, **_extra_ignored):
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
`trid3nt_server/tools/__init__.py`:

```python
from .derive.compute_cross_section import compute_cross_section  # noqa: E402,F401
```

The block is grouped by subpackage and sorted; add your line to the group
matching your module's folder.

This is what puts your tool in `TOOL_REGISTRY` at startup. Omit it and the tool
silently never exists.

---

## Step 4 - the corpus + the mandatory retrieval check

This is a HARD rule: **every new tool gets `corpus.yaml` queries AND must pass
the `retrieve_visible_tools(prompt, None, 8)` visibility check before
acceptance.**

### Why the corpus exists (the retrieval index)

The per-turn tool list is trimmed for token cost: instead of showing the LLM the
whole registry every turn, `retrieve_visible_tools`
(`trid3nt_server/tools/search/tool_retrieval.py`) composes the visible
set as:

```
CORE_FLOOR  UNION  the Case's accrued visible set  UNION  discover top-k
```

The `discover top-k` term ranks tools against the user's text with a BM25 + local
dense + name-substring fusion over an index built from each tool's audited
docstring **plus its `corpus.yaml` example queries**
(`search_tools._build_index`, which composes one flat corpus by walking every
co-located `corpus.yaml` under `tools/`). If your tool has no corpus entry, the index has
nothing but the docstring to route on, and natural user phrasings that do not
literally echo the docstring will MISS it -- the tool becomes unreachable even
though it is registered.

Add 5-10 realistic, natural user-prompt queries keyed by your function name, in
the `corpus.yaml` beside your module. Cover synonyms, regional variants, and
adjacent intent. Real entry (`derive/compute_cross_section/corpus.yaml`):

```yaml
compute_cross_section:
- draw a section view of the ground and water surface along this line
- plot the flood depth along the road through this neighborhood
- give me a cross-section profile of the head surface versus the land surface across the seepage zone
- overlay the DEM and the bathymetry along this transect so I can see bank to channel
- show me the freeboard along the levee, ground line versus water surface on one chart
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
from trid3nt_server.tools.search.search_tools import search_tools as dd
from trid3nt_server.tools.search.tool_retrieval import retrieve_visible_tools

dd._get_index()  # warm the BM25 + dense index from TOOL_REGISTRY + corpus
name = "compute_cross_section"
for q in ["show the elevation profile across this ridge"]:
    vis = retrieve_visible_tools(q, None, 8)
    assert name in vis, f"{name} not surfaced for {q!r}"
    assert len(vis) < len(TOOL_REGISTRY), "full registry == cold fail-open, not real routing"
```

`retrieve_visible_tools` is FAIL-OPEN by design (a cold index, an empty ranking,
or any error returns the full registry, logged) -- over-inclusion is cheap,
dropping a needed tool is a silent break. The check above rejects the fail-open
case so it proves the CORPUS actually routes.

---

## Step 5 - the test

The test tree MIRRORS the product tree, so a derive tool's test is
`tests/derive/test_<your_tool>.py`. Model it on
`tests/derive/test_compute_cross_section.py`. A minimal test asserts three things:
registration + metadata, the corpus coverage, and the tool's own behavior
(called directly via `TOOL_REGISTRY[name].fn`, since the decorator returns the
undecorated function):

```python
from trid3nt_server.tools import TOOL_REGISTRY

def test_registered():
    assert "compute_cross_section" in TOOL_REGISTRY
    m = TOOL_REGISTRY["compute_cross_section"].metadata
    assert m.ttl_class == "live-no-cache" and m.cacheable is False

def test_corpus():
    import pathlib, yaml
    from trid3nt_server.tools.derive.compute_cross_section import compute_cross_section as mod
    p = pathlib.Path(mod.__file__).resolve().parent / "corpus.yaml"
    corpus = yaml.safe_load(p.read_text())
    assert len(corpus["compute_cross_section"]) >= 3
```

Monkeypatch the network and the object store (see the sibling test's stubs) so
the test is offline and deterministic. Run it from the repo root with the agent
venv, through the slice that owns your subsystem:

```bash
venvs/agent/bin/python -m pytest tests/derive/test_<your_tool>.py -q
```

---

## Step 6 - the 1000-char docstring rule (front-load routing)

The OpenAI adapter **always truncates the tool description to 1000 chars**
(`trid3nt_server/adapters/openai_adapter.py`:
`(dumped.get("description") or dumped["name"])[:1000]`; `adapter.py` applies
the same `doc[:1000]` cap on the docstring-only fallback path). Provider
selection itself is `adapters/model_selection.py` (`MODEL_PROVIDER`, `openai`
default) - independent of any one adapter, so the cap is enforced per adapter
rather than once upstream of all of them. Everything past ~1000 chars is
invisible to the model.

Therefore:

- **Front-load the routing block** in the first ~1000 chars: What it does / When
  to use / When NOT to use. That is what the LLM reads to decide whether to call
  your tool.
- **Lift closed choices into `Literal[...]` in the signature** -- enums survive
  schema generation independently of the truncated prose.
- **Purge dead infra prose.** Do not spend the budget on implementation notes;
  put those below the routing block (for humans) where truncation is harmless.

The docstring structure that works: a one-line summary, then
`**What it does:**`, `**When to use:**`, `**When NOT to use:**`,
`**Parameters:**`, `**Returns:**`. Routing to a sibling tool belongs inside the
"When NOT to use" block, where it is routing; an implementation or cache note
does not belong in the docstring at all.

---

## The complete minimal tool, end to end

`trid3nt_server/tools/_example_tool_template.py` is a full,
working, copy-me tool: `example_bbox_area`, a geodesic (pyproj.Geod) area compute
that returns a dict. It shows metadata (a `cacheable=False` / `live-no-cache`
compute), the `**_extra_ignored` signature, a front-loaded routing docstring, the
typed error convention, and `@register_tool`. It ships gated behind
`TRID3NT_ENABLE_EXAMPLE_TOOL` so it stays out of the production catalog; a real
tool decorates unconditionally (delete the gate). It ships NO corpus, because an
inert tool must not sit in the retrieval index -- yours writes one.

To copy it into your own tool:

1. Make `<subpackage>/<your_tool>/` with an `__init__.py`, and copy
   `_example_tool_template.py` in as `<your_tool>.py`; rename the function, the
   `name=` in the metadata, and `__all__`.
2. Replace the body with your fetch/compute; return a `LayerURI` (map layer) or a
   dict (scalar/tabular).
3. Set the metadata correctly for your case (a derive tool over a handed-in
   layer: `cacheable=False` + `ttl_class="live-no-cache"` + `open_world_hint=False`).
4. Delete the `TRID3NT_ENABLE_EXAMPLE_TOOL` gate; decorate the function directly
   with `@register_tool(_METADATA, ...)`.
5. Add the eager import (step 3), the corpus (step 4), and the test (step 5).
6. Run the visibility check (step 4) and your test. Ship.
