# DataSeam - derived view

GENERATED from `docs/model/data-seam.sysml` by `scripts/model_check.py --view`. Never hand-edited: regenerate it, and `tests/test_model_conformance.py` fails while it is stale.

Plane: **workflow**. System: **fetcher**. One seam of the system of systems indexed by [`README.md`](README.md) - never the whole picture.

## Blocks and flows

```mermaid
flowchart LR
    bedLadderRegistry["BedLadderRegistry<br/>trid3nt_server/fallbacks/ladder.py"]
    bedPainter["BedPainter<br/>trid3nt_server/workflows/mesh/shared/primitives.py"]
    bedResultModel["BedResultModel<br/>contracts/trid3nt_contracts/execution.py"]
    blueTopoDeclaration["BedSourceDeclaration<br/>trid3nt_server/tools/fetchers/ocean/fetch_bluetopo/source.yaml"]
    blueTopoSource["BlueTopoSource<br/>trid3nt_server/tools/fetchers/ocean/fetch_bluetopo/hooks.py"]
    coastalComposite["CoastalComposite<br/>trid3nt_server/tools/fetchers/_router/hooks/topobathy.py"]
    coastalDeclaration["BedSourceDeclaration<br/>trid3nt_server/tools/fetchers/ocean/fetch_topobathy/source.yaml"]
    coastlineDeclaration["BedSourceDeclaration<br/>trid3nt_server/tools/fetchers/ocean/fetch_osm_coastline/source.yaml"]
    fetcherRouter["FetcherRouter<br/>trid3nt_server/tools/fetchers/_router/router.py"]
    freeSurfaceReader["FreeSurfaceReader<br/>trid3nt_server/workflows/telemac/templates/stratified_flow/lake_level.py"]
    ladderWalker["LadderWalker<br/>trid3nt_server/fallbacks/walker.py"]
    lakeDeclaration["BedSourceDeclaration<br/>trid3nt_server/tools/fetchers/ocean/fetch_greatlakes_bathymetry/source.yaml"]
    lakeLevelDeclaration["BedSourceDeclaration<br/>trid3nt_server/tools/fetchers/ocean/fetch_greatlakes_water_level/source.yaml"]
    shorelineLadder["ShorelineLadder<br/>trid3nt_server/workflows/mesh/shoreline.py"]
    waterBodyClassifier["WaterBodyClassifier<br/>trid3nt_server/tools/fetchers/_router/hooks/topobathy_class.py"]
    blueTopoSource -- "BedProvenance" --> bedResultModel
    blueTopoDeclaration -- "BedSourceParams" --> blueTopoSource
    waterBodyClassifier -- "PartialCoverGap" --> ladderWalker
    waterBodyClassifier -- "BedLadderDeclaration" --> bedLadderRegistry
    waterBodyClassifier -- "BedLadderRung" --> bedLadderRegistry
    coastalDeclaration -- "WaterBodyClassDeclaration" --> waterBodyClassifier
    coastalComposite -- "PartialCoverGap" --> ladderWalker
    coastalComposite -- "BedLadderDeclaration" --> bedLadderRegistry
    coastalComposite -- "BedLadderRung" --> bedLadderRegistry
    bedLadderRegistry -- "PerRequestLadderChoice" --> fetcherRouter
    waterBodyClassifier -- "StoppedClassRefusal" --> coastalComposite
```

## Interface items

### `BedLadderDeclaration`

A whole ladder: which capability it governs, its ordered rungs, and the typed code its terminal refusal wears so a refusal keeps the capability's own error vocabulary.

| item | type | required |
| --- | --- | --- |
| `capability` | String | required |
| `rungs` | RungList | required |
| `refuse_error_code` | String | required |
| `coverage_exempt_params` | StringList | optional |

### `BedLadderRung`

One alternative on a bed ladder. ``consequence`` is what descending to it COSTS, and it is the only thing the loudness gate keys on, so a rung that crosses datasets must say so or the gate never asks.

