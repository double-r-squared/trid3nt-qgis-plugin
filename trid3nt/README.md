# `trid3nt/` - the SDK

The domain: fetchers and derive tools, the mesh front, the engines and their
templates, the typed inputs, the format set, the store, settings and
credentials. It imports neither `trid3nt_harness` nor `trid3nt_server`; the
edges that still reach up are listed in the import-graph test's allow-list.

## Files

| file | what it is |
| --- | --- |
| `__init__.py` | The package door. |
| `errors.py` | `DeclarativeError` - the base every typed failure carries its `error_code` on, below both the input layer and the template runtime. |

## Subfolders

| subfolder | what lives there |
| --- | --- |
| `credentials/` | The connect handshake, and the resolver over the credential each row declares. |
| `inputs/` | Every input into the system as a typed value: a Point, an Extent and a Shape, each with one ingestion from every form a user hands it in, and the user's own file adopted as a layer. |
| `render/` | The format set a product reaches the map in - a COG raster, a GeoJSON vector, an MDAL mesh, a chart payload - and everything it passes through on the way. |
| `settings/` | The changeable judgments: one keyword dictionary, three levels (default, the user's file, the run), and the one reader every module calls. |
| `store/` | What the daemon keeps: the object store, the case documents, and the sweep over the cache. |
| `tools/` | The registered tool surface: fetchers and derive tools, the mesh front, with search and meta beside them. |
| `workflows/` | The engine layer: the template runtime, the executor, the engine packages. |
