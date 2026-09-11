# `workflows/telemac/products/` - what a solved run is answered with

A solve leaves files. What OPENS those files on the server, and what turns what
they hold into the answer - the rasterized map layers, the scalars a narration
quotes, the chart spec - lives here, and only here: the worker is the engine room
and derives nothing.

Everything past the primary layer is best-effort by contract. A missing
deposition COG, an unparsed slick or an unpublished results mesh retracts
nothing.

## Files

| file | what it is |
| --- | --- |
| `__init__.py` | The door. Consumers import the module they mean. |
| `postprocess_telemac.py` | A solved result's fields to the per-question map products still published here: the transported field an oil or a sediment run leads with, the bed evolution, the free surface, the oxygen sag, the wave and coastal fields. |
| `products.py` | A solved reach to its map layers, its scalars and its chart spec, for the questions whose products ride beside the transported field: the slick, the bed, the sag. |
| `rain_on_grid.py` | A solved catchment to its peak-depth envelope and the outlet hydrograph the engine measured it by. |
| `run_reads.py` | What a solved run's own files say, read on the server: GAIA's closure out of the listing, the slick out of the drogues track. |
| `agitation.py` | A solved harbour to its Kd field: the sheltering the structure buys, the transect through its own shadow strip, and the dispersion the period and depth imply. |
| `stratified.py` | A solved basin to its water column: the planes at the deepest column, the profile the run started from and ended at, and the surface-downwind / return-flow-at-depth pair the same baroclinic run answers. |
| `streeter_phelps.py` | The Streeter-Phelps closed-form dissolved-oxygen sag, the WAQTEL O2 verification reference. |
