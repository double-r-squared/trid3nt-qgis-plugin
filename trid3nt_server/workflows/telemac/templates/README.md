# `workflows/telemac/templates/` - one package per question

A template is the recipe (`<name>.py`), its declarations (`declarations.py`) and
its routing phrasings (`corpus.yaml`). The recipe carries a STEERING body of the
module's own raw keywords, the parts it is made of, the DATA chain it consumes,
the MESH recipe it triangulates on and the door it hands them to; the
declarations carry every value it can be given.

ONE TEMPLATE PER QUESTION. A structural fork of the deck - a tracer, an oil
slick and a moving bed fill DIFFERENT slots, not different values - is a
different template, never a switch on a param. What varies WITHIN one question is
a composite that states nothing when it is given nothing: a decay rate, a
dredging rule, a wind, a hyetograph against a constant rate.

## The templates

| folder | what it is |
| --- | --- |
| `__init__.py` | The package door. A template is imported for its registration, so nothing is re-exported here. |
| `shared/` | The bodies, chains and rows a good portion of several templates share. A body here is a PART a template LISTS, never a parent it extends. |
| `river_dye/` | `telemac_river_dye` - a conservative plume down a reach, decaying when a decaying substance is named; its release is a Point slot whose name the tracer takes. |
| `river_oil_spill/` | `telemac_river_oil_spill` - an oil slick on a reach: floating particles plus the dissolved fraction. |
| `river_scour/` | `telemac_river_scour` - a mobile bed under a reach: scour, deposition, grain sorting, and the NESTOR dredge rule. |
| `river_sediment_plume/` | `telemac_river_sediment_plume` - one settling class over a bed with no stock, so only what was injected deposits. |
| `do_sag/` | `telemac_do_sag` - an outfall's BOD load to the dissolved-oxygen sag downstream. |
| `rain_on_grid/` | `telemac_rain_on_grid` - a storm over a catchment to the outlet hydrograph; `cn_infiltration.py` is its SCS curve-number infiltration. |
| `agitation/` | `artemis_harbor_agitation` - swell at the harbour mouth to the agitation field behind a declared structure (ARTEMIS); `barrier.py` turns that structure's centreline into the footprint the mesh subtracts. |
| `stratified_flow/` | `telemac3d_stratified_flow` - a lake's water column to its vertical structure, wind circulation read off the same baroclinic run (TELEMAC-3D); `measured_bed.py` clips the water to the part the survey sounded, and `lake_level.py` reads the free surface the basin opens at off the gauge that watches it. |
