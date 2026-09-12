# `render/` - the format set a product reaches the map in

Render accepts exactly what QGIS opens and nothing else: a raster as a COG, a
vector as GeoJSON, a mesh in any MDAL format with the dataset files written
beside it, a series or a profile as a chart payload. A product that is not in
the set does not arrive: the engine that produces one carries the transform that
normalizes it, beside its own module surface.

Publication is automatic: a product is published as it is made, and every
`LayerURI` bound for the client crosses one seam before it is tracked. That is
what makes the layers a run put on the canvas an auditable list rather than a
side effect. Nothing here paints a field or reshapes an engine's read - the
values are measured where they are read, and the style is the row the producer
declared beside them.

## Files

| file | what it is |
| --- | --- |
| `__init__.py` | The format set, stated. |
| `charts.py` | The one Vega-Lite chart-envelope builder every chart routes through. |
| `cog.py` | COG encoding - the step that makes a raster renderable; best-effort by contract. |
| `formats.py` | The four kinds a product arrives in, and the publish of one outputs list into layers and charts. |
| `layer_uri_emit.py` | The single seam every client-bound `LayerURI` crosses. |
| `mesh_display.py` | The MDAL faces: a built mesh as `.2dm`, and a derived value per node as the ASCII dataset loaded beside a mesh. |
| `outputs_seam.py` | A finished run's outputs, read back off its own record. |
| `pipeline_emitter.py` | One session's pipeline snapshot and its accumulating `loaded_layers`. |
| `presets.py` | The preset family: four data kinds, one `.qml` writer, and the range a producer measured. |
| `publish.py` | The raster publish mechanism - write the COG, register it, notify. |
| `restyle.py` | THE restyle seam, beside the presets: the user's edit of a declared style. |
| `uri_registry.py` | The session-scoped layer-handle registry - one uri per layer. |