| item | type | required |
| --- | --- | --- |
| `name` | String | required |
| `consequence` | String | required |
| `describes` | String | required |
| `call` | String | optional |
| `source` | String | optional |
| `params` | Map | optional |
| `supplies_param` | String | optional |

### `BedProvenance`

What a bed fetch knows and the bytes do not. The DATUM is the load- bearing one: BlueTopo is on NAVD88, an orthometric datum and explicitly not a navigational or tidal one, which is the whole reason it merges with a NAVD88 land DEM with no transformation. ``coverage_fraction`` is below 1.0 for any AOI holding land, by construction, and saying so is the honest report rather than a failure.

| item | type | required |
| --- | --- | --- |
| `vertical_datum` | String | required |
| `tile_count` | Integer | required |
| `resolution_tiers` | StringList | required |
| `coverage_fraction` | Real | required |
| `rung_coverage` | Map | optional |

### `BedSourceParams`

The request a bed source takes. ``min_pixel_m`` only COARSENS: it never invents a cell finer than the tile that was read, which is why it is the param the resolution declaration hangs off.

| item | type | required |
| --- | --- | --- |
| `bbox` | RealList | required |
| `target_crs` | String | required |
| `timeout_s` | Real | required |
| `min_pixel_m` | Real | optional |

### `PartialCoverGap`

A rung that served PART of the request, and how much. The walker reads both off it: the share already painted, and the note the gate shows the person being asked to accept the substitution that fills the rest.

| item | type | required |
| --- | --- | --- |
| `covered_fraction` | Real | required |
| `gap_note` | String | required |

### `PerRequestLadderChoice`

Which ladder governs ONE request. The chooser reads the request's own params; returning nothing means no per-request ladder applies and the capability's static ladder stands.

| item | type | required |
| --- | --- | --- |
| `capability` | String | required |
| `params` | Map | required |
| `selector` | Callable | optional |

### `StoppedClassRefusal`

A class whose every rung stopped, and what is missing before it can have one. It is carried as DATA so the refusal names the gap rather than saying only that there is one.

| item | type | required |
| --- | --- | --- |
| `STOPPED_CLASSES` | Map | required |
| `water_body_class` | String | required |

### `WaterBodyClassDeclaration`

The class vocabulary, declared on the row and read by the classifier. Three names and no fourth: a class the row can state is a class a ladder answers for, and the two must not drift apart.

| item | type | required |
| --- | --- | --- |
| `water_body_class` | String | required |
| `coastal_estuary` | String | required |
| `navigable_river` | String | required |
| `small_inland_stream` | String | required |

## Requirements

