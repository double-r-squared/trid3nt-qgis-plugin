# `trid3nt_server/` - the daemon

The local server the QGIS plugin talks to: one WebSocket process that holds a
turn loop, dispatches the registered tools, drives the engine workflows and
publishes what they produce back onto the caller's canvas. Everything here runs
on one machine against one user; the only wire shapes it speaks are
`trid3nt_contracts`.

## Files

| file | what it is |
| --- | --- |
| `__init__.py` | The package door and its version. |
| `__main__.py` | `python -m trid3nt_server` - the way the daemon is started. |
| `main.py` | The `trid3nt-server` console script: importing `trid3nt_server.tools` is what populates the registry. |
| `errors.py` | `DeclarativeError` - the base every typed failure carries its `error_code` on, below both the input layer and the declarative library. |

## Subfolders

| subfolder | what lives there |
| --- | --- |
| `render/` | The format set a product reaches the map in - a COG raster, a GeoJSON vector, an MDAL mesh, a chart payload - and everything it passes through on the way. |
| `model/` | The model connection: provider adapters, credentials, and the guards over a model turn. |
| `inputs/` | Every input into the system as a typed value: a Point, an Extent and a Shape, each with one ingestion from every form a user hands it in, the user's own file adopted as a layer, the fill that reads a run's inputs through them, and the gate a turn waits on a person at. |
| `server/` | The daemon core: connection loop, turn engine, dispatch, session state. |
| `store/` | What the daemon keeps: the object store, the case documents, and the sweep over the cache. |
| `tools/` | The registered tool surface: fetchers and derive tools, the mesh front, with search and meta beside them. |
| `workflows/` | The declarative engine layer: the runtime, the executor, TELEMAC. |
