# `emission/` - a computed layer on its way to the map

Publication is automatic: a raster is published as it is produced, and every
`LayerURI` bound for the client crosses one seam before it is tracked. That is
what makes the layers a run put on the canvas an auditable list rather than a
side effect. The preset family that writes a style document lives here; the
seam that decides WHICH row a layer is painted by is `workflows/publishing`.

## Files

| file | what it is |
| --- | --- |
| `__init__.py` | The emission surface the rest of the server imports. |
| `charts.py` | The one Vega-Lite chart-envelope builder every chart routes through. |
| `cog.py` | COG encoding - the step that makes a raster renderable; best-effort by contract. |
| `layer_uri_emit.py` | The single seam every client-bound `LayerURI` crosses. |
| `mesh_display.py` | A built mesh as the SMS `.2dm` MDAL opens directly. |
| `outputs_seam.py` | `outputs.json` -> published layers: a solved product's style derived from its manifest entry. |
| `pipeline_emitter.py` | One session's pipeline snapshot and its accumulating `loaded_layers`. |
| `presets.py` | The preset family: four data kinds, one `.qml` writer. |
| `publish.py` | The raster publish mechanism - write the COG, register it, notify. |
| `uri_registry.py` | The session-scoped layer-handle registry - one uri per layer. |
