# `inputs/` - the typed inputs

What a user hands the system arrives in many forms - a canvas pick, a typed
value, a selected layer, a place name - and is read in one. Each kind here has
ONE ingestion function that takes every form and returns the typed value, and
one home for what is done with that value afterwards: where a point is allowed
to be, what a shape's lines are, how an AOI becomes the bound domain.

The engine-neutral SLOTS a solved run stands on live here too - the domain it is
solved over, the bed its nodes carry, the runs of its edge that carry a boundary
condition, the level it stands at, the flow an inflow carries and the line a
placed read follows. A raster engine fills the first three with a grid, so none
of them belongs to an engine package.

Nothing here knows a template, a question or an engine. A slot names the kind it
takes and the role it plays; the ingestion is the same for every slot of that
kind, whichever workflow or route asked. A user's own FILE is an input of the
same standing, and it enters here too.

## Files

| file | what it is |
| --- | --- |
| `__init__.py` | The door: the kinds and their ingestions. |
| `domain.py` | `Domain` - the closed polygon a run is solved over - and `domain`, its ingestion from a drawing, the user's layer, a fetched waterbody or a typed ring; the runs a producer cut its polygon between and the COMPANION geometries it measured beside the polygon (a centerline, an outlet), each addressable as `Ref("<row>.<name>")`; the outer ring a mesher's extent reads, and the point inside it a nearest-site query ranks against. |
| `bed.py` | `Bed` - what the domain's nodes carry for elevation - and `bed`, its ingestion from a surface, a layer of soundings, or a depth in metres below the free surface; the derive a point survey is interpolated by, named. A measurement over a wider surface is composed by the merge derive in the DATA body, before anything reaches this slot. A bed is an ELEVATION, which is what makes the flip its own: a survey's DEPTHS below its project datum are read here as elevations, and the shift onto the run's own vertical frame is `vertical_datum.py`'s. |
| `boundary.py` | `BoundaryRun` - two Points on the domain's edge and a type (`wall`, `inflow`, `outflow`, `open`, `rating_curve`) - and `boundary_runs`, its ingestion from drawn lines, typed rows or a producer's own runs; the faces each run prescribes, walls excluded; and which types the water CROSSES, which is the edge that is not shoreline. |
| `observation.py` | `Observation` - one measured value with where, when and how far away it was measured - and `observation`, its ingestion from a fetched or supplied point layer (the nearest reporting site, the unit the slot reads) or from the number the caller stated; the sentence the run journal carries about the sample's age. |
| `line.py` | The LINE a placed read is measured along - a producer's own centerline, a drawn polyline, a line layer or typed vertices - read as ONE geometry, so a profile down a reach and a profile across a lake are the same read. |
| `slots.py` | The door onto the roles: which ingestion each ROLE reads through, what the row told its slot about the value, and what the canvas offers for a slot a user fills by hand. |
| `instant.py` | An INSTANT - the moment a run is about - and `instant`, its ingestion from a date, a datetime or a trailing-Z timestamp; `day`, the calendar day a daily record is asked over; and the `event_time` coercion the wire route passes through. A value that does not parse refuses rather than reading the latest. |
| `structure.py` | A STRUCTURE - a built thing in the water - ingested as the FOOTPRINT it occupies: a surveyed centreline given its declared width, or a polygon used verbatim. A centreline bounds no area, so subtracting one removes nothing. |
| `point.py` | `Point` - one location with an optional name - and `point`, its ingestion from a pick, a pair, a `"lat,lon"` string, a point layer or a geocoded place; the pick-or-wire coercion; containment in a domain, the move onto a wet node, the UTM projection and the context layer. |
| `extent.py` | `Extent` - one lon/lat box with an optional name - and `extent`, its ingestion from a bbox pick, the canvas AOI, a place or a layer's bounds; and what a box means against another - the same extent, or overlapping ground. |
| `shape.py` | `Shape` - one feature collection with an optional name - and `shape`, its ingestion from the draw, a stored layer, a geometry or typed vertices; its `polylines` and `polygons`. |
| `aoi.py` | AOI coercion and ACQUISITION: `location`/`bbox` resolved to exactly one area - an extent verbatim, the box around a Point, or a place geocoded through `geocode_place` - and the step that rebinds the domain to it. |
| `geometry.py` | Reading a GEOMETRY SOURCE - a layer object, its uri, a path or inline GeoJSON - flattened to its geometries, and the one UTM-zone rule. |
| `user_input.py` | The user-input species: clicks, sketches and typed values, normalized once per SHAPE, with the coercions that carry the wire route through the same normalizers. |
| `layer_fields.py` | Reading one field off whatever shape a fetched layer arrived in. |
| `vertical_datum.py` | The ZERO two sources count from - read off the layer that states it, or off the source row a bare name declares - and the refusal that names the two when they differ or one states none; `onto_frame`, which reads any source on the RUN's own vertical frame through the shift the source publishes about itself, else the offset the runtime fetches between the two frames at the point the run stands on. |
| `user_layer.py` | A file the USER pushed in: staged to object storage, validated and converted, then minted as a layer on their case with origin `user`. |
