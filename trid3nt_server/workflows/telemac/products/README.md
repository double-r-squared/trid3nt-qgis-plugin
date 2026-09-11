# `workflows/telemac/products/` - what a solved run is answered with

A solve leaves files. What OPENS those files on the server for the open-water
questions - the free-surface, wave, agitation, 3D and coastal fields and the
catchment products - and the listing readers every primitive shares live here:
the worker is the engine room and derives nothing.

Everything past the primary layer is best-effort by contract. A missing
deposition COG, an unparsed slick or an unpublished results mesh retracts
nothing.

## Files

| file | what it is |
| --- | --- |
| `__init__.py` | The door. Consumers import the module they mean. |
| `postprocess_telemac.py` | A solved open-water result's fields to the map products still published here: the free surface, the wave, agitation, 3D and coastal fields. |
| `rain_on_grid.py` | A solved catchment to its peak-depth envelope and the outlet hydrograph the engine measured it by. |
| `run_reads.py` | What a solved run's own listing says, read on the server: the engine's demand, GAIA's closure, the water-volume closure, the outlet hydrograph, and the wetted fraction off the result. |
| `agitation.py` | A solved harbour to its Kd field: the sheltering the structure buys, the transect through its own shadow strip, and the dispersion the period and depth imply. |
| `stratified.py` | A solved basin to its water column: the planes at the deepest column, the profile the run started from and ended at, and the surface-downwind / return-flow-at-depth pair the same baroclinic run answers. |