| requirement | satisfied by | verified by |
| --- | --- | --- |
| **AFreeSurfaceAndABedShareOneStatedDatum** | `lakeLevelDeclaration`, `lakeDeclaration`, `freeSurfaceReader` | `tests/test_open_water_domains.py::test_the_level_is_the_nearest_gauges_last_reading`<br/>`tests/test_open_water_domains.py::test_a_level_and_a_bed_on_two_datums_refuse_by_name`<br/>`tests/test_open_water_domains.py::test_water_no_gauge_watches_refuses_rather_than_opening_at_the_datum`<br/>`tests/test_open_water_domains.py::test_the_thermocline_is_stated_below_the_water_top_not_the_datum` |
| **AStoppedRungNeverShips** | `waterBodyClassifier`, `coastalComposite` | `tests/test_bathymetry_data_seam.py::test_a_small_inland_stream_has_no_ladder_and_refuses_naming_both_gaps`<br/>`tests/test_bathymetry_data_seam.py::test_a_navigable_river_has_no_ladder_and_refuses_naming_its_stopped_primary`<br/>`tests/test_bathymetry_data_seam.py::test_no_ladder_anywhere_ships_an_ehydro_rung`<br/>`tests/test_bathymetry_data_seam.py::test_a_stopped_class_refuses_before_the_cache_and_before_the_network`<br/>`tests/test_bathymetry_data_seam.py::test_a_tile_row_with_no_delivered_link_is_not_data`<br/>`tests/test_bathymetry_data_seam.py::test_an_aoi_no_delivered_tile_reaches_refuses_by_name`<br/>`tests/test_bathymetry_data_seam.py::test_the_synthetic_slot_is_stated_as_deferred_rather_than_forgotten` |
| **BedSourceStatesItsDatum** | `blueTopoSource`, `blueTopoDeclaration`, `bedResultModel` | `tests/test_bathymetry_data_seam.py::test_a_tile_that_states_navd88_passes_the_datum_gate`<br/>`tests/test_bathymetry_data_seam.py::test_a_tile_that_states_no_navd88_refuses_rather_than_merging`<br/>`tests/test_bathymetry_data_seam.py::test_the_envelope_states_the_datum_in_provenance`<br/>`tests/test_bathymetry_data_seam.py::test_the_bluetopo_spec_declares_the_delegate_hooks_and_the_result_model` |
| **OneSourceReadsOnOneStatedDatum** | `bedPainter`, `lakeDeclaration`, `coastalDeclaration`, `blueTopoDeclaration` | `tests/test_bathymetry_data_seam.py::test_every_bed_capable_source_row_states_its_vertical_datum`<br/>`tests/test_bathymetry_data_seam.py::test_the_lake_row_pins_one_product_of_the_mixed_mosaic`<br/>`tests/test_mesh_topology_and_bed.py::test_a_source_row_that_states_no_datum_is_not_a_bed`<br/>`tests/test_mesh_topology_and_bed.py::test_the_bed_card_states_the_datum_and_the_native_cell_of_its_source`<br/>`tests/test_emit_on_fetch_seam.py::test_input_layer_name_shape_and_purpose` |
| **PerWaterBodyClassLadders** | `waterBodyClassifier`, `bedLadderRegistry`, `fetcherRouter` | `tests/test_bathymetry_data_seam.py::test_the_coastal_ladder_puts_bluetopo_above_the_cudem_composite`<br/>`tests/test_bathymetry_data_seam.py::test_every_declared_class_either_ladders_or_stops_by_name`<br/>`tests/test_bathymetry_data_seam.py::test_an_undeclared_class_keeps_the_rows_unclassed_ladder`<br/>`tests/test_bathymetry_data_seam.py::test_every_class_ladder_ends_at_refuse_with_the_rows_own_error_code` |
| **SubstitutionIsDeclared** | `waterBodyClassifier`, `ladderWalker`, `coastalComposite` | `tests/test_bathymetry_data_seam.py::test_falling_from_bluetopo_to_the_cudem_composite_is_a_declared_cross_dataset_rung`<br/>`tests/test_bathymetry_data_seam.py::test_a_partial_bluetopo_cover_is_reported_as_a_gap_not_a_whole_bed`<br/>`tests/test_fallback_ladder.py::test_declared_rung_fills_the_gap_and_splits_coverage` |
| **ThalwegBurningIsNeverABedSource** | `blueTopoSource`, `waterBodyClassifier`, `coastalComposite` | `tests/test_model_conformance.py::test_the_model_conforms_to_the_tree`<br/>`tests/test_bathymetry_data_seam.py::test_the_selected_tiles_run_coarsest_first_so_the_finest_paints_last` |
| **TheShorelineIsALadderWithStatedFidelity** | `shorelineLadder`, `coastlineDeclaration` | `tests/test_mesh_shoreline_ladder.py::test_a_coarse_ask_is_served_by_the_local_file_without_a_fetch`<br/>`tests/test_mesh_shoreline_ladder.py::test_a_harbour_ask_climbs_past_a_shoreline_that_cannot_describe_it`<br/>`tests/test_mesh_shoreline_ladder.py::test_an_ask_no_rung_resolves_refuses_naming_every_rung`<br/>`tests/test_mesh_shoreline_ladder.py::test_an_unset_shoreline_variable_is_named_rather_than_exported`<br/>`tests/test_mesh_shoreline_ladder.py::test_the_land_is_on_the_left_of_the_way_the_way_was_drawn` |
| **WaterBodyClassComesFromHeldData** | `waterBodyClassifier`, `coastalDeclaration` | `tests/test_bathymetry_data_seam.py::test_a_tidal_ftype_classifies_the_reach_as_coastal_estuary`<br/>`tests/test_bathymetry_data_seam.py::test_no_mapped_water_surface_classifies_the_reach_as_a_small_inland_stream`<br/>`tests/test_bathymetry_data_seam.py::test_a_wide_inland_river_refuses_naming_what_would_have_decided_it`<br/>`tests/test_bathymetry_data_seam.py::test_the_classifier_never_guesses_a_class_from_an_unknown_ftype`<br/>`tests/test_bathymetry_data_seam.py::test_the_topobathy_row_declares_the_water_body_class_it_ladders_on` |

