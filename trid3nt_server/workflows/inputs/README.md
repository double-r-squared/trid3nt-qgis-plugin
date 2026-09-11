# `workflows/inputs/` - the typed inputs

What a user hands a template arrives in many forms - a canvas pick, a typed
value, a selected layer, a place name - and is read in one. Each kind here has
ONE ingestion function that takes every form and returns the typed value, and
one home for what is done with that value afterwards: where a point is allowed
to be, what a shape's lines are, how an AOI becomes the bound domain.

Nothing here knows a template or a question. A slot names the kind it takes
and the role it plays; the ingestion is the same for every slot of that kind.

## Files

| file | what it is |
| --- | --- |
| `__init__.py` | The door: the three kinds and their ingestions. |
| `point.py` | `Point` - one location with an optional name - and `point`, its ingestion from a pick, a pair, a `"lat,lon"` string, a point layer or a geocoded place; the pick-or-wire coercion; containment in a domain, the move onto a wet node, the UTM projection and the context layer. |
| `extent.py` | `Extent` - one lon/lat box with an optional name - and `extent`, its ingestion from a bbox pick, the canvas AOI, a place or a layer's bounds. |
| `shape.py` | `Shape` - one feature collection with an optional name - and `shape`, its ingestion from the draw, a stored layer, a geometry or typed vertices; its `polylines` and `polygons`. |
| `aoi.py` | AOI coercion and ACQUISITION: `location`/`bbox` resolved to exactly one area, geocoded through `geocode_place`, and the step that rebinds the domain to it. |
| `geometry.py` | Reading a GEOMETRY SOURCE - a layer object, its uri, a path or inline GeoJSON - flattened to its geometries, and the one UTM-zone rule. |
| `layer_fields.py` | Reading one field off whatever shape a fetched layer arrived in. |
