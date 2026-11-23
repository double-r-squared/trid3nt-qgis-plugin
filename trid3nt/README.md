# `trid3nt/` - the SDK

The domain: fetchers and derive tools, the mesh front, the engines and their
templates, the typed inputs, the format set, the store, settings and
credentials. It imports neither `trid3nt_harness` nor `trid3nt_server`: a seam
an upper package fills is a hook defined here and registered from above, and
with nothing registered nothing is emitted and nothing asks.

## Files

| file | what it is |
| --- | --- |
| `__init__.py` | The package door: `case`, loaded on first use. |
| `errors.py` | `DeclarativeError` - the base every typed failure carries its `error_code` on, below both the input layer and the template runtime. |

## Subfolders

| subfolder | what lives there |
| --- | --- |
| `credentials/` | The connect handshake, and the resolver over the credential each row declares. |
| `inputs/` | Every input into the system as a typed value: a Point, an Extent and a Shape, each with one ingestion from every form a user hands it in, and the user's own file adopted as a layer. |
| `sdk/` | The SDK's own front: `case(name)` and a Model over a pinned called or copied template, set by attribute, filled and run in-process, unattended. |
| `render/` | The format set a product reaches the map in - a COG raster, a GeoJSON vector, an MDAL mesh, a chart payload - and everything it passes through on the way. |
| `session/` | The requests a live QGIS session answers - a processing run, a provider layer - and the replies they park on; with no session bound each refuses. |
| `settings/` | The changeable judgments: one keyword dictionary, three levels (default, the user's file, the run), and the one reader every module calls. |
| `store/` | What the daemon keeps: the object store, the case documents, and the sweep over the cache. |
| `tools/` | The registered tool surface: fetchers and derive tools, the mesh front, with search and meta beside them. |
| `workflows/` | The engine layer: the template runtime, the executor, the engine packages. |