## What each requirement says

- **AFreeSurfaceAndABedShareOneStatedDatum** - RULING 2026-09-06 (docs/IDEAS.md, "REMEDY RULINGS" (b)): a water body's LEVEL is an observation, fetched the way a river's discharge is - the nearest gauge to the AOI, at the day the run is about - and the run's free surface opens at it. Data is assumed true; nothing thresholds the reading. WHAT MAKES THE ARITHMETIC LEGAL is that both documents state their zero. The gauge row and the bed row each state a vertical datum, and the subtraction between a level and an elevation is only defined when the two statements agree. Two rows stating different zeros, with no offset stated anywhere, REFUSE BY NAME - both names and both datums - rather than adding numbers that are not on one axis. A row stating none refuses for the reason OneSourceReadsOnOneStatedDatum already gives. Left at the dictionary's own zero the free surface sits ON the chart datum, which is where the bed is counted from, so the rim of a surveyed basin carries no water at all. Clipping that rim away would be a threshold on values; giving it its real water is the observation.
- **AStoppedRungNeverShips** - SIGNED DECISION - load-bearing unverified items are verified live before any rung ships, and a dead assumption stops its rung rather than shipping it. Two rungs the methodology names stopped on measured grounds and are absent here rather than declared: eHydro, the navigable primary: its queryable surface is one layer of survey-boundary polygons carrying a horizontal projection and no vertical datum field at all, with the soundings behind per-survey bulk archives on another host. A bed whose datum is unknowable from its index cannot state its datum, so it cannot be on this ladder. What the methodology leaves under it is BlueTopo alone, and one source is not a degradation path - so the navigable class has no ladder either and refuses, naming the stopped primary. NXSDB, the small-stream primary: its measurements carry depth below the water surface and no bed elevation and no vertical datum, published as one national GeoPackage on a host serving no range requests. That is a producer's input, not a bed anyone can fetch. So the small-stream class likewise has no rung and REFUSES, naming both gaps and the synthetic slot below them. That slot is DEFERRED BY RULING, not merely unbuilt (HAPPY PATH FIRST, SYNTHETIC DEFERRED, 2026-09-02, amending the signed methodology): no synthetic bathymetry is produced now, the Bieger regression the methodology named as the candidate does not build, and whether a fabricated bed may ever stand in for a survey is a USER decision rather than a gap for an implementation to close. Best-case behaviour is established first and never intertwined with sad-path interpolation. The slot is therefore STATED and EMPTY - an absence somebody decided, so that a later reader finds a ruling where they would otherwise find an oversight. Empty, it is a refusal, and the refusal is the honest floor working.
- **BedSourceStatesItsDatum** - SIGNED DECISION - the bed's vertical datum is stated in provenance, not assumed by a reader. BlueTopo publishes NAVD88 and says so twice in each tile; a tile that states neither is refused rather than merged, because the whole reason this source outranks the alternatives is that its datum is known. The datum, the tiles, the tiers and the measured coverage ride on the returned layer, since none of them survives in the raster bytes.
- **OneSourceReadsOnOneStatedDatum** - RULING 2026-09-06 (docs/IDEAS.md, "STAGE 3 RULINGS, ACCEPTANCE FINDINGS", (b); recorded in docs/REANALYZE_LEDGER.md, "2026-09-06 - bed datum: one stated datum per source, refuse otherwise"): every bed source states its VERTICAL DATUM on its own source row, taken from the dataset's own documentation and never inferred from the bytes - a raster carries numbers, and what they are counted from is not among them. A row that cannot state one carries none, and a row carrying none is refused as a bed rather than painted and read later as if it were on whatever the run assumed. A mosaic that mixes datums is therefore not a bed source. The NCEI DEM_all endpoint serves the lake-datum bathymetry, the NAVD88 coastal tiles, the same tiles on MHW and the EGM2008 global bases merged under one name, so the row that reads it as a bed pins the ONE product it means and states that product's datum. The stated datum is the WHOLE guard. A post-paint population test over the elevations is not one: real terrain puts an outlier as far from its neighbours as a datum offset does, so such a test reads the shape of the ground rather than the reference it is counted from. The datum rides onto the layer's provenance card beside the acquisition instant and the native cell, since a bed the user is shown to refine is a bed they may stitch another source onto, and that is the metadata the two have to be compared on.
- **PerWaterBodyClassLadders** - SIGNED DECISION - per-class ladders on the topobathy row (bathymetry methodology, M1). A bed source fits one KIND of water and not another, so the row has one ladder per class rather than one ladder: coastal or estuary takes BlueTopo above the CUDEM composite, and the other two classes take none - each stopped for a measured reason recorded under AStoppedRungNeverShips. Which ladder governs a request is resolved from the request itself, before the walk. A row that declares no class keeps the unclassed ladder. A class nobody declared is not a class anybody may assume, so the per-class ladders govern the row that states which water it is and nothing else.
- **SubstitutionIsDeclared** - SIGNED DECISION - a cross-dataset substitution is a DECLARED rung taken loudly through the gate, never a fill a fetch performs on its own. Falling from BlueTopo to the CUDEM composite crosses datasets and wears that consequence, so the gate asks before it happens and the activation records which rung painted what share. A rung that served only part of the request says so as a gap carrying the measured share, which is what lets the next rung fill the rest under the gate instead of the first rung returning a half-painted bed as a whole one.
- **ThalwegBurningIsNeverABedSource** - SIGNED DECISION - thalweg and stream burning stay REJECTED as a bed source. Burning lowers DEM cells along a mapped network so flow routing honours it: the depth is chosen for ROUTING reasons and bears no relation to true bathymetry, and a burned surface is unsuited even for measuring local slope. It enforces direction and connectivity and says nothing about cross-sectional conveyance. Reading a routing offset as a channel depth is precisely the confusion the correct-data-class law forbids. The rule is written as a dependency: no module on this bed seam may reach for the hydrologic-conditioning family, which is where fill, resolve-flats and stream-network conditioning live. A bed source that imported them would be conditioning terrain and calling the result a survey.
- **TheShorelineIsALadderWithStatedFidelity** - RULING 2026-09-06 (docs/IDEAS.md, "STAGE 3 RULINGS, LIVE FINDINGS", (c)): a declarative OSM coastline fetcher is the HARBOUR-SCALE rung of the shoreline a domain is cut from, and GSHHG at full resolution is the COARSE rung; the ladder resolves the file, the rung is journaled, and an ask below every rung's coverage refuses BY NAME. A shoreline is the same kind of input as a bed and carries the same obligation: a substrate coarser than the triangles asked for does not produce a coarser domain, it produces scraps that touch at points, which the mesh acceptance then refuses. Naming the coverage is what turns that into an answer a caller can act on. The machine-local rung is named by TRID3NT_GSHHG_SHP and nothing exports it on a caller's behalf: unset, it states no resolution, is walked last, and is named in the refusal.
- **WaterBodyClassComesFromHeldData** - SIGNED DECISION - the class comes from data the chain already holds (the reach, water and waterbody rows), never from a guess. A tidal FType among the mapped water makes the reach coastal; no mapped water surface at all makes it a small inland stream, because a channel too narrow to be mapped as an area is a flowline only and that absence is a real answer about the channel. Where the held rows CANNOT decide, the classifier REFUSES and names what was missing rather than falling to a conservative default. A mapped inland channel surface says the river is wide; the navigable class means a federally maintained navigation channel, and no row this chain holds says whether this one is - so it refuses and names that.
