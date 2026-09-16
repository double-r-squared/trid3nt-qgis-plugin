# `workflows/telemac/templates/` - one package per question

A template is the recipe (`<name>.py`), its declarations (`declarations.py`) and
its routing phrasings (`corpus.yaml`), and NOTHING else - no helper module, no
function of its own. The recipe is VALUES: a STEERING body of the module's own
raw keywords restated whole, the DATA rows it consumes - the DOMAIN it solves
over, the BED every node carries, whatever else this question reads - the
OUTPUTS it PLACES, the reads that need a place the user gives, with the CAPTIONS
that name them, the ANSWER measured off the solved run, and the door it hands
them to; the declarations carry every value only this question asks.

A template states NO MESH RECIPE either, unless the question cannot be asked
over the one the workflow builds from those slots: `rain_on_grid` triangulates a
catchment as a band, `agitation` punches a structure out of the water, and those
two state their own. Every other package lets the workflow own its stages - the
recipe, the seated settle, the runtime levers - and states only what differs.

NAMED FOR THE QUESTION, NEVER FOR THE BODY. A template solves over any water:
drawn, picked, fetched or purely surveyed, with a river reach as ONE way to fill
the domain slot. So the folder is `water_temperature`, not `river_temperature`.

A template states NO OUTPUT OF ITS OWN. What the run writes, how each variable
is styled and what it is captioned are the MODULE's, stated once in its output
table (`../modules/README.md`); a template naming a printout list or a style row
is refused by the template-grammar lint. A question with no placed read lists
nothing at all.

ONE TEMPLATE PER QUESTION. A structural fork of the deck - a tracer, an oil
slick and a moving bed fill DIFFERENT slots, not different values - is a
different template, never a switch on a param. What varies WITHIN one question is
a composite that states nothing when it is given nothing: a decay rate, a wind,
a hyetograph against a constant rate.

## The templates

| folder | what it is |
| --- | --- |
| `__init__.py` | The package door. A template is imported for its registration, so nothing is re-exported here. |
| `dye_release/` | `telemac_dye_release` - a conservative plume down a reach, decaying when a decaying substance is named (its die-off presets are the template's values); its release is a Point slot whose name the tracer takes. |
| `oil_spill/` | `telemac_oil_spill` - an oil slick on a reach: the floats' track beside the dissolved fraction; the oil presets are the template's values. |
| `bed_scour/` | `telemac_bed_scour` - a mobile bed under a reach: the bed evolution off GAIA's own result, the bed over time and a marker beside them; the gradation presets are the template's values. |
| `channel_dredging/` | `telemac_channel_dredging` - a maintenance dredge of a navigation channel: the fairway held at a design depth under the surface the run opens at, the spoil laid into a disposal area, and the dredged and dumped volumes read off the engine's own report lines. The water it solves over is the domain slot, its bed the published USACE survey merged over the terrain, and its reference profiles are laid along the domain's own centerline. |
| `sediment_plume/` | `telemac_sediment_plume` - one settling class over a bed with no stock, so only what was injected deposits; the water it solves over is the domain slot, its bed the survey merged over the terrain, and the deposited fraction is held against the shared release-mass relation in `../helpers/released_mass.py`. |
| `do_sag/` | `telemac_do_sag` - an outfall's BOD load to the dissolved-oxygen profile downstream, drawn against the Streeter-Phelps closed form in `streeter_phelps.py` and the standard. The water it solves over is the domain slot walked downstream from the outfall, its bed the published survey merged over the terrain, and the profile is read along the domain's own centerline. |
| `water_temperature/` | `telemac_water_temperature` - how warm a body of water gets under a week of real weather: the WAQTEL heat budget over the hourly RAWS record the `Atmosphere` composite carries onto the deck, and the temperature series where the ask places it. |
| `micropollutant_release/` | `telemac_micropollutant_release` - a sorbing substance released into a reach: where it ends up dissolved, on the river's suspended sediment and on the bed, over the five tracers the micropol process appends. |
| `eutrophication/` | `telemac_eutrophication` - what ONE PASS down an enriched reach does to it: algal growth on stated nitrate and phosphate, the nutrient drawdown, and the oxygen the growth and its decay drive, all read along the reach rather than over a season. |
| `rain_on_grid/` | `telemac_rain_on_grid` - a storm over the domain this run solves on, to the peak depth and the outlet hydrograph, charted and placed as the station that carries it; the domain is the catchment traced upslope of a pour point or the basin the user drew, and the land-cover table its infiltration surface is read from is the template's value. |
| `agitation/` | `artemis_harbor_agitation` - swell at the harbour mouth to the agitation coefficient behind a declared structure, `field("KD")` off ARTEMIS and its profile along the transect `derive_transect` lays through the structure on the incident direction; the water it solves over is the domain slot and the structure's centreline becomes the footprint the mesh subtracts through the shared structure ingestion. |
| `stratified_flow/` | `telemac3d_stratified_flow` - a body of water's column to its vertical structure: the column at the deepest node charted against the prescribed one, and the wind circulation read off the same baroclinic run's velocity column (TELEMAC-3D). Its domain is drawn or supplied, its bed the charted lake survey or whatever the caller holds, and the level it opens at the gauge that watches it. |
