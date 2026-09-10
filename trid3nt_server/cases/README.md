# `cases/` - the case's own layers

The two seams that move a layer between the plugin and a case in the direction
the rest of the server does not: a layer the user pushed UP into a case, and a
read the user asked for by clicking the map. Both answer about layers that are
already in object storage; neither computes one.

## Files

| file | what it is |
| --- | --- |
| `__init__.py` | The package door; only names that do not collide with a submodule are re-exported. |
| `ingest_user_layer.py` | A plugin-pushed vector or raster adopted into a case as a first-class layer. |
| `probe_point.py` | Every raster on a case read at one clicked point, with outside-extent, nodata and unreadable reported rather than hidden. |
