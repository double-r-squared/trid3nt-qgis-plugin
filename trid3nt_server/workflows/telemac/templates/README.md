# `workflows/telemac/templates/` - one package per question

A template is the recipe (`<name>.py`), its declarations (`declarations.py`) and
its routing phrasings (`corpus.yaml`). The recipe is VALUES: a STEERING body of
the module's own raw keywords restated whole, the DATA chain it consumes, the
MESH recipe it triangulates on, the OUTPUTS it reads off the solved run with the
CAPTIONS that name them and the ANSWER measured from them, and the door it hands
them to; the declarations carry every value it can be given, the preset tables
among them. A template defines no function; an analytic reference it draws lives
in a sibling module and is named on the outputs list.

ONE TEMPLATE PER QUESTION. A structural fork of the deck - a tracer, an oil
slick and a moving bed fill DIFFERENT slots, not different values - is a
different template, never a switch on a param. What varies WITHIN one question is
a composite that states nothing when it is given nothing: a decay rate, a
dredging rule, a wind, a hyetograph against a constant rate.

## The templates

| folder | what it is |
| --- | --- |
| `__init__.py` | The package door. A template is imported for its registration, so nothing is re-exported here. |
| `reach.py` | The ONE shared DATA row module, by exception: the reach rows two or more river templates read - the geocode, the seed, the flowline, the two coverage measures, the carrier discharge at the cycle the ask names, the signed net rain, the event-time coercion - and the steps that name them. A template restates its own keywords and params; it names these rows. |
| `river_dye/` | `telemac_river_dye` - a conservative plume down a reach, decaying when a decaying substance is named (its die-off presets are the template's values); its release is a Point slot whose name the tracer takes. |
| `river_oil_spill/` | `telemac_river_oil_spill` - an oil slick on a reach: the floats' track beside the dissolved fraction; the oil presets are the template's values. |
| `river_scour/` | `telemac_river_scour` - a mobile bed under a reach: the bed evolution off GAIA's own result, the bed over time, a marker beside them, and the NESTOR dredge rule; the gradation presets are the template's values. |
| `river_sediment_plume/` | `telemac_river_sediment_plume` - one settling class over a bed with no stock, so only what was injected deposits; `river_sediment_plume/injected_mass.py` resolves the mass the pulse put in, which the deposited fraction is measured against. |
| `do_sag/` | `telemac_do_sag` - an outfall's BOD load to the dissolved-oxygen profile downstream, drawn against the Streeter-Phelps closed form in `streeter_phelps.py` and the standard. |
| `rain_on_grid/` | `telemac_rain_on_grid` - a storm over a catchment to the peak depth and the outlet hydrograph, charted and placed as the station that carries it; the land-cover table its infiltration surface is read from is the template's value, and `rain_on_grid/storm.py` resolves the storm it is driven by, a real hyetograph or a constant design rate. |
| `agitation/` | `artemis_harbor_agitation` - swell at the harbour mouth to the agitation coefficient behind a declared structure, `field("KD")` off ARTEMIS and its profile along the transect `derive_transect` lays through the structure on the incident direction; `agitation/barrier.py` turns that structure's centreline into the footprint the mesh subtracts. |
| `stratified_flow/` | `telemac3d_stratified_flow` - a lake's water column to its vertical structure: the temperature on the surface and bottom planes as one-scale maps, the column at the deepest node charted against the prescribed one, and the wind circulation read off the same baroclinic run's velocity column (TELEMAC-3D); `measured_bed.py` clips the water to the part the survey sounded, and `lake_level.py` reads the free surface the basin opens at off the gauge that watches it. |
